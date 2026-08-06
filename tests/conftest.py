"""Fixture wiring. The doubles themselves live in tests/fakes.py."""

from __future__ import annotations

import pytest

from fakes import DEFAULT_FILES, FakeFileSystem, StubRetriever
from featurepilot.config import Settings
from featurepilot.metrics.events import InMemorySink
from featurepilot.metrics.recorder import MetricsRecorder
from featurepilot.tools.registry import ToolRegistry


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
