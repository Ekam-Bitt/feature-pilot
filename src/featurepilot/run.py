"""Run orchestration.

Assembles a run and streams it. The order here is load-bearing:

1. sandbox up, dependencies installed **with** network
2. network cut — everything the agent does from here has no egress
3. snapshot taken, so the repair loop has a clean baseline to return to
4. MCP servers spawned and tools discovered dynamically
5. graph compiled against the Postgres checkpointer
6. streamed, with interrupts surfacing to the caller

Steps 1–3 cannot be reordered: installing after the cut fails, and snapshotting
before the install would make `restore()` delete the dependencies.
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from featurepilot.config import Role, Settings, get_settings
from featurepilot.contracts import HumanDecision
from featurepilot.graph.build import build_graph
from featurepilot.graph.context import RunContext
from featurepilot.graph.state import AgentState, new_state
from featurepilot.lifecycle import RunPhase
from featurepilot.mcp.client import MCPToolLoader
from featurepilot.metrics.events import CompositeSink, EventSink, InMemorySink
from featurepilot.metrics.recorder import MetricsRecorder
from featurepilot.retrieval.base import Retriever
from featurepilot.retrieval.filesystem import FilesystemRetriever
from featurepilot.sandbox.runner import Sandbox
from featurepilot.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

#: Run artifacts live here. Gitignored; inspecting a run should never need a
#: database to be up.
ARTIFACT_ROOT = Path(".fp")


def _make_retriever(kind: str, registry: ToolRegistry, settings: Settings) -> Retriever:
    """Select the retrieval strategy by config.

    This function is the entirety of the Phase 1A -> 1B switch. The graph is
    retrieval-agnostic, so adding `embedding`/`hybrid` here changes no node.
    """
    if kind == "filesystem":
        return FilesystemRetriever(registry)
    raise NotImplementedError(
        f"retriever {kind!r} arrives in Phase 1B; set FP_RETRIEVER=filesystem for now"
    )


@dataclass(slots=True)
class RunHandle:
    """Everything a caller needs to drive and inspect one run."""

    run_id: str
    thread_id: str
    graph: Any
    ctx: RunContext
    sink: InMemorySink
    #: Tests already failing before any edit. Lets the tester distinguish
    #: "the patch broke this" from "this repository was already broken".
    baseline_failures: tuple[str, ...] = ()
    #: Tests collected at baseline, so a shrinking suite is detectable.
    baseline_total: int = 0
    #: Updated as the run streams. Teardown reads this rather than querying the
    #: checkpointer: the exit stack unwinds LIFO, so by teardown time the
    #: checkpointer connection is already closed and any query fails.
    last_phase: RunPhase | None = None

    @property
    def config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}}

    async def state(self) -> AgentState:
        snapshot = await self.graph.aget_state(self.config)
        return dict(snapshot.values)  # type: ignore[return-value]

    async def pending_interrupt(self) -> dict[str, Any] | None:
        """The payload the graph is parked on, if any.

        This is how the API answers "what is this run waiting for" after a
        restart, without keeping anything in memory.
        """
        snapshot = await self.graph.aget_state(self.config)
        for task in snapshot.tasks:
            for interrupt in getattr(task, "interrupts", ()) or ():
                value = getattr(interrupt, "value", None)
                if isinstance(value, dict):
                    return value
        return None


@contextlib.asynccontextmanager
async def open_run(
    repo_path: Path,
    issue: str,
    *,
    issue_ref: str = "",
    settings: Settings | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    auto_approve: bool = False,
    extra_sinks: tuple[EventSink, ...] = (),
    install_dependencies: bool = True,
    resume: bool = False,
) -> AsyncIterator[RunHandle]:
    """Provision a run and yield a handle. Tears everything down on exit.

    `resume=True` attaches to the container an earlier process left behind
    instead of building a fresh one, so the agent's edits and the installed
    dependencies are still there. The graph state comes from the Postgres
    checkpoint; together they make a killed run genuinely continuable rather
    than merely restartable.
    """
    settings = settings or get_settings()
    # Tracing is configured once per run, before any model call.
    from featurepilot.llm import configure_tracing

    configure_tracing(settings)
    run_id = run_id or uuid.uuid4().hex[:12]
    thread_id = thread_id or run_id

    memory = InMemorySink()
    durable = await _durable_sinks(settings, run_id)
    sink = CompositeSink([memory, *durable, *extra_sinks])
    recorder = MetricsRecorder(run_id=run_id, sink=sink, settings=settings)

    sandbox = Sandbox(repo_path, settings=settings, run_id=run_id)
    loader = MCPToolLoader(run_id)
    handle: RunHandle | None = None

    async with contextlib.AsyncExitStack() as stack:
        # Registered first so it runs last: traces are flushed after every
        # other teardown has finished emitting.
        stack.callback(_flush_traces)
        stack.push_async_callback(sink.aclose)
        if resume:
            sandbox = await Sandbox.attach(run_id, settings=settings, repo_path=repo_path)
            log.info("attached to the sandbox left by run %s", run_id)
        else:
            await sandbox.start()
        # Conditional teardown: a run parked on a human — or killed mid-repair —
        # must keep its container, or resuming would restore the graph state and
        # find the agent's edits gone. Orphans are bounded by the reaper.
        stack.push_async_callback(lambda: _teardown(sandbox, handle))

        if install_dependencies and not resume:
            install = await sandbox.install_dependencies()
            if not install.ok:
                # Not fatal: some repos need no install step, and a test run will
                # fail loudly soon enough if the environment is genuinely broken.
                log.warning("dependency install reported failure: %s", install.combined[-800:])

        if not resume:
            await sandbox.cut_network()
            await sandbox.snapshot()

        # One test run against untouched code. Costs a few seconds and is what
        # makes "did this patch help" answerable on a repository that already had
        # failures — which the fixture does, deliberately, and real repos usually
        # do by accident.
        # On resume the baseline is already in the checkpoint; re-measuring it
        # against a half-patched tree would produce a wrong one.
        baseline: tuple[str, ...] = ()
        baseline_total = 0
        if not resume:
            baseline, baseline_total = await _baseline(sandbox)
            log.info(
                "baseline: %d of %d test(s) already failing before any edit",
                len(baseline),
                baseline_total,
            )

        await loader.connect()
        stack.push_async_callback(loader.aclose)
        registry = await loader.discover()

        retriever = _make_retriever(settings.retriever, registry, settings)
        await retriever.prepare()

        ctx = RunContext(
            settings=settings,
            registry=registry,
            retriever=retriever,
            recorder=recorder,
            call=_structured_call(settings, recorder),
            tool_loop=_tool_loop_call(settings, recorder),
            sandbox=sandbox,
            auto_approve=auto_approve,
        )

        checkpointer = await stack.enter_async_context(_checkpointer(settings))
        graph = build_graph(ctx).compile(checkpointer=checkpointer)

        await recorder.run_started(repo=str(repo_path), issue_ref=issue_ref or "(inline)")
        handle = RunHandle(
            run_id=run_id,
            thread_id=thread_id,
            graph=graph,
            ctx=ctx,
            sink=memory,
            baseline_failures=baseline,
            baseline_total=baseline_total,
        )
        yield handle


def _flush_traces() -> None:
    """Drain the trace upload queue before the process can exit."""
    from featurepilot.tracing import flush

    flush()


async def _teardown(sandbox: Sandbox, handle: RunHandle | None) -> None:
    """Destroy the sandbox only when the run is genuinely over.

    A run waiting on plan approval, or one killed mid-repair, is resumable — but
    only if its container still exists. Keeping it is what makes `--resume` mean
    "carry on" rather than "start again with a fresh checkout".
    """
    if handle is None or handle.last_phase is None:
        # Never got far enough to know; do not strand a container.
        await sandbox.destroy()
        return

    phase = handle.last_phase
    if phase in (RunPhase.DONE, RunPhase.FAILED):
        await sandbox.destroy()
    else:
        log.info(
            "run %s left at %s; keeping sandbox %s so it can be resumed "
            "(reaped automatically after %ds)",
            handle.run_id,
            phase.value,
            sandbox.run_id,
            sandbox.settings.sandbox_reap_after_s,
        )


async def _durable_sinks(settings: Settings, run_id: str) -> list[EventSink]:
    """Postgres, Redis and on-disk artifacts, each optional.

    Every one degrades to a no-op when its backing service is absent, so the
    "works with only ANTHROPIC_API_KEY" contract holds — you simply lose the
    dashboard and the live stream, not the run.
    """
    from featurepilot.metrics.sinks import FileArtifactSink, PostgresSink, RedisSink

    sinks: list[EventSink] = [FileArtifactSink(ARTIFACT_ROOT, run_id)]
    sinks.append(await PostgresSink(settings.postgres_dsn).start())
    sinks.append(await RedisSink(settings.redis_url, run_id).start())
    return sinks


async def _baseline(sandbox: Sandbox) -> tuple[tuple[str, ...], int]:
    """Failing test IDs and the total collected, before the agent touches anything.

    The total matters as much as the failures: deleting a passing test is a way to
    make a suite green that no pass/fail comparison can detect.

    Uses the trusted shell path rather than `exec`: this is Feature Pilot running
    its own command, not the agent choosing one.
    """
    from featurepilot.graph.nodes.tester import TEST_COMMAND, collected_total, failing_ids

    result = await sandbox._exec_shell(TEST_COMMAND, timeout=600)
    return tuple(sorted(failing_ids(result.combined))), collected_total(result.combined)


@contextlib.asynccontextmanager
async def _checkpointer(settings: Settings) -> AsyncIterator[Any]:
    """Postgres when reachable, in-memory otherwise.

    Falling back rather than failing keeps a demo runnable when only Docker is
    up, but it is logged loudly: without Postgres a killed run cannot be resumed,
    which is one of the properties this project claims.

    **Only connection setup is guarded.** An earlier version wrapped the `yield`
    in the same `try`, so any exception raised anywhere in the run — a model
    returning a malformed contract, for instance — was caught here, reported as
    "postgres unavailable", and then the generator yielded a second time and died
    with "generator didn't stop after athrow()". The real error never surfaced.
    Connection failures and run failures are different things and must not share
    a handler.
    """
    manager: Any = None
    saver: Any
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        manager = AsyncPostgresSaver.from_conn_string(settings.postgres_dsn, serde=_serde())
        saver = await manager.__aenter__()
        await saver.setup()
        log.info("checkpointing to postgres")
    except Exception as exc:  # noqa: BLE001 - any connection problem degrades the same way
        log.warning(
            "postgres checkpointer unavailable (%s); using in-memory checkpoints. "
            "Resume across restarts will not work. Run `docker compose up -d`.",
            exc,
        )
        manager = None
        saver = InMemorySaver(serde=_serde())

    try:
        # Outside the guard: exceptions from the run propagate untouched.
        yield saver
    finally:
        if manager is not None:
            await manager.__aexit__(None, None, None)


def _serde() -> Any:
    """Allow our own Pydantic contracts through msgpack.

    State holds `PlannerOutput`, `CoderOutput` and friends. LangGraph warns on
    deserialising unregistered types today and will block them in a future
    version, so declare them rather than depending on a deprecation window.
    """
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    from featurepilot import contracts
    from featurepilot.lifecycle import RunPhase as _RunPhase

    allowed = [
        # The state's phase enum. Without it a resumed run gets a plain str
        # back, and every identity check in the router silently stops matching.
        _RunPhase,
        contracts.PlannerOutput,
        contracts.RetrieverOutput,
        contracts.RetrievedChunk,
        contracts.PlanStep,
        contracts.CoderOutput,
        contracts.FileEdit,
        contracts.CriticOutput,
        contracts.TesterOutput,
        contracts.FailingTest,
        contracts.DebuggerOutput,
        contracts.ReviewerOutput,
        contracts.PRSummary,
        contracts.HumanDecision,
    ]
    return JsonPlusSerializer(allowed_msgpack_modules=allowed)


def _structured_call(settings: Settings, recorder: MetricsRecorder) -> Any:
    from featurepilot import llm

    async def call(role: Role, output_model: Any, messages: Any, *, escalate: bool = False) -> Any:
        return await llm.call_structured(
            role, output_model, messages, settings=settings, recorder=recorder, escalate=escalate
        )

    return call


def _tool_loop_call(settings: Settings, recorder: MetricsRecorder) -> Any:
    from featurepilot import llm

    async def loop(
        role: Role,
        messages: Any,
        registry: ToolRegistry,
        *,
        escalate: bool = False,
        max_iterations: int = 12,
    ) -> Any:
        return await llm.run_tool_loop(
            role,
            messages,
            registry,
            settings=settings,
            recorder=recorder,
            escalate=escalate,
            max_iterations=max_iterations,
        )

    return loop


async def stream_run(
    handle: RunHandle,
    *,
    issue: str,
    repo_path: Path,
    issue_ref: str = "",
    resume: HumanDecision | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Advance the run, yielding one dict per node completion.

    `resume` answers a pending interrupt. Passing it means the graph continues
    from the checkpoint rather than starting over.
    """
    payload: Any
    if resume is not None:
        payload = Command(resume=resume)
    else:
        payload = new_state(
            run_id=handle.run_id,
            repo_path=str(repo_path),
            issue=issue,
            issue_ref=issue_ref,
        )
        payload["baseline_failures"] = list(handle.baseline_failures)
        payload["baseline_total"] = handle.baseline_total

    async for chunk in handle.graph.astream(payload, handle.config, stream_mode="updates"):
        for node_name, update in chunk.items():
            if isinstance(update, dict) and (phase := update.get("phase")) is not None:
                # Cached for teardown, which cannot query the checkpointer.
                # Coerced because a resumed run's phase arrives as a plain string.
                handle.last_phase = RunPhase(str(phase))
            yield {"node": node_name, "update": update}
