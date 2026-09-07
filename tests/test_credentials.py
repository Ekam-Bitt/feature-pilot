"""Per-run credentials.

A public frontend lets a visitor supply their own keys for their session, with
the server's own as the default. That makes credentials *run data*, and the
process environment the wrong place to keep them: two concurrent runs with
different keys race, and whichever writes `os.environ` last bills both.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from featurepilot.config import Role, Settings
from featurepilot.credentials import RunCredentials

SERVER = Settings(
    anthropic_api_key=SecretStr("sk-server"),
    github_token=SecretStr("ghp-server"),
    _env_file=None,
)  # type: ignore[call-arg]


class TestResolution:
    def test_empty_credentials_fall_back_to_the_server_key(self) -> None:
        resolved = RunCredentials().resolve(SERVER)
        assert resolved.anthropic_api_key is not None
        assert resolved.anthropic_api_key.get_secret_value() == "sk-server"
        assert resolved.github_token is not None
        assert resolved.github_token.get_secret_value() == "ghp-server"

    def test_supplied_credentials_win(self) -> None:
        resolved = RunCredentials(
            anthropic_api_key=SecretStr("sk-visitor"), github_token=SecretStr("ghp-visitor")
        ).resolve(SERVER)
        assert resolved.anthropic_api_key.get_secret_value() == "sk-visitor"
        assert resolved.github_token.get_secret_value() == "ghp-visitor"

    def test_one_supplied_key_does_not_drop_the_other(self) -> None:
        resolved = RunCredentials(anthropic_api_key=SecretStr("sk-visitor")).resolve(SERVER)
        assert resolved.anthropic_api_key.get_secret_value() == "sk-visitor"
        assert resolved.github_token.get_secret_value() == "ghp-server"

    def test_resolution_does_not_mutate_the_server_settings(self) -> None:
        RunCredentials(anthropic_api_key=SecretStr("sk-visitor")).resolve(SERVER)
        assert SERVER.anthropic_api_key.get_secret_value() == "sk-server"

    def test_byo_marks_the_session_as_not_using_server_credentials(self) -> None:
        assert RunCredentials().uses_server_credentials is True
        assert RunCredentials(anthropic_api_key=SecretStr("sk-v")).uses_server_credentials is False

    def test_blank_strings_are_not_credentials(self) -> None:
        """The frontend modal submits empty fields when the visitor skips it."""
        creds = RunCredentials(anthropic_api_key=SecretStr("  "), github_token=SecretStr(""))
        assert creds.uses_server_credentials is True
        assert creds.resolve(SERVER).anthropic_api_key.get_secret_value() == "sk-server"


class TestNoProcessWideLeak:
    """The load-bearing property: a run's key reaches the model without ever
    passing through the environment, so concurrent runs cannot cross-bill."""

    def test_chat_model_carries_the_runs_key_not_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from featurepilot.llm import chat_model

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-environment")
        visitor = Settings(anthropic_api_key=SecretStr("sk-visitor"), _env_file=None)  # type: ignore[call-arg]

        model = chat_model(Role.CODER, settings=visitor)

        assert model.api_key == "sk-visitor"

    def test_two_runs_keep_their_own_keys(self) -> None:
        from featurepilot.llm import chat_model

        a = Settings(anthropic_api_key=SecretStr("sk-a"), _env_file=None)  # type: ignore[call-arg]
        b = Settings(anthropic_api_key=SecretStr("sk-b"), _env_file=None)  # type: ignore[call-arg]

        model_a = chat_model(Role.CODER, settings=a)
        model_b = chat_model(Role.CODER, settings=b)

        # Constructing the second must not retroactively change the first.
        assert (model_a.api_key, model_b.api_key) == ("sk-a", "sk-b")


class TestCredentialsNeverEscape:
    """A visitor's key must not appear anywhere a run leaves a trace: the SSE
    stream, Redis, Postgres, the .fp artifacts, or a log line. Redaction is
    checked by searching serialised output for the secret rather than by
    asserting on a field list, so a new field that carries it still fails."""

    SECRET = "sk-visitor-do-not-leak"

    def test_a_records_public_view_never_contains_it(self) -> None:
        from featurepilot.api.manager import RunRecord

        record = RunRecord(run_id="r", repo="/tmp/x", issue_ref="a/b#1")
        record.credentials = RunCredentials(anthropic_api_key=SecretStr(self.SECRET))
        assert self.SECRET not in str(record.public())

    def test_secretstr_does_not_render_in_a_repr(self) -> None:
        """The whole record is logged on failure paths (`log.exception`), so
        the dataclass repr is a real exposure route."""
        from featurepilot.api.manager import RunRecord

        record = RunRecord(run_id="r", repo="/tmp/x", issue_ref="a/b#1")
        record.credentials = RunCredentials(anthropic_api_key=SecretStr(self.SECRET))
        assert self.SECRET not in repr(record)
        assert self.SECRET not in repr(record.credentials)

    def test_resolved_settings_do_not_render_it(self) -> None:
        resolved = RunCredentials(anthropic_api_key=SecretStr(self.SECRET)).resolve(SERVER)
        assert self.SECRET not in repr(resolved)
        assert self.SECRET not in str(resolved.model_dump())

    def test_the_key_is_absent_from_the_process_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every other run in this process would otherwise inherit it."""
        import os

        from featurepilot.llm import chat_model

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        resolved = RunCredentials(anthropic_api_key=SecretStr(self.SECRET)).resolve(SERVER)
        chat_model(Role.CODER, settings=resolved)
        assert os.environ.get("ANTHROPIC_API_KEY") != self.SECRET
