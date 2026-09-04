"""Recura - autonomous revenue-recovery agent.

Importing the package loads `.env` so that the setup the README documents actually
takes effect. Real environment variables still win; see src/env.py.
"""

from src.env import load_env as _load_env

_load_env()
