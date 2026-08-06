"""Feature Pilot CLI.

Renders the run as it happens and handles the approval gate inline. Deliberately
thin: it consumes `featurepilot.run`, exactly as the Phase 2 web UI will consume
the same stream over SSE, so neither surface owns behaviour the other lacks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table

from featurepilot.config import Role, get_settings
from featurepilot.contracts import HumanDecision
from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase
from featurepilot.run import RunHandle, open_run, stream_run

app = typer.Typer(
    add_completion=False,
    help="Feature Pilot: turn a GitHub issue into a tested patch.",
    no_args_is_help=True,
)
console = Console()

#: What each node is doing, in words a human reading a terminal wants.
NODE_LABEL = {
    "retrieve": "Searching the repository",
    "plan": "Planning the change",
    "approval": "Waiting for your approval",
    "code": "Writing code",
    "test": "Running the test suite",
    "debug": "Diagnosing the failure",
    "review": "Reviewing the patch",
    "summarize": "Writing the PR summary",
}


def _issue_text(issue: str | None, github: int | None) -> tuple[str, str]:
    """Resolve the issue body and a human-readable reference."""
    if github is not None:
        from eval.dataset import GITHUB_REPO

        proc = subprocess.run(  # noqa: S603
            ["gh", "issue", "view", str(github), "--repo", GITHUB_REPO, "--json", "title,body"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise typer.BadParameter(
                f"could not read issue #{github} from {GITHUB_REPO}: {proc.stderr.strip()}"
            )
        data = json.loads(proc.stdout)
        return f"# {data['title']}\n\n{data['body']}", f"{GITHUB_REPO}#{github}"

    if issue is None:
        raise typer.BadParameter("pass --issue <path> or --github <number>")
    path = Path(issue)
    if not path.is_file():
        raise typer.BadParameter(f"no such issue file: {issue}")
    return path.read_text(encoding="utf-8"), str(path)


def _render_plan(payload: dict[str, Any]) -> None:
    body = [f"[bold]{payload.get('summary', '')}[/bold]", ""]
    for i, step in enumerate(payload.get("steps") or [], start=1):
        files = step.get("files") or []
        suffix = f"  [dim]{', '.join(files)}[/dim]" if files else ""
        body.append(f"  {i}. {step.get('description', '')}{suffix}")
    if questions := payload.get("open_questions"):
        body += ["", "[yellow]Questions that need your answer:[/yellow]"]
        body += [f"  - {q}" for q in questions]
    confidence = payload.get("confidence", "unknown")
    body += ["", f"[dim]confidence: {confidence}[/dim]"]
    console.print(Panel("\n".join(body), title="Plan", border_style="cyan"))


def _ask_approval(payload: dict[str, Any]) -> HumanDecision:
    _render_plan(payload)
    answers: list[str] = []
    for question in payload.get("open_questions") or []:
        answers.append(Prompt.ask(f"[yellow]{question}[/yellow]", default=""))

    choice = Prompt.ask("Approve this plan?", choices=["y", "n"], default="y", console=console)
    if choice == "y":
        return HumanDecision(verdict="approve", answers=answers)
    feedback = Prompt.ask("What should change?", default="", console=console)
    return HumanDecision(verdict="reject", feedback=feedback, answers=answers)


def _render_update(node: str, update: dict[str, Any]) -> None:
    label = NODE_LABEL.get(node, node)
    console.print(f"[dim]·[/dim] {label}")

    if (tests := update.get("tests")) is not None:
        colour = "green" if tests.success else "red"
        console.print(f"  [{colour}]{tests.passed} passed, {tests.failed} failed[/{colour}]")
        if tests.baseline_known:
            # The counts alone are misleading on a repo with pre-existing
            # failures; the deltas are what the run turns on.
            if tests.resolved:
                console.print(f"    [green]fixed {len(tests.resolved)}[/green] previously failing")
            if tests.regressions:
                console.print(f"    [red]broke {len(tests.regressions)}[/red]:")
                for tid in tests.regressions[:5]:
                    console.print(f"      [red]{tid}[/red]")
            if tests.pre_existing:
                console.print(
                    f"    [dim]{len(tests.pre_existing)} already failing before this "
                    "patch (out of scope)[/dim]"
                )
        else:
            for failure in tests.failing_tests[:5]:
                console.print(f"    [red]FAILED[/red] {failure.test_id}")
                if failure.message:
                    console.print(f"      [dim]{failure.message[:160]}[/dim]")

    if (code := update.get("code")) is not None and code.diff:
        console.print(Syntax(code.diff, "diff", theme="ansi_dark", word_wrap=True))
        for assumption in code.assumptions:
            console.print(f"  [yellow]assumption:[/yellow] {assumption}")

    if (diagnosis := update.get("diagnosis")) is not None:
        console.print(f"  [magenta]{diagnosis.failure_category}[/magenta]: {diagnosis.root_cause}")
        console.print(f"  [dim]retry: {diagnosis.retry}[/dim]")

    if (review := update.get("review")) is not None:
        colour = "green" if review.verdict == "approve" else "red"
        console.print(f"  [{colour}]review: {review.verdict}[/{colour}]")
        for item in review.blocking:
            console.print(f"    [red]blocking:[/red] {item}")
        for reason in review.reasons[:5]:
            console.print(f"    [dim]{reason}[/dim]")

    if (pr := update.get("pr")) is not None:
        console.print(
            Panel(
                f"[bold]{pr.title}[/bold]\n\n{pr.body}\n\n[dim]Test plan:[/dim] {pr.test_plan}",
                title="Pull request",
                border_style="green",
            )
        )

    if error := update.get("error"):
        console.print(f"  [red]{error}[/red]")


def _render_summary(handle: RunHandle, final: AgentState) -> None:
    totals = handle.ctx.recorder.totals
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row("outcome", str(final.get("phase", "?")))
    table.add_row("attempts", str(final.get("attempt", 0)))
    table.add_row("model calls", str(totals.model_calls))
    table.add_row("tool calls", str(totals.tool_calls))
    table.add_row("tokens", f"{totals.input_tokens} in / {totals.output_tokens} out")
    table.add_row("cost", f"${totals.cost_usd:.4f}")
    if totals.total_refs:
        table.add_row("nonexistent refs", f"{totals.nonexistent_ref_rate:.1%}")
    console.print(Panel(table, title="Run", border_style="dim"))


async def _solve(
    repo: Path,
    issue_body: str,
    issue_ref: str,
    *,
    auto_approve: bool,
    install: bool,
    run_id: str | None = None,
    resuming: bool = False,
) -> int:
    settings = get_settings()
    async with open_run(
        repo,
        issue_body,
        issue_ref=issue_ref,
        settings=settings,
        auto_approve=auto_approve,
        install_dependencies=install,
        run_id=run_id,
        resume=resuming,
    ) as handle:
        console.print(
            f"[bold]Feature Pilot[/bold] run [cyan]{handle.run_id}[/cyan] "
            f"on [dim]{repo}[/dim]  ·  {issue_ref}"
        )
        console.print(
            f"[dim]{len(handle.ctx.registry)} tools discovered over MCP: "
            f"{', '.join(handle.ctx.registry.names())}[/dim]\n"
        )

        # Resuming a parked run answers its pending interrupt rather than
        # starting the graph over from the issue.
        resume: HumanDecision | None = None
        if resuming:
            pending = await handle.pending_interrupt()
            if pending is None:
                console.print("[yellow]nothing to resume — that run is not parked.[/yellow]")
                return 1
            resume = _ask_approval(pending)

        # Each pass runs until the graph completes or parks on an interrupt.
        while True:
            async for event in stream_run(
                handle,
                issue=issue_body,
                repo_path=repo,
                issue_ref=issue_ref,
                resume=resume,
            ):
                node = event["node"]
                if node == "__interrupt__":
                    continue
                _render_update(node, event["update"] or {})

            pending = await handle.pending_interrupt()
            if pending is None:
                break
            resume = _ask_approval(pending)

        final = await handle.state()
        _render_summary(handle, final)
        if final.get("phase") not in (RunPhase.DONE, RunPhase.FAILED):
            console.print(
                f"\n[yellow]Run parked.[/yellow] Continue with: "
                f"[bold]fpilot resume {handle.run_id} --issue <same issue>[/bold]"
            )
        return 0 if final.get("phase") is RunPhase.DONE else 1


@app.command()
def solve(
    issue: str = typer.Option(None, "--issue", "-i", help="Path to an issue markdown file."),
    github: int = typer.Option(None, "--github", "-g", help="Issue number on the fixture repo."),
    repo: Path = typer.Option(
        Path("fixtures/target-repo"), "--repo", "-r", help="Repository to work on."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the approval gate (open questions still stop)."
    ),
    install: bool = typer.Option(
        True, "--install/--no-install", help="Install the target repo's dependencies."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show library logging."),
) -> None:
    """Solve an issue: plan, patch, test, repair, and summarise."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    body, ref = _issue_text(issue, github)
    raise typer.Exit(asyncio.run(_solve(repo, body, ref, auto_approve=yes, install=install)))


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id printed when the run parked."),
    issue: str = typer.Option(None, "--issue", "-i", help="The same issue file."),
    github: int = typer.Option(None, "--github", "-g", help="The same GitHub issue."),
    repo: Path = typer.Option(Path("fixtures/target-repo"), "--repo", "-r"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Continue a run that parked on approval or was killed mid-flight.

    Reattaches to the sandbox the earlier process left behind and picks the graph
    up from its Postgres checkpoint, so the agent's edits and the installed
    dependencies survive.
    """
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    body, ref = _issue_text(issue, github)
    raise typer.Exit(
        asyncio.run(
            _solve(
                repo,
                body,
                ref,
                auto_approve=False,
                install=False,
                run_id=run_id,
                resuming=True,
            )
        )
    )


@app.command()
def serve(
    host: str = typer.Option(None, "--host", help="Defaults to FP_API_HOST."),
    port: int = typer.Option(None, "--port", help="Defaults to FP_API_PORT."),
) -> None:
    """Serve the HTTP API and its SSE stream.

    The same run pipeline the CLI drives, exposed over HTTP — so the Phase 2 web
    UI consumes exactly what the terminal does.
    """
    import uvicorn

    settings = get_settings()
    console.print(
        f"[bold]Feature Pilot API[/bold] on "
        f"http://{host or settings.api_host}:{port or settings.api_port}  "
        f"[dim](docs at /docs)[/dim]"
    )
    uvicorn.run(
        "featurepilot.api.main:app",
        host=host or settings.api_host,
        port=port or settings.api_port,
    )


@app.command()
def doctor() -> None:
    """Check that everything a run needs is present."""
    table = Table("check", "status", "detail")

    settings = get_settings()
    table.add_row(
        "anthropic key",
        "[green]ok[/green]" if settings.anthropic_api_key else "[red]missing[/red]",
        "ANTHROPIC_API_KEY",
    )

    try:
        import docker

        version = docker.from_env().version()["Version"]
        table.add_row("docker", "[green]ok[/green]", f"engine {version}")
    except Exception as exc:  # noqa: BLE001
        table.add_row("docker", "[red]unreachable[/red]", str(exc)[:60])

    try:
        import psycopg

        with psycopg.connect(settings.postgres_dsn, connect_timeout=3):
            table.add_row("postgres", "[green]ok[/green]", "checkpoints will persist")
    except Exception as exc:  # noqa: BLE001
        table.add_row("postgres", "[yellow]absent[/yellow]", f"resume disabled — {str(exc)[:44]}")

    # Redis is easy to omit here and shouldn't be: it carries the API's SSE
    # events, and `manager.subscribe` returns None when it is down rather than
    # raising. A stream then degrades to silence, so without this row a user with
    # Redis stopped gets an empty event feed and a clean bill of health.
    try:
        import redis

        redis.from_url(settings.redis_url, socket_connect_timeout=3).ping()
        table.add_row("redis", "[green]ok[/green]", "API event stream will deliver")
    except Exception as exc:  # noqa: BLE001
        table.add_row("redis", "[yellow]absent[/yellow]", f"SSE silent — {str(exc)[:48]}")

    for role in (Role.PLANNER, Role.CODER, Role.REVIEWER):
        table.add_row(f"model:{role}", "[green]ok[/green]", settings.model_for(role))

    table.add_row("retriever", "[green]ok[/green]", settings.retriever)
    table.add_row(
        "tracing",
        "[green]on[/green]" if settings.tracing_enabled else "[dim]off[/dim]",
        settings.langsmith_project if settings.tracing_enabled else "no LangSmith key",
    )
    console.print(table)


@app.command()
def reap(older_than: int = typer.Option(0, "--older-than", help="Seconds. 0 removes all.")) -> None:
    """Remove leftover sandbox containers from crashed runs."""
    from featurepilot.sandbox.runner import Sandbox

    removed = asyncio.run(Sandbox.reap_stale(older_than))
    if removed:
        console.print(f"removed {len(removed)} container(s):")
        for name in removed:
            console.print(f"  [dim]{name}[/dim]")
    else:
        console.print("[dim]nothing to reap[/dim]")


if __name__ == "__main__":
    app()
