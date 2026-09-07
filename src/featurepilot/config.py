"""Runtime configuration.

Contract: `git clone && docker compose up && uv run fpilot` must work with
only ANTHROPIC_API_KEY set. Every other key is optional and has a documented
local fallback, so nothing here may be `Field(...)`-required except that one.
"""

from __future__ import annotations

import importlib.util
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"

#: Settings whose environment variable belongs to somebody else.
#:
#: These names are the ecosystem's, not ours: the Anthropic SDK, the LangSmith
#: CLI, and every Postgres client already read them. Namespacing them under `FP_`
#: would mean anyone with `ANTHROPIC_API_KEY` already exported has to duplicate
#: it, and that the LangSmith CLI cannot see the key this app uses — which is
#: exactly the friction that prompted this mapping.
#:
#: Everything not listed here is genuinely ours and *is* prefixed, because
#: `MAX_ATTEMPTS` or `SANDBOX_MEMORY` unprefixed would collide with anything.
STANDARD_ENV_NAMES: dict[str, str] = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "voyage_api_key": "VOYAGE_API_KEY",
    "langsmith_api_key": "LANGSMITH_API_KEY",
    "langsmith_project": "LANGSMITH_PROJECT",
    "langsmith_workspace_id": "LANGSMITH_WORKSPACE_ID",
    "postgres_dsn": "DATABASE_URL",
    "redis_url": "REDIS_URL",
    "github_token": "GITHUB_TOKEN",
    "aws_region": "AWS_REGION",
}


def _boto3_available() -> bool:
    return importlib.util.find_spec("boto3") is not None


def env_alias(field_name: str) -> str:
    """The environment variable a setting is read from.

    One name per setting, deliberately. An earlier version also accepted an
    `FP_`-prefixed alias for the third-party keys as a migration path; nothing
    ever used it, so it was two spellings and a branch to maintain for a case
    that did not exist.
    """
    return STANDARD_ENV_NAMES.get(field_name) or f"FP_{field_name.upper()}"


class Role(StrEnum):
    """Model-tiering axis. Each graph node declares the role it plays, and the
    LLM factory maps role -> model, so re-tiering for cost is one config edit
    rather than a hunt through node modules.
    """

    ROUTER = "router"  # cheap classification (1B+; 1A routing is LLM-free)
    PLANNER = "planner"
    CODER = "coder"
    CRITIC = "critic"
    DEBUGGER = "debugger"
    REVIEWER = "reviewer"
    SUMMARIZER = "summarizer"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # No global prefix: `alias_generator` decides per field, so third-party
        # keys keep their conventional names and only our own settings get `FP_`.
        alias_generator=env_alias,
        # Keeps `Settings(anthropic_api_key=...)` working in tests and code, which
        # an alias_generator would otherwise force to the alias spelling.
        populate_by_name=True,
        extra="ignore",
    )

    # --- models -----------------------------------------------------------
    # LiteLLM-style identifiers, so swapping to ollama/, openai/, or groq/
    # is a value change with no code change.
    anthropic_api_key: SecretStr | None = None

    model_router: str = "anthropic/claude-haiku-4-5"
    model_planner: str = "anthropic/claude-sonnet-5"
    model_coder: str = "anthropic/claude-sonnet-5"
    model_critic: str = "anthropic/claude-sonnet-5"
    model_debugger: str = "anthropic/claude-sonnet-5"
    model_reviewer: str = "anthropic/claude-sonnet-5"
    model_summarizer: str = "anthropic/claude-haiku-4-5"

    # Used only on the final retry, where the cost of another failed attempt
    # exceeds the cost of a better model.
    model_escalation: str = "anthropic/claude-opus-5"

    # --- budget guards ----------------------------------------------------
    # Load-bearing, not polish: an agent that loops is an agent that bills.
    max_attempts: int = 3
    # Cumulative input tokens are a poor ceiling for a tool loop: the same
    # retrieval context is re-sent on every call, so one piece of information is
    # counted once per iteration. Measured on click — 19 calls x ~21k = 400k
    # cumulative, of which the vast majority was an identical cached prefix
    # costing a tenth of list price. Cost is the honest instrument; this stays
    # only as a runaway backstop, set high enough not to fire on real work.
    max_tokens_per_run: int = 2_000_000
    max_usd_per_run: float = 2.00
    max_tokens_per_call: int = 16_000
    # Per-request deadline and retries. The client shipped with neither, and a
    # run against click hung for 817 seconds before LiteLLM's own default fired,
    # spending the wall clock and producing nothing. Requests carrying 70k
    # characters of context are slow enough that an explicit deadline matters.
    request_timeout_s: float = 180.0
    request_max_retries: int = 2

    # --- datastores -------------------------------------------------------
    postgres_dsn: str = "postgresql://featurepilot:featurepilot@localhost:5433/featurepilot"
    redis_url: str = "redis://localhost:6380/0"

    # --- sandbox ----------------------------------------------------------
    sandbox_image: str = "featurepilot-sandbox:py313"
    sandbox_memory: str = "2g"
    sandbox_cpus: float = 2.0
    sandbox_pids_limit: int = 512
    sandbox_command_timeout_s: int = 300
    # Test runs get their own, longer ceiling: a suite legitimately takes
    # longer than an agent's `grep`, and conflating the two means either
    # slow greps or truncated test runs.
    test_timeout_s: int = 600
    # Orphans from a crashed run are reaped at the next start. An hour is well
    # past any legitimate run, so a concurrent run is never touched.
    sandbox_reap_after_s: int = 3600
    sandbox_workdir: str = "/work"

    # --- retrieval --------------------------------------------------------
    # Names come from `retrieval/strategies.py`, which is also what the offline
    # benchmark constructs — so a measured improvement and the running agent
    # cannot disagree. 1B adds `embedding`/`hybrid` there; no node changes.
    retriever: str = "clean-query+content-rank"
    retrieval_top_k: int = 8
    retrieval_fusion_pool: int = 40

    # Publishing PRs. Absent => gh's ambient `gh auth` credential is used.
    github_token: SecretStr | None = None

    # Only read when a model is bedrock/*; LiteLLM hands it to boto3.
    aws_region: str = "us-east-1"

    # Absent VOYAGE key => local fastembed. Keeps the repo free and offline.
    voyage_api_key: SecretStr | None = None
    embed_model_local: str = "BAAI/bge-small-en-v1.5"
    embed_model_voyage: str = "voyage-code-3"
    rerank_model_voyage: str = "rerank-2.5"

    # --- observability ----------------------------------------------------
    langsmith_api_key: SecretStr | None = None
    langsmith_project: str = "feature-pilot"
    #: Only needed for org-scoped (service) keys. A personal key implies its
    #: own workspace, so this stays empty for most setups.
    langsmith_workspace_id: str = ""

    # --- api --------------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    #: Origins the browser frontend is served from. Comma-separated in the
    #: environment. Defaults to the local dev server only: a public deployment
    #: names its own origin rather than inheriting a permissive default.
    api_cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    #: Each concurrent run holds a sandbox container of `sandbox_memory`, so
    #: this is a memory ceiling, not a preference. Runs past it queue.
    max_concurrent_runs: int = 2
    #: Rejected outright past this many waiting, so a queue cannot grow
    #: unboundedly while every visitor watches a stalled placeholder.
    max_queued_runs: int = 8
    #: A publicly reachable deployment sets this false: a run against a server
    #: path would carry that path's contents back out through the retrieval
    #: context and the diff. The CLI and local development leave it on.
    allow_local_repos: bool = True
    #: Ceiling on what runs using *this server's* credentials may spend per
    #: day. A visitor who supplies their own key is spending their own money
    #: and is not counted against it.
    max_usd_per_day: float = 5.00

    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.api_cors_origins.split(",") if o.strip()]

    @field_validator(
        "anthropic_api_key",
        "voyage_api_key",
        "langsmith_api_key",
        "github_token",
        mode="before",
    )
    @classmethod
    def _empty_secret_is_absent(cls, value: object) -> object:
        """An unset CI secret arrives as "", not as a missing variable.

        `SecretStr("")` is a truthy object, so every "is this configured?"
        check would read true and the run would report a credential it does
        not have — then fail deep inside a provider call instead of at
        `doctor`.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _require_a_usable_provider(self) -> Settings:
        """Fail fast and legibly rather than deep inside a LiteLLM call.

        Only enforced for hosted anthropic/* models: pointing every role at
        ollama/* is a legitimate zero-key configuration.
        """
        hosted = [m for m in self._configured_models() if m.startswith("anthropic/")]
        if hosted and self.anthropic_api_key is None:
            raise ValueError(
                "ANTHROPIC_API_KEY is unset but these roles use hosted Anthropic "
                f"models: {sorted(set(hosted))}. Either set the key, or point the "
                "FP_MODEL_* settings at a local provider (e.g. ollama/qwen2.5-coder)."
            )
        # Bedrock has no static key to check — boto3's credential chain covers
        # env vars, ~/.aws, and EC2 instance roles — but the SDK must exist.
        bedrock = [m for m in self._configured_models() if m.startswith("bedrock/")]
        if bedrock and not _boto3_available():
            raise ValueError(
                f"these roles use Bedrock models but boto3 is not installed: "
                f"{sorted(set(bedrock))}. Install it with `uv sync --extra aws`."
            )
        return self

    def _configured_models(self) -> list[str]:
        return [
            self.model_router,
            self.model_planner,
            self.model_coder,
            self.model_critic,
            self.model_debugger,
            self.model_reviewer,
            self.model_summarizer,
        ]

    def model_for(self, role: Role) -> str:
        return str(getattr(self, f"model_{role.value}"))

    @property
    def tracing_enabled(self) -> bool:
        return self.langsmith_api_key is not None

    @property
    def voyage_enabled(self) -> bool:
        return self.voyage_api_key is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
