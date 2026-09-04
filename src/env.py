"""Load `.env` into the process environment.

The README tells a reviewer to `cp .env.example .env` and put their key in it. Nothing in
the codebase read that file, so following the documented instruction produced a `.env`
that did nothing and a `make tier1` that reported the key as missing while it sat on disk.

Deliberately dependency-free: `python-dotenv` is a fine library, but this is fifteen lines
and the project already declines dependencies it does not need.

Real environment variables always win over the file. A key exported in the shell is a
deliberate act; a key in `.env` is a default.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def load_env(path: Path | None = None) -> int:
    """Read KEY=VALUE lines into os.environ. Returns how many variables were set."""
    target = path or ENV_PATH
    if not target.exists():
        return 0

    loaded = 0
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        # Never clobber a variable the caller exported on purpose.
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded
