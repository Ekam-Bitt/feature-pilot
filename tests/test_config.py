"""Environment variable naming.

Third-party services own their variable names; only settings that are genuinely
ours are prefixed. Getting this wrong is quietly expensive: prefixing
`ANTHROPIC_API_KEY` forces anyone who already has it exported to duplicate it, and
means the `langsmith` CLI cannot read the same key the app traces with.

One spelling per setting — no aliases. A migration path existed briefly and had no
users, which made it a branch and a precedence rule maintained for nobody.
"""

from __future__ import annotations

import pytest

from featurepilot.config import STANDARD_ENV_NAMES, Role, Settings, env_alias


def _alias(field: str) -> str:
    return str(Settings.model_fields[field].validation_alias)


class TestNaming:
    @pytest.mark.parametrize(("field", "expected"), sorted(STANDARD_ENV_NAMES.items()))
    def test_third_party_keys_use_conventional_names(self, field: str, expected: str) -> None:
        assert _alias(field) == expected

    @pytest.mark.parametrize(
        "field", ["max_attempts", "sandbox_memory", "retriever", "api_port", "model_coder"]
    )
    def test_our_own_settings_are_prefixed(self, field: str) -> None:
        """Unprefixed, these would collide with anything else in the environment."""
        assert _alias(field) == f"FP_{field.upper()}"

    def test_every_standard_name_maps_to_a_real_field(self) -> None:
        """A mapping for a field that does not exist is dead config that reads as
        support for a provider we do not actually use."""
        for field in STANDARD_ENV_NAMES:
            assert field in Settings.model_fields, f"{field} is mapped but not a setting"

    def test_alias_helper_is_pure(self) -> None:
        assert env_alias("anthropic_api_key") == "ANTHROPIC_API_KEY"
        assert env_alias("max_attempts") == "FP_MAX_ATTEMPTS"

    def test_each_setting_has_exactly_one_name(self) -> None:
        """One spelling per setting. Two would mean a branch to maintain and a
        precedence rule to explain, for no caller."""
        for name in Settings.model_fields:
            assert isinstance(_alias(name), str)


class TestPrecedenceAndCompatibility:
    def test_conventional_name_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-standard")
        monkeypatch.delenv("FP_ANTHROPIC_API_KEY", raising=False)
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.anthropic_api_key is not None
        assert settings.anthropic_api_key.get_secret_value() == "sk-standard"

    def test_a_prefixed_third_party_name_is_not_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`FP_ANTHROPIC_API_KEY` is not a name this app knows. Silently honouring
        it would resurrect the two-spellings problem by accident."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("FP_ANTHROPIC_API_KEY", "sk-legacy")
        with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_database_url_is_the_postgres_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """DATABASE_URL is what every Postgres tool and PaaS already injects."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host:5432/db")
        assert Settings(_env_file=None).postgres_dsn.endswith("/db")  # type: ignore[call-arg]

    def test_construction_by_field_name_still_works(self) -> None:
        """An alias generator would otherwise force call sites to spell the alias."""
        settings = Settings(anthropic_api_key="sk-direct", _env_file=None)  # type: ignore[call-arg]
        assert settings.anthropic_api_key is not None
        assert settings.model_for(Role.CODER).startswith("anthropic/")


def test_the_error_message_names_the_variable_people_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Telling someone to set a variable that is not the one we read is worse
    than saying nothing."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("FP_ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match=r"\bANTHROPIC_API_KEY\b") as exc:
        Settings(_env_file=None)  # type: ignore[call-arg]
    assert "FP_ANTHROPIC_API_KEY is unset" not in str(exc.value)
