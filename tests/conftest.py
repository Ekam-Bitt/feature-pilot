"""Fixture wiring. The doubles themselves live in tests/fakes.py."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from fakes import DEFAULT_FILES, FakeFileSystem, StubRetriever
from featurepilot.config import Settings, get_settings
from featurepilot.metrics.events import InMemorySink
from featurepilot.metrics.recorder import MetricsRecorder
from featurepilot.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make the suite independent of whoever is running it.

    `Settings` reads `.env` and refuses to construct without a provider
    credential, so a developer with a populated `.env` and a CI runner with none
    disagree about whether a test can build its subject at all. That is how 24
    API tests passed here and errored on the first CI run — the suite was reading
    ambient configuration it never declared.

    Forcing a dummy key makes both environments agree, and guarantees no test can
    reach a real provider with a real credential even by accident. The cache is
    cleared on both sides because `get_settings` is `lru_cache`d: a value built
    under one test's environment must not leak into the next.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings() -> Settings:
    """Dummy key so the provider validator passes without a real credential.
    `_env_file=None` keeps a developer's local .env out of the test run."""
    return Settings(anthropic_api_key="sk-ant-test", _env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def sink() -> InMemorySink:
    return InMemorySink()


@pytest.fixture
def recorder(sink: InMemorySink, settings: Settings) -> MetricsRecorder:
    return MetricsRecorder(run_id="run-test", sink=sink, settings=settings)


@pytest.fixture
def fake_fs() -> FakeFileSystem:
    return FakeFileSystem(DEFAULT_FILES)


@pytest.fixture
def fake_registry(fake_fs: FakeFileSystem) -> ToolRegistry:
    return fake_fs.as_registry()


@pytest.fixture
def stub_retriever() -> StubRetriever:
    return StubRetriever()
