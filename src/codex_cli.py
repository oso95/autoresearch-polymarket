"""Compatibility shim for older imports.

The runtime is provider-agnostic now; prefer importing from `src.model_cli`.
"""

from src.model_cli import *  # noqa: F401,F403
