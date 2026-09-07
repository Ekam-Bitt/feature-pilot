"""LiteLLM reads provider credentials from the environment; Settings is the
single source of truth that exports them."""

from __future__ import annotations

import os

import pytest

from featurepilot.config import Settings
from featurepilot.llm import _export_provider_keys

BEDROCK = "bedrock/us.anthropic.claude-sonnet-4-5-20250929-v1:0"


class TestAwsRegionExport:
    def test_bedrock_models_export_the_region(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_REGION", raising=False)
        settings = Settings(model_coder=BEDROCK, aws_region="us-east-1", _env_file=None)  # type: ignore[call-arg]
        _export_provider_keys(settings)
        assert os.environ["AWS_REGION"] == "us-east-1"

    def test_an_existing_region_is_not_clobbered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AWS_REGION", "eu-west-1")
        settings = Settings(model_coder=BEDROCK, aws_region="us-east-1", _env_file=None)  # type: ignore[call-arg]
        _export_provider_keys(settings)
        assert os.environ["AWS_REGION"] == "eu-west-1"

    def test_no_bedrock_no_export(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AWS_REGION", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        _export_provider_keys(Settings(_env_file=None))  # type: ignore[call-arg]
        assert "AWS_REGION" not in os.environ
