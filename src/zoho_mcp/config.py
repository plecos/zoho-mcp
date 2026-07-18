"""Shared config loading.

Anchors ``.env`` to the project root rather than the caller's current working
directory -- ``python-dotenv``'s default search walks up from cwd, which
breaks when the server is spawned by an MCP client from an arbitrary
directory (e.g. ``claude mcp add`` with a user-scoped server).
"""

from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def load_env() -> None:
    """Load ``.env`` from the project root, regardless of the caller's cwd."""
    load_dotenv(PROJECT_ROOT / ".env")
