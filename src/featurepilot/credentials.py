"""Credentials for one run.

The public frontend lets a visitor supply their own keys, used for their
session only, with the server's own as the default. That makes credentials run
data rather than process configuration — and the process environment the wrong
place to keep them, because two concurrent runs with different keys race there
and whichever writes last bills both.

Nothing here is ever persisted or logged: a resolved Settings copy lives for
the duration of one run and is dropped with it.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from featurepilot.config import Settings


def _present(secret: SecretStr | None) -> SecretStr | None:
    """Treat a blank secret as absent — the frontend's modal submits empty
    fields when a visitor skips it, and SecretStr("") is a truthy object."""
    if secret is None or not secret.get_secret_value().strip():
        return None
    return secret


@dataclass(frozen=True, slots=True)
class RunCredentials:
    """What a caller supplied for this run. Absent fields fall back to the
    server's own configuration."""

    anthropic_api_key: SecretStr | None = None
    github_token: SecretStr | None = None

    @property
    def uses_server_credentials(self) -> bool:
        """True when this run spends the operator's money and publishes under
        the operator's identity — which is what the public deployment caps."""
        return _present(self.anthropic_api_key) is None and _present(self.github_token) is None

    def resolve(self, settings: Settings) -> Settings:
        """A Settings copy for this run, without touching the caller's."""
        supplied = {
            name: value
            for name, value in (
                ("anthropic_api_key", _present(self.anthropic_api_key)),
                ("github_token", _present(self.github_token)),
            )
            if value is not None
        }
        return settings.model_copy(update=supplied) if supplied else settings
