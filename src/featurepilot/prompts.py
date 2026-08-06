"""Prompt loading.

Prompts live in `prompts/*.md`, never inlined in Python: they're diffable,
reviewable in a PR, and tunable without touching code.

Loaded once and cached. That's also a prompt-caching requirement — the rendered
system prompt has to be byte-identical across turns or the provider cache never
hits, and re-reading a file someone is editing mid-run would silently break that.
"""

from __future__ import annotations

from functools import lru_cache

from featurepilot.config import PROMPTS_DIR


class PromptMissing(FileNotFoundError):
    def __init__(self, name: str) -> None:
        available = sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))
        super().__init__(f"no prompt {name!r} in {PROMPTS_DIR}; available: {available}")


@lru_cache(maxsize=32)
def load(name: str) -> str:
    """Return the prompt body for `name` (without the .md extension)."""
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptMissing(name)
    return path.read_text(encoding="utf-8").strip()
