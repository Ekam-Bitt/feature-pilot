"""FastAPI surface.

Four endpoints, which together are everything the CLI does — deliberately, so the
Phase 2 web UI has no reason to grow behaviour the terminal lacks:

    POST /runs                 start a run
    GET  /runs/{id}            status, including what it is waiting for
    GET  /runs/{id}/stream     SSE: live node-by-node activity
    POST /runs/{id}/approve    answer a pending plan approval

The stream replays what has already happened before going live, so a client that
connects late sees the whole run rather than joining mid-story.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from featurepilot.api.manager import RunManager, replay, subscribe
from featurepilot.config import get_settings
from featurepilot.contracts import HumanDecision

log = logging.getLogger(__name__)

#: Heartbeat cadence for an idle stream. Without it, proxies close a quiet SSE
#: connection during a long model call and the UI looks dead.
KEEPALIVE_SECONDS = 15.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.manager = RunManager(get_settings())
    try:
        yield
    finally:
        # Cancel in-flight runs so their containers are destroyed rather than
        # left behind for the reaper.
        await app.state.manager.aclose()


app = FastAPI(
    title="Feature Pilot",
    summary="Turn a GitHub issue into a tested patch.",
    lifespan=lifespan,
)


class StartRun(BaseModel):
    repo: str = Field(default="fixtures/target-repo", description="Repository to work on.")
    issue: str | None = Field(default=None, description="Issue text, inline.")
    issue_path: str | None = Field(default=None, description="Path to an issue file.")
    issue_ref: str = Field(default="", description="Human-readable reference.")
    auto_approve: bool = Field(
        default=False, description="Skip the plan gate. Open questions still stop the run."
    )


class Decision(BaseModel):
    verdict: str = Field(default="approve", description="approve | reject")
    feedback: str = ""
    answers: list[str] = Field(default_factory=list)


def _manager() -> RunManager:
    return app.state.manager  # type: ignore[no-any-return]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _read_issue(issue_path: str, issue_ref: str) -> tuple[str, str]:
    path = Path(issue_path)
    if not path.is_file():
        raise HTTPException(400, f"no such issue file: {issue_path}")
    return path.read_text(encoding="utf-8"), (issue_ref or str(path))


@app.post("/runs", status_code=201)
async def create_run(body: StartRun) -> dict[str, Any]:
    if body.issue_path:
        # Filesystem calls go to a thread: small as these reads are, blocking the
        # event loop in a request handler stalls every in-flight SSE stream too.
        issue, ref = await asyncio.to_thread(_read_issue, body.issue_path, body.issue_ref)
    elif body.issue:
        issue, ref = body.issue, body.issue_ref or "(inline)"
    else:
        raise HTTPException(400, "provide either issue or issue_path")

    repo = Path(body.repo)
    if not await asyncio.to_thread(repo.is_dir):
        raise HTTPException(400, f"no such repository: {body.repo}")

    record = await _manager().start(repo, issue, issue_ref=ref, auto_approve=body.auto_approve)
    return record.public()


@app.get("/runs")
async def list_runs() -> list[dict[str, Any]]:
    return _manager().list()


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    record = _manager().get(run_id)
    if record is None:
        raise HTTPException(404, f"unknown run {run_id}")
    return record.public()


@app.post("/runs/{run_id}/approve")
async def approve(run_id: str, body: Decision) -> dict[str, Any]:
    record = _manager().get(run_id)
    if record is None:
        raise HTTPException(404, f"unknown run {run_id}")
    if record.pending is None:
        raise HTTPException(409, "this run is not waiting for a decision")

    rejected = body.verdict.lower().startswith("r")
    decision = HumanDecision(
        verdict="reject" if rejected else "approve",
        feedback=body.feedback,
        answers=body.answers,
    )
    verdict = decision.verdict
    ok = await _manager().approve(run_id, decision)
    if not ok:
        raise HTTPException(409, "this run is not waiting for a decision")
    return {"run_id": run_id, "verdict": verdict}


@app.delete("/runs/{run_id}")
async def cancel_run(run_id: str) -> dict[str, Any]:
    if not await _manager().cancel(run_id):
        raise HTTPException(404, f"no cancellable run {run_id}")
    return {"run_id": run_id, "cancelled": True}


@app.get("/runs/{run_id}/stream")
async def stream(run_id: str) -> EventSourceResponse:
    record = _manager().get(run_id)
    if record is None:
        raise HTTPException(404, f"unknown run {run_id}")
    return EventSourceResponse(_events(run_id), ping=int(KEEPALIVE_SECONDS))


async def _events(run_id: str) -> AsyncIterator[dict[str, str]]:
    """Replay, then follow.

    Replaying first means a client that connects after the run started still sees
    the plan it is being asked to approve, rather than an empty pane.
    """
    manager = _manager()
    record = manager.get(run_id)
    if record is None:
        return

    seen = 0
    for event in replay(record):
        seen += 1
        yield {"event": str(event.kind), "data": event.model_dump_json()}

    pubsub = await subscribe(manager.settings, run_id)
    try:
        while True:
            if pubsub is not None:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message and message.get("data"):
                    raw = message["data"]
                    text = raw.decode() if isinstance(raw, bytes) else str(raw)
                    with contextlib.suppress(json.JSONDecodeError):
                        payload = json.loads(text)
                        yield {"event": str(payload.get("kind", "message")), "data": text}
                        continue
            else:
                # Redis is unavailable; fall back to draining the in-memory sink
                # so the stream still works, just without cross-process fan-out.
                events = replay(record)
                for event in events[seen:]:
                    yield {"event": str(event.kind), "data": event.model_dump_json()}
                seen = len(events)
                await asyncio.sleep(0.5)

            current = manager.get(run_id)
            if current is None:
                return
            if current.finished:
                yield {"event": "run_finished", "data": json.dumps(current.public())}
                return
            if current.pending is not None:
                yield {
                    "event": "awaiting_human",
                    "data": json.dumps({"run_id": run_id, "pending": current.pending}),
                }
                # Wait to be woken rather than re-announcing on a timer. The
                # event is also set when a run fails or is cancelled, so this
                # cannot hang on a run that will never be approved.
                await current.resumed.wait()
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe()
                await pubsub.aclose()


def serve() -> None:  # pragma: no cover - entry point
    import uvicorn

    settings = get_settings()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":  # pragma: no cover
    serve()
