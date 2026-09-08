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
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.syntax import Syntax
from rich.table import Table

from featurepilot.config import Role, get_settings
from featurepilot.contracts import HumanDecision, PRSummary
from featurepilot.github.client import GhError, ensure_gh_available
from featurepilot.github.clone import (
    ClonedRepo,
    clone_root_for,
    reattach_clone,
    shallow_clone,
)
from featurepilot.github.issues import IssueRef, fetch_issue, parse_issue_url
from featurepilot.github.publish import PublishError, branch_name, publish_run
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


def _issue_text(issue: str | None, github: int | None, github_repo: str) -> tuple[str, str]:
    """Resolve the issue body and a human-readable reference."""
    if github is not None:
        proc = subprocess.run(  # noqa: S603
            ["gh", "issue", "view", str(github), "--repo", github_repo, "--json", "title,body"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise typer.BadParameter(
                f"could not read issue #{github} from {github_repo}: {proc.stderr.strip()}"
            )
        data = json.loads(proc.stdout)
        return f"# {data['title']}\n\n{data['body']}", f"{github_repo}#{github}"

    if issue is None:
        raise typer.BadParameter("pass --issue <path> or --github <number>")
    path = Path(issue)
    if not path.is_file():
        raise typer.BadParameter(f"no such issue file: {issue}")
    return path.read_text(encoding="utf-8"), str(path)


@dataclass(frozen=True, slots=True)
class PublishSpec:
    """What the post-run publish step needs, decided before the run starts."""

    clone: ClonedRepo
    draft: bool
    skip_confirm: bool


def _github_token() -> str | None:
    token = get_settings().github_token
    return token.get_secret_value() if token else None


def _resolve_issue_url(target: str) -> IssueRef:
    try:
        return parse_issue_url(target)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _confirm_publish(spec: PublishSpec, pr: PRSummary) -> bool:
    """The second human gate: show exactly what would reach GitHub, then ask."""
    branch = branch_name(spec.clone.ref.number, pr.title)
    console.print(
        Panel(
            f"[bold]{pr.title}[/bold]\n\n{pr.body}\n\n"
            f"[dim]branch[/dim] {branch}  [dim]\u2192 fork \u2192 PR on[/dim] {spec.clone.ref.slug}"
            + ("  [yellow](draft)[/yellow]" if spec.draft else ""),
            title="Ready to publish",
            border_style="yellow",
        )
    )
    if spec.skip_confirm:
        return True
    if not _interactive():
        # Unattended, nobody approved this. Silence is not consent for a write
        # to someone else's repository — `--yes` is how consent is given.
        console.print(
            "[yellow]not published:[/yellow] no terminal to confirm from. "
            "Re-run with [bold]--yes[/bold] to publish unattended."
        )
        return False
    choice = Prompt.ask("Open this PR on GitHub?", choices=["y", "n"], default="n", console=console)
    return choice == "y"


async def _publish(handle: RunHandle, final: AgentState, spec: PublishSpec) -> int:
    """Fork, push, and open the PR for a DONE run. Returns an exit code."""
    pr = final.get("pr")
    code = final.get("code")
    if pr is None or code is None or not code.diff.strip():
        console.print("[red]nothing to publish:[/red] the run produced no diff or no PR summary")
        return 1
    if not _confirm_publish(spec, pr):
        console.print(
            f"[yellow]not published.[/yellow] diff and summary remain in "
            f"[dim].fp/runs/{handle.run_id}/[/dim]"
        )
        return 0
    try:
        result = await asyncio.to_thread(
            publish_run,
            spec.clone,
            code.diff,
            pr,
            token=_github_token(),
            draft=spec.draft,
        )
    except (PublishError, GhError) as exc:
        console.print(f"[red]publish failed:[/red] {exc}")
        console.print(
            f"[dim]the run itself succeeded; retry with: "
            f"fpilot resume {handle.run_id} <issue-url> --push[/dim]"
        )
        return 1
    verb = "updated" if result.reused_pr else "opened"
    console.print(
        Panel(
            f"[bold]{result.pr_url}[/bold]\n\n"
            f"[dim]branch[/dim] {result.branch}  [dim]on[/dim] {result.fork}  [dim]{verb}[/dim]",
            title="Pull request published",
            border_style="green",
        )
    )
    pr_url_file = Path(".fp") / "runs" / handle.run_id / "pr_url.txt"
    try:
        pr_url_file.parent.mkdir(parents=True, exist_ok=True)
        pr_url_file.write_text(result.pr_url + "\n", encoding="utf-8")
    except OSError:  # artifact write is best-effort; the URL is on screen
        pass
    return 0


def _interactive() -> bool:
    """Whether there is a person to ask. One function so both the gates and
    the tests agree on what "unattended" means."""
    return sys.stdin.isatty()


def _report_parked(handle: RunHandle, payload: dict[str, Any]) -> None:
    """Explain a park to a log file rather than to a person at a terminal."""
    console.print(
        Panel(
            f"[bold]{payload.get('summary', '')}[/bold]\n\n"
            + "\n".join(f"  - {q}" for q in payload.get("open_questions") or [])
            + "\n\n[dim]No terminal attached, so these cannot be answered here.[/dim]",
            title="Run needs answers",
            border_style="yellow",
        )
    )
    console.print(
        f"Answer them interactively with: [bold]fpilot resume {handle.run_id} <issue-url>[/bold]"
    )


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
    publish: PublishSpec | None = None,
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
            # `--yes` skips the gate only when the planner asked nothing
            # (planner.py:91); with questions outstanding the graph still parks.
            # Unattended — CI, a cron, a pipe — there is no stdin to read, and
            # reaching for one turns "needs a human" into an EOFError traceback.
            if not _interactive():
                _report_parked(handle, pending)
                return 2
            resume = _ask_approval(pending)

        final = await handle.state()
        _render_summary(handle, final)
        if final.get("phase") not in (RunPhase.DONE, RunPhase.FAILED):
            console.print(
                f"\n[yellow]Run parked.[/yellow] Continue with: "
                f"[bold]fpilot resume {handle.run_id} --issue <same issue>[/bold]"
            )
        if final.get("phase") is not RunPhase.DONE:
            return 1
        if publish is not None:
            return await _publish(handle, final, publish)
        return 0


@app.command()
def solve(
    target: str = typer.Argument(
        None, help="A public GitHub issue URL: clone the repo and solve it end to end."
    ),
    issue: str = typer.Option(None, "--issue", "-i", help="Path to an issue markdown file."),
    github: int = typer.Option(None, "--github", "-g", help="Issue number on --github-repo."),
    github_repo: str = typer.Option(
        "Ekam-Bitt/featurepilot-fixture", "--github-repo", help="Repo --github numbers refer to."
    ),
    repo: Path = typer.Option(
        Path("fixtures/target-repo"), "--repo", "-r", help="Repository to work on."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the approval gates (open questions still stop)."
    ),
    push: bool = typer.Option(
        False, "--push", help="After a successful run: fork, push, and open a PR (asks first)."
    ),
    draft: bool = typer.Option(False, "--draft", help="Open the PR as a draft."),
    install: bool = typer.Option(
        True, "--install/--no-install", help="Install the target repo's dependencies."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show library logging."),
) -> None:
    """Solve an issue: plan, patch, test, repair, summarise — and optionally publish."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    publish_spec: PublishSpec | None = None
    run_id: str | None = None
    if target is not None:
        ref = _resolve_issue_url(target)
        token = _github_token()
        try:
            # Fail before any tokens are spent, not after the run.
            ensure_gh_available(token=token)
            fetched = fetch_issue(ref, token=token)
        except GhError as exc:
            raise typer.BadParameter(str(exc)) from exc
        run_id = uuid.uuid4().hex[:12]
        console.print(f"[dim]cloning {ref.slug} (depth 1)\u2026[/dim]")
        clone = shallow_clone(ref, clone_root_for(run_id), token=token)
        body, ref_str = fetched.text, f"{ref.slug}#{ref.number}"
        repo = clone.path
        if push:
            publish_spec = PublishSpec(clone=clone, draft=draft, skip_confirm=yes)
    else:
        if push:
            raise typer.BadParameter(
                "--push needs an issue URL target: publishing requires an upstream repo to PR"
            )
        body, ref_str = _issue_text(issue, github, github_repo)
    raise typer.Exit(
        asyncio.run(
            _solve(
                repo,
                body,
                ref_str,
                auto_approve=yes,
                install=install,
                run_id=run_id,
                publish=publish_spec,
            )
        )
    )


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id printed when the run parked."),
    target: str = typer.Argument(None, help="The same issue URL, for a URL-started run."),
    issue: str = typer.Option(None, "--issue", "-i", help="The same issue file."),
    github: int = typer.Option(None, "--github", "-g", help="The same GitHub issue."),
    github_repo: str = typer.Option(
        "Ekam-Bitt/featurepilot-fixture", "--github-repo", help="Repo --github numbers refer to."
    ),
    repo: Path = typer.Option(Path("fixtures/target-repo"), "--repo", "-r"),
    push: bool = typer.Option(
        False, "--push", help="Publish after the resumed run completes (asks first)."
    ),
    draft: bool = typer.Option(False, "--draft", help="Open the PR as a draft."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the publish confirmation."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Continue a run that parked on approval or was killed mid-flight.

    Reattaches to the sandbox the earlier process left behind and picks the graph
    up from its Postgres checkpoint, so the agent's edits and the installed
    dependencies survive. For a URL-started run the clone under .fp/clones is
    reattached too, so --push still works.
    """
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    publish_spec: PublishSpec | None = None
    if target is not None:
        ref_ = _resolve_issue_url(target)
        token = _github_token()
        try:
            fetched = fetch_issue(ref_, token=token)
        except GhError as exc:
            raise typer.BadParameter(str(exc)) from exc
        clone = reattach_clone(run_id, ref_)
        if clone is None:
            raise typer.BadParameter(
                f"no clone for run {run_id} under .fp/clones — was this run started from a URL?"
            )
        body, ref = fetched.text, f"{ref_.slug}#{ref_.number}"
        repo = clone.path
        if push:
            publish_spec = PublishSpec(clone=clone, draft=draft, skip_confirm=yes)
    else:
        if push:
            raise typer.BadParameter("--push needs the issue URL argument")
        body, ref = _issue_text(issue, github, github_repo)
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
                publish=publish_spec,
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

    # gh carries the whole publish path (issue fetch, fork, push, PR). Without
    # it runs still work; the diff just stops at the terminal.
    try:
        ensure_gh_available(token=_github_token())
        detail = "GITHUB_TOKEN" if settings.github_token else "ambient gh auth"
        table.add_row("gh", "[green]ok[/green]", f"publishing available ({detail})")
    except GhError as exc:
        table.add_row("gh", "[yellow]absent[/yellow]", f"no PR publishing — {str(exc)[:44]}")

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
