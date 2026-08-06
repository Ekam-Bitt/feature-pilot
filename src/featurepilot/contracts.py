"""Typed node I/O.

Every node returns one of these models, produced via structured outputs. Two
consequences the whole design leans on:

1. The router reads typed fields instead of parsing prose, so routing is a
   pure function (see graph/router.py).
2. Nodes are independently testable — you can assert on a `DebuggerOutput`
   without running a model.

Schema constraints: these are sent to the model as JSON Schema, so keep to
str / int / bool / float / Literal / list / nested BaseModel. Avoid numeric
and length constraints (`ge`, `max_length`) — structured outputs ignores them
and they become client-side-only validation that can reject a
model-shaped-correctly response.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Confidence = Literal["low", "medium", "high"]

FailureCategory = Literal[
    "syntax",  # code does not parse
    "import",  # missing/incorrect import or dependency
    "assertion",  # test ran and the assertion failed — the interesting case
    "timeout",  # test hung
    "env",  # environment/fixture problem, not the patch's fault
    "unrelated",  # failure predates our change
]


# --- shared value objects -------------------------------------------------


class PlanStep(BaseModel):
    """One intended change. Deliberately coarse — a step is a unit of intent,
    not a diff hunk; the Coder decides the mechanics."""

    description: str = Field(description="What to change and why, in one or two sentences.")
    files: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths this step is expected to touch.",
    )


class FileEdit(BaseModel):
    path: str = Field(description="Repo-relative path.")
    rationale: str = Field(description="Why this edit is needed for the issue.")


class RetrievedChunk(BaseModel):
    """A retrieval hit. `why` exists so retrieval quality is inspectable
    without re-running the retriever, and so 1B stages can be compared."""

    path: str
    start_line: int
    end_line: int
    score: float
    why: str = Field(description="Why this chunk was returned for the query.")
    content: str = ""


class FailingTest(BaseModel):
    test_id: str = Field(description="e.g. tests/test_cart.py::test_total")
    message: str = Field(description="Assertion or error message, trimmed.")


# --- node outputs ---------------------------------------------------------


class PlannerOutput(BaseModel):
    summary: str = Field(description="One-paragraph statement of the fix you intend to make.")
    steps: list[PlanStep]
    files_needed: list[str] = Field(
        default_factory=list,
        description="Files you must read before editing. Retrieval will fetch these.",
    )
    open_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Questions that must be answered by a human before coding can proceed. "
            "Leave empty if the issue is unambiguous — do not invent questions."
        ),
    )
    confidence: Confidence


class RetrieverOutput(BaseModel):
    """Returned by every Retriever implementation. 1A's FilesystemRetriever and
    1B's HybridRetriever produce the same shape, which is what keeps the graph
    retrieval-agnostic."""

    files: list[str] = Field(default_factory=list)
    chunks: list[RetrievedChunk] = Field(default_factory=list)
    confidence: float = 0.0
    strategy: str = Field(default="", description="Which implementation produced this.")


class CoderOutput(BaseModel):
    edits: list[FileEdit]
    diff: str = Field(default="", description="Unified diff of all edits, filled by the node.")
    assumptions: list[str] = Field(
        default_factory=list,
        description="Assumptions made that a reviewer should check.",
    )


class CriticOutput(BaseModel):
    """Wired in Phase 1B. Defined now so adding the node is one edge rather
    than a schema migration plus a prompt rewrite."""

    assumptions_challenged: list[str] = Field(default_factory=list)
    missing_files: list[str] = Field(
        default_factory=list,
        description="Files that should have been read or changed but were not.",
    )
    alternative_fix: str | None = Field(
        default=None, description="A materially different approach, if one is better."
    )
    blast_radius: list[str] = Field(
        default_factory=list,
        description="Other modules this change could plausibly break.",
    )
    verdict: Literal["proceed", "revise"]


class TesterOutput(BaseModel):
    """Produced mechanically from the test run, never by a model.

    Judged **against a baseline** taken before any edit, rather than against a
    fully green suite. Requiring everything green assumes the repository had no
    pre-existing failures — false for the fixture (five independent defects) and
    false for most real repositories. Without a baseline, fixing the issue you
    were asked about still reports failure, and the debugger is then handed
    failures that have nothing to do with the patch.

    This is the FAIL_TO_PASS / PASS_TO_PASS split SWE-bench uses, for the same
    reason.
    """

    exit_code: int
    passed: int = 0
    failed: int = 0
    failing_tests: list[FailingTest] = Field(default_factory=list)
    raw_output: str = ""

    #: Failing now, passing at baseline. The patch broke these.
    regressions: list[str] = Field(default_factory=list)
    #: Failing at baseline, passing now. The patch fixed these.
    resolved: list[str] = Field(default_factory=list)
    #: Still failing and also failing at baseline — not this patch's business.
    pre_existing: list[str] = Field(default_factory=list)
    #: Whether a baseline was available. False means `success` falls back to
    #: requiring a wholly green suite, which is the honest reading with no
    #: baseline to compare against.
    baseline_known: bool = False
    #: How many tests were failing at baseline. Distinguishes "the repository
    #: started green" from "it started broken", which changes what success means.
    baseline_size: int = 0
    #: Total tests collected at baseline, and now. A drop means tests were
    #: deleted or skipped — which a pass/fail comparison cannot see, because
    #: removing a *passing* test produces no regression. Making the suite
    #: smaller is a way to make it green, and it must not read as success.
    baseline_total: int = 0
    collected_total: int = 0
    #: Test files the patch modified. Present or not, the result below was
    #: measured against the ORIGINAL tests.
    modified_test_files: list[str] = Field(default_factory=list)
    #: True when the suite was re-run with the agent's test edits reverted.
    #: Makes "did it bend the tests to fit" a measurement, not a judgement.
    verified_against_original_tests: bool = False

    @property
    def all_green(self) -> bool:
        """Literal reading: the entire suite passes."""
        return self.exit_code == 0 and self.failed == 0

    @property
    def tests_disappeared(self) -> int:
        """How many tests stopped being collected. Non-zero is disqualifying."""
        if not self.baseline_known or not self.baseline_total:
            return 0
        return max(0, self.baseline_total - self.collected_total)

    @property
    def success(self) -> bool:
        """The routing signal: did this patch do its job without breaking anything?

        Three cases, because one rule does not cover them:

        - **No baseline** — fall back to requiring a wholly green suite.
        - **Baseline was green** — there is nothing to resolve, so not breaking
          anything *is* success. Requiring a resolved test here would make every
          run against a healthy repository fail forever, including a feature
          request where nothing was failing to begin with.
        - **Baseline had failures** — require progress. A patch that breaks
          nothing and fixes nothing has not addressed the issue, and belongs with
          the debugger rather than the reviewer.
        """
        if not self.baseline_known:
            return self.all_green
        if self.regressions:
            return False
        # Shrinking the suite is not a fix.
        if self.tests_disappeared:
            return False
        if self.baseline_size == 0:
            return self.all_green
        return bool(self.resolved)


class DebuggerOutput(BaseModel):
    failure_category: FailureCategory
    root_cause: str = Field(description="The actual cause, not a restatement of the error.")
    suggested_edits: list[FileEdit] = Field(default_factory=list)
    retry: bool = Field(
        description=(
            "True if another coding attempt is worth making. False when the failure is "
            "environmental, pre-existing, or you cannot identify a fix."
        )
    )


class ReviewerOutput(BaseModel):
    verdict: Literal["approve", "reject"]
    reasons: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(
        default_factory=list,
        description="Issues that must be fixed before merge. Empty on approve.",
    )


class PRSummary(BaseModel):
    title: str
    body: str = Field(description="Markdown PR description: what changed and why.")
    test_plan: str = Field(description="How a reviewer verifies this.")


# --- human input ----------------------------------------------------------


class HumanDecision(BaseModel):
    """A human's answer at an approval gate.

    Typed like a node output rather than passed as a bare string, so resuming a
    checkpointed run goes through the same validation as everything else — and
    so `feedback` has a defined home instead of being smuggled into a message.
    """

    verdict: Literal["approve", "reject"]
    feedback: str = Field(
        default="",
        description="Why, and what to change. Fed back to the planner on reject.",
    )
    #: Answers to PlannerOutput.open_questions, in the order asked.
    answers: list[str] = Field(default_factory=list)
