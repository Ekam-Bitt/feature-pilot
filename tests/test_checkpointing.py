"""Postgres checkpointing: what survives a round-trip, and what does not.

Marked `postgres` and excluded from the default run, because it needs the compose
datastore. It needs no model, no container and no money.

The property under test is the one the project got wrong once and defends in two
places. State goes through the production `_checkpointer` and `_serde`, comes back
out through a fresh saver — a real SELECT and a real deserialisation, not a value
handed back from memory — and then:

- Pydantic contracts must return as objects. That is what the `allowed` list in
  `run._serde` exists to buy, and a resumed run reads `plan.steps[0].files`.
- `RunPhase` must *not* be expected to return as a `RunPhase`. It is a `StrEnum`,
  so msgpack stores it as the string it already is and registration cannot restore
  a type that was never distinguishable on the wire. `router._as_phase` is what
  keeps a resumed run routable, and this test pins that division of labour so a
  future reader does not "fix" the coercion away on the strength of the comment
  next to the registration list.
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from featurepilot.config import Settings
from featurepilot.contracts import PlannerOutput, PlanStep
from featurepilot.graph.router import _as_phase
from featurepilot.lifecycle import RunPhase
from featurepilot.run import _checkpointer

pytestmark = pytest.mark.postgres

PLAN = PlannerOutput(
    summary="checkpoint round-trip",
    steps=[PlanStep(description="survive storage", files=["src/shopsvc/cart.py"])],
    open_questions=[],
    confidence="high",
)


class _State(TypedDict, total=False):
    phase: RunPhase
    plan: PlannerOutput


async def _write(state: _State) -> _State:
    return {"phase": RunPhase.REVIEW, "plan": PLAN}


def _graph() -> Any:
    graph: Any = StateGraph(_State)
    graph.add_node("write", _write)
    graph.add_edge(START, "write")
    graph.add_edge("write", END)
    return graph


def _require_postgres(saver: object) -> None:
    """`_checkpointer` degrades to memory by design; a green in-memory run here
    would prove nothing about storage, so say so loudly instead."""
    name = type(saver).__name__
    if name != "AsyncPostgresSaver":
        pytest.skip(f"postgres unreachable ({name} in use); run `docker compose up -d`")


async def test_state_survives_a_round_trip_through_postgres() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    config: Any = {"configurable": {"thread_id": "test-round-trip"}}

    async with _checkpointer(settings) as saver:
        _require_postgres(saver)
        await _graph().compile(checkpointer=saver).ainvoke({}, config)

    # Fresh saver, fresh connection: the values below are deserialised from the
    # database rather than remembered from the write above.
    async with _checkpointer(settings) as saver:
        _require_postgres(saver)
        values = (await _graph().compile(checkpointer=saver).aget_state(config)).values

    plan = values["plan"]
    assert isinstance(plan, PlannerOutput), f"contract came back as {type(plan).__name__}"
    assert plan.summary == "checkpoint round-trip"
    assert isinstance(plan.steps[0], PlanStep), "nested contract lost"
    assert plan.steps[0].files == ["src/shopsvc/cart.py"]

    phase = values["phase"]
    assert phase == RunPhase.REVIEW, "the phase value itself must survive"
    assert _as_phase(phase) is RunPhase.REVIEW, "coercion must restore a routable enum"


async def test_the_phase_comes_back_as_a_plain_string() -> None:
    """Pins the limitation the coercion exists for.

    If this ever starts failing, `RunPhase` is round-tripping as an enum and the
    comments in `run._serde` and `router.route` need updating — but `_as_phase`
    should still stay, since it costs one isinstance check and removes a class of
    silent routing failure.
    """
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    config: Any = {"configurable": {"thread_id": "test-phase-type"}}

    async with _checkpointer(settings) as saver:
        _require_postgres(saver)
        await _graph().compile(checkpointer=saver).ainvoke({}, config)

    async with _checkpointer(settings) as saver:
        _require_postgres(saver)
        values = (await _graph().compile(checkpointer=saver).aget_state(config)).values

    assert not isinstance(values["phase"], RunPhase), (
        "a StrEnum now survives msgpack; see this test's docstring"
    )
    assert isinstance(values["phase"], str)
