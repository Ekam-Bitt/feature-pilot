"""Model access, via LiteLLM.

Nodes ask for a *role* ("planner", "coder"), never a model string. Re-tiering for
cost is then one edit in config.py, and switching provider — Anthropic to Ollama
to OpenAI — is a value change with no code change anywhere.

Structured outputs are the default path: every node returns a Pydantic contract,
so the model is constrained to a schema rather than asked to produce parseable
prose.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any, cast

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage
from pydantic import BaseModel

from featurepilot.config import Role, Settings, get_settings
from featurepilot.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from featurepilot.metrics.recorder import MetricsRecorder

log = logging.getLogger(__name__)

#: Structured-output attempts before giving up. Three rather than two because
#: the first retry sometimes repeats the same malformed shape, and a third with
#: the schema spelled out usually lands.
MAX_STRUCTURED_ATTEMPTS = 3

#: Character budget for tool results carried in a tool-loop transcript.
#:
#: The loop re-sends the whole history each turn, so the transcript is paid for
#: once per remaining iteration — it is the largest per-call component, not a
#: one-off. Measured on click: a 60k budget put ~21k tokens on every one of 19
#: calls. Older results are elided once the budget is passed; the model has
#: already acted on them, and the recent ones are what it is reasoning about.
#:
#: Do not shrink this expecting a saving. Measured on click: cutting it to 18k
#: took per-call size down but pushed call count from 19 to 39 and total tokens
#: from 406k to 680k — with less state in hand the agent re-explores. Per-call
#: context and iteration count trade against each other.
TOOL_TRANSCRIPT_BUDGET = 50_000

#: Never elide the most recent results: they are the ones being reasoned about.
KEEP_RECENT_TOOL_RESULTS = 4


def _prune_tool_results(history: list[BaseMessage]) -> list[BaseMessage]:
    """Replace the content of older tool results once the transcript grows too big.

    Structure is preserved rather than dropping messages: a ToolMessage whose
    matching tool_call disappears is rejected by the provider, so the message
    stays and only its body is replaced by a stub.
    """
    from langchain_core.messages import ToolMessage

    indices = [i for i, m in enumerate(history) if isinstance(m, ToolMessage)]
    if len(indices) <= KEEP_RECENT_TOOL_RESULTS:
        return history

    total = sum(len(str(history[i].content)) for i in indices)
    if total <= TOOL_TRANSCRIPT_BUDGET:
        return history

    prunable = indices[:-KEEP_RECENT_TOOL_RESULTS]
    pruned = list(history)
    for i in prunable:
        original = str(pruned[i].content)
        if len(original) <= 200:
            continue
        message = pruned[i]
        pruned[i] = ToolMessage(
            content=(
                f"[earlier result elided to stay within budget — {len(original)} "
                "characters. Re-read a narrow range if you still need it.]"
            ),
            tool_call_id=getattr(message, "tool_call_id", ""),
            status=getattr(message, "status", "success"),
        )
        total -= len(original)
        if total <= TOOL_TRANSCRIPT_BUDGET:
            break
    return pruned


def _repair_prompt(output_model: type[BaseModel], error: Exception | None) -> str:
    """Tell the model exactly what shape was expected and what it got wrong."""
    try:
        schema = json.dumps(output_model.model_json_schema(), indent=2)[:4000]
    except Exception:  # noqa: BLE001 - schema generation must not mask the real error
        schema = f"(schema for {output_model.__name__} unavailable)"
    return (
        f"Your previous response could not be parsed as {output_model.__name__}.\n\n"
        f"Validation error:\n{error}\n\n"
        f"Required JSON schema:\n{schema}\n\n"
        "Return a single JSON object matching that schema exactly. Every array "
        "field must be a JSON array, not a string. Do not include XML tags, "
        "tool-call markup, prose, or markdown fences."
    )


def configure_tracing(settings: Settings) -> bool:
    """Turn on LangSmith tracing if a key is configured.

    LangGraph and LangChain instrument themselves from environment variables, so
    every node, tool call and token count becomes a trace with no code changes —
    which is why this is three env vars rather than a tracing layer.

    Returns whether tracing is on. Absent a key it is a silent no-op, keeping the
    "works with only ANTHROPIC_API_KEY" contract.
    """
    if settings.langsmith_api_key is None:
        # Explicitly off: a stale LANGSMITH_TRACING in the shell would otherwise
        # make every call try to reach a project we have no key for, and each one
        # pays a connection timeout.
        os.environ["LANGSMITH_TRACING"] = "false"
        return False

    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    if settings.langsmith_workspace_id:
        # Required when the key is org-scoped rather than personal; without it
        # the API cannot tell which workspace the traces belong to.
        os.environ["LANGSMITH_WORKSPACE_ID"] = settings.langsmith_workspace_id
    log.info("langsmith tracing enabled for project %r", settings.langsmith_project)
    return True


def _export_provider_keys(settings: Settings) -> None:
    """LiteLLM reads provider credentials from the environment. Export from
    Settings so there is exactly one source of truth (.env via pydantic) rather
    than two ways to configure a key.
    """
    if settings.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key.get_secret_value()


def chat_model(
    role: Role,
    *,
    settings: Settings | None = None,
    escalate: bool = False,
    max_tokens: int | None = None,
) -> BaseChatModel:
    """Build the chat model for `role`.

    `escalate=True` swaps in the escalation model. Used only on a final retry,
    where another failed attempt costs more than a better model would.
    """
    from langchain_litellm import ChatLiteLLM

    settings = settings or get_settings()
    _export_provider_keys(settings)

    model = settings.model_escalation if escalate else settings.model_for(role)
    return ChatLiteLLM(
        model=model,
        max_tokens=max_tokens or settings.max_tokens_per_call,
        # An explicit deadline and retry count. Without them a large request can
        # hang until some library default fires — observed on click: 817 seconds
        # of wall clock, zero completed calls, nothing to show for it.
        request_timeout=settings.request_timeout_s,
        max_retries=settings.request_max_retries,
        # Sampling params are rejected on current Anthropic models; steer with
        # prompting instead. Left unset deliberately.
    )


async def call_structured[T: BaseModel](
    role: Role,
    output_model: type[T],
    messages: list[BaseMessage],
    *,
    settings: Settings | None = None,
    recorder: MetricsRecorder | None = None,
    escalate: bool = False,
) -> T:
    """Invoke `role`'s model constrained to `output_model`.

    Checks the budget before spending and records usage after, so accounting and
    the run ceiling are enforced in one place rather than per node.

    Retries on a validation failure, showing the model the schema and the exact
    error. Observed failure worth defending against: a model occasionally emits
    tool-call markup (`<parameter name="...">`) into a field expecting a list,
    and a bare "that did not parse" is not enough to correct it — the retry has to
    say what shape was wanted.

    Three attempts, then fail the run cleanly rather than propagating a
    half-parsed object into routing.
    """
    settings = settings or get_settings()
    if recorder is not None:
        recorder.guard()

    model = chat_model(role, settings=settings, escalate=escalate)
    structured = model.with_structured_output(output_model, include_raw=True)

    attempt_messages = list(messages)
    last_error: Exception | None = None

    for attempt in range(MAX_STRUCTURED_ATTEMPTS):
        # include_raw=True makes this a {"raw", "parsed", "parsing_error"} dict.
        # The union in the runnable's signature covers include_raw=False too.
        result = cast(dict[str, Any], await structured.ainvoke(attempt_messages))

        if recorder is not None:
            await _record_usage(recorder, role, settings, escalate, result.get("raw"))

        parsed = result.get("parsed")
        if isinstance(parsed, output_model):
            return parsed

        last_error = result.get("parsing_error") or ValueError(
            f"model returned no valid {output_model.__name__}"
        )
        log.warning(
            "%s returned an unparseable %s (attempt %d/%d): %s",
            role,
            output_model.__name__,
            attempt + 1,
            MAX_STRUCTURED_ATTEMPTS,
            last_error,
        )
        if attempt < MAX_STRUCTURED_ATTEMPTS - 1:
            attempt_messages = [
                *messages,
                HumanMessage(content=_repair_prompt(output_model, last_error)),
            ]

    raise ValueError(
        f"{role} failed to produce a valid {output_model.__name__} after "
        f"{MAX_STRUCTURED_ATTEMPTS} attempts: {last_error}"
    )


async def run_tool_loop(
    role: Role,
    messages: list[BaseMessage],
    registry: ToolRegistry,
    *,
    settings: Settings | None = None,
    recorder: MetricsRecorder | None = None,
    escalate: bool = False,
    max_iterations: int = 12,
) -> list[BaseMessage]:
    """Let the model work with tools until it stops calling them.

    Returns the full message list including tool results, so the caller can ask
    for a structured summary afterwards with the whole trajectory in context.

    Bounded on three axes, because an unbounded agent loop is the failure mode
    that actually costs money: `max_iterations`, the run's token/cost ceiling via
    `recorder.guard()`, and the sandbox's own per-command timeout.
    """
    from featurepilot.tools.langchain_adapter import as_langchain_tools, execute_tool_calls

    settings = settings or get_settings()
    model = chat_model(role, settings=settings, escalate=escalate)
    bound = model.bind_tools(as_langchain_tools(registry))

    history = list(messages)
    for _round in range(max_iterations):
        if recorder is not None:
            recorder.guard()

        reply = await bound.ainvoke(_prune_tool_results(history))
        if recorder is not None:
            await _record_usage(recorder, role, settings, escalate, reply)
        history.append(reply)

        calls = getattr(reply, "tool_calls", None)
        if not calls:
            # Pruned on the way out too: the caller appends a summary request
            # and re-sends this, so an unpruned transcript would be paid for twice.
            return _prune_tool_results(history)

        results = await execute_tool_calls(registry, reply)
        history.extend(results)
    else:
        # Out of iterations with the model still reaching for tools. Say so in the
        # transcript rather than silently truncating: the summary step should know
        # the work may be incomplete.
        history.append(
            HumanMessage(
                content=(
                    f"You have used all {max_iterations} tool-call rounds. Stop calling "
                    "tools and report what you completed and what remains."
                )
            )
        )
    return _prune_tool_results(history)


async def _record_usage(
    recorder: MetricsRecorder,
    role: Role,
    settings: Settings,
    escalate: bool,
    raw: Any,
) -> None:
    """Pull token counts off the raw response.

    Usage metadata location varies by provider and LangChain version, so this is
    defensive: unaccounted tokens are a metrics gap, not a reason to fail a run.
    """
    usage: dict[str, Any] = {}
    if raw is not None:
        usage = getattr(raw, "usage_metadata", None) or {}
        if not usage:
            meta = getattr(raw, "response_metadata", {}) or {}
            usage = meta.get("usage", meta.get("token_usage", {})) or {}

    def pick(*keys: str) -> int:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                return value
        return 0

    model_name = settings.model_escalation if escalate else settings.model_for(role)
    await recorder.record_model_call(
        role=role,
        model=model_name,
        input_tokens=pick("input_tokens", "prompt_tokens"),
        output_tokens=pick("output_tokens", "completion_tokens"),
        cache_read=pick("cache_read_input_tokens"),
    )
