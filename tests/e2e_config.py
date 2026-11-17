"""Shared config resolution for the end-to-end tests.

The e2e tests need paths to local ROM files (a GameCube disc and the
button-counter ``.dol``). These are provided either via environment variables
(``GC_MCP_TEST_ROM`` / ``GC_MCP_BUTTON_COUNTER_ROM``) or, when the env var is
unset, via a local ``.e2e.env`` file at the repo root (git-ignored) so the
paths never need to be committed.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The git-ignored file that may hold the ROM paths (``KEY=VALUE`` lines).
CONFIG_FILE = Path(__file__).resolve().parents[1] / ".e2e.env"


def resolve(name: str) -> str:
    """Return the value for ``name`` from the environment or ``.e2e.env``.

    Prefers the environment variable; if unset, looks for a ``KEY=VALUE`` line
    in the repo-root ``.e2e.env`` file (blank lines and ``#`` comments are
    ignored). Returns ``""`` if neither is set.
    """
    value = os.environ.get(name, "")
    if value:
        return value
    if not CONFIG_FILE.is_file():
        return ""
    for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if sep and key.strip() == name:
            return val.strip()
    return ""
