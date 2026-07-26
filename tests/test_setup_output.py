"""Tests for what `zoho-mcp-setup` prints once auth succeeds.

The setup flow itself is thin wiring over tested pieces, but the config
snippet it hands the user is not: an MCP client rejects the whole server
entry if the path or the JSON shape is wrong, and the failure surfaces as
"server disconnected" with nothing pointing back here.
"""

import json
import os
import sys
from pathlib import Path

from zoho_mcp.setup_auth import build_client_config_snippet, zoho_mcp_executable


def test_executable_sits_next_to_the_running_interpreter():
    # Console scripts land in the same Scripts/bin directory as python
    # itself, so a relative "zoho-mcp" that only resolves on PATH isn't
    # good enough -- MCP clients launch with their own environment.
    executable = zoho_mcp_executable()

    assert executable.is_absolute()
    assert executable.parent == Path(sys.executable).parent


def test_executable_name_matches_the_platform():
    name = zoho_mcp_executable().name

    assert name == ("zoho-mcp.exe" if os.name == "nt" else "zoho-mcp")


def test_config_snippet_is_valid_json_in_the_shape_clients_expect():
    # str(Path(...)) rather than a literal: Path renders with the running
    # platform's separator, and the snippet has to carry whatever the
    # client will actually be asked to execute.
    executable = Path("/opt/venv/bin/zoho-mcp")

    parsed = json.loads(build_client_config_snippet(executable))

    assert parsed == {
        "mcpServers": {"zoho-mcp": {"command": str(executable), "args": []}}
    }


def test_config_snippet_keeps_windows_backslashes_intact():
    # A Windows path round-trips through JSON only if the backslashes are
    # escaped; pasting a snippet with a bare \U or \n in it is a parse
    # error in the client's config file, not a broken path.
    windows_path = Path(r"F:\Repos\zoho-mcp\.venv\Scripts\zoho-mcp.exe")

    parsed = json.loads(build_client_config_snippet(windows_path))

    assert parsed["mcpServers"]["zoho-mcp"]["command"] == str(windows_path)
