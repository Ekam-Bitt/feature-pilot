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
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, SecretStr
from sse_starlette.sse import EventSourceResponse

from featurepilot.api.manager import RunManager, replay, subscribe
from featurepilot.config import get_settings
from featurepilot.contracts import HumanDecision
from featurepilot.credentials import RunCredentials
from featurepilot.github.client import GhError
from featurepilot.github.issues import parse_issue_url
from featurepilot.github.publish import PublishError
from featurepilot.lifecycle import RunPhase

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
# The frontend is served from another origin, so without this a browser can
# start a run and then be unable to read its own event stream. Credentials are
# sent in the body, not as cookies, so `allow_credentials` stays off.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins(),
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["content-type"],
)


class StartRun(BaseModel):
    repo: str = Field(default="fixtures/target-repo", description="Repository to work on.")
    issue: str | None = Field(default=None, description="Issue text, inline.")
    issue_path: str | None = Field(default=None, description="Path to an issue file.")
    issue_url: str | None = Field(
        default=None, description="Public GitHub issue URL; clones the repo and solves it."
    )
    issue_ref: str = Field(default="", description="Human-readable reference.")
    auto_approve: bool = Field(
        default=False, description="Skip the plan gate. Open questions still stop the run."
    )
    auto_publish: bool = Field(
        default=False, description="URL runs only: fork, push, and open the PR when DONE."
    )
    draft: bool = Field(default=False, description="Open the PR as a draft.")
    # Write-only: held in memory for this run, never persisted, never returned.
    # Omit them to use the server's own credentials.
    anthropic_api_key: SecretStr | None = Field(
        default=None, description="Your own Anthropic key, used for this run only."
    )
    github_token: SecretStr | None = Field(
        default=None, description="Your own GitHub token, so the PR is authored by you."
    )

    def credentials(self) -> RunCredentials:
        return RunCredentials(
            anthropic_api_key=self.anthropic_api_key, github_token=self.github_token
        )


class PublishRequest(BaseModel):
    draft: bool = Field(default=False, description="Open the PR as a draft.")


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
    sources = [s for s in (body.issue, body.issue_path, body.issue_url) if s]
    if len(sources) != 1:
        raise HTTPException(400, "provide exactly one of issue, issue_path, issue_url")

    if body.issue_url:
        try:
            issue_ref = parse_issue_url(body.issue_url)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        credentials = body.credentials()
        # Capacity before budget: a full host cannot serve anyone, whereas an
        # exhausted allowance still serves a visitor who brings their own key.
        if not _manager().accepting_runs:
            raise HTTPException(
                503,
                "at capacity — every sandbox slot and the queue are full; try again shortly",
                headers={"Retry-After": "60"},
            )
        if not _manager().within_daily_budget(credentials):
            raise HTTPException(
                429,
                "this server's daily model budget is spent. Supply your own "
                "Anthropic key to run now — it is used for your session only.",
            )
        # The clone happens in the driving task; this returns before it lands.
        record = await _manager().start_from_url(
            issue_ref,
            auto_approve=body.auto_approve,
            auto_publish=body.auto_publish,
            draft=body.draft,
            credentials=credentials,
        )
        return record.public()

    if not getattr(_manager().settings, "allow_local_repos", True):
        raise HTTPException(
            403,
            "this deployment only runs from a public issue URL — pass issue_url",
        )

    if body.issue_path:
        # Filesystem calls go to a thread: small as these reads are, blocking the
        # event loop in a request handler stalls every in-flight SSE stream too.
        issue, ref = await asyncio.to_thread(_read_issue, body.issue_path, body.issue_ref)
    else:
        issue, ref = body.issue or "", body.issue_ref or "(inline)"

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


@app.get("/runs/{run_id}/artifacts")
async def artifacts(run_id: str) -> dict[str, Any]:
    """What the run produced: the patch, the PR summary, the test outcome.

    Deliberately not on the event stream. Events are published to Redis and
    LangSmith, so `MetricEvent.redacted()` strips diffs and file contents from
    them — which leaves a browser with no way to see the one thing the run
    exists to produce. This is that way: read once, by whoever holds the run id.
    """
    record = _manager().get(run_id)
    if record is None:
        raise HTTPException(404, f"unknown run {run_id}")
    # Null rather than 404 while a run is still working, or racing the moment
    # the phase flips to DONE before the artifacts are stashed.
    return {
        "run_id": run_id,
        "diff": record.diff,
        "pr_summary": record.pr_summary.model_dump() if record.pr_summary else None,
        "test_summary": record.test_summary,
    }


@app.post("/runs/{run_id}/publish")
async def publish(run_id: str, body: PublishRequest) -> dict[str, Any]:
    """The second human gate: this request *is* the approval to touch GitHub."""
    record = _manager().get(run_id)
    if record is None:
        raise HTTPException(404, f"unknown run {run_id}")
    if record.pr_url:
        return record.public()  # idempotent: the PR already exists
    if record.publishing:
        raise HTTPException(409, "publish already in flight")
    if record.phase is not RunPhase.DONE:
        raise HTTPException(409, f"run is {record.phase}, not done")
    if not record.publishable:
        raise HTTPException(
            409, "nothing to publish — run was not started from an issue URL, or produced no diff"
        )
    # Draft is a floor, not a preference, when the run publishes under this
    # server's identity rather than a visitor's own token.
    draft = _manager().effective_draft(record.credentials, requested_draft=body.draft)
    try:
        await _manager().publish(run_id, draft=draft)
    except (PublishError, GhError) as exc:
        raise HTTPException(502, f"publish failed: {exc}") from exc
    return record.public()


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
    return EventSourceResponse(
        _events(run_id),
        ping=int(KEEPALIVE_SECONDS),
        # sse-starlette already sends `X-Accel-Buffering: no`, which nginx
        # honours. Cloudflare does not: it buffers any response it might
        # compress, and an SSE response never completes, so the whole stream
        # is held. Deployed behind a tunnel this was 52 events on loopback and
        # zero through the edge — the page polled its status happily and never
        # drew the approval gate. `no-transform` is the request not to rewrite
        # the body, which is what turns the compression pass off.
        headers={"Cache-Control": "no-store, no-transform"},
    )


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
