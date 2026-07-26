"""Smoke-test a packed .mcpb by running it the way a host would.

Unpacks the bundle, launches it using the command declared in its own
``manifest.json``, and drives an MCP handshake over stdio. Nothing here is
hardcoded about how the server starts -- the point is to test the manifest,
not a copy of it, so a broken ``mcp_config`` fails here rather than on a
user's machine after install.

Exists because the failures this catches are invisible to the unit suite. The
bundle's own tests check that the manifest is *internally* consistent; only
actually launching it proves the host can. Two real examples, both found by
hand before this existed: the manifest schema requires ``mcp_config.command``
even for the ``uv`` server type, and a server that starts fine on the
developer's machine may not on a runner with no Python installed.

Usage:
    python scripts/smoke_bundle.py path/to/zoho-mcp.mcpb
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

# Generous: the first launch resolves and installs every dependency, which on
# a cold CI runner is the slowest thing that happens here by far.
STARTUP_TIMEOUT_SECONDS = 300
SHUTDOWN_TIMEOUT_SECONDS = 60
PROTOCOL_VERSION = "2025-06-18"


class SmokeFailure(Exception):
    """Raised when the bundle doesn't behave the way a host would need."""


def _resolve_command(manifest: dict, bundle_dir: Path) -> list[str]:
    """Build the argv the host would run, from the manifest's own mcp_config."""
    config = manifest["server"]["mcp_config"]
    argv = [config["command"], *config.get("args", [])]
    return [part.replace("${__dirname}", str(bundle_dir)) for part in argv]


def _read_message(process: subprocess.Popen) -> dict:
    """Read one JSON-RPC message, failing loudly if the server died instead."""
    assert process.stdout is not None
    line = process.stdout.readline()
    if not line:
        stderr = ""
        if process.stderr is not None:
            stderr = process.stderr.read()
        raise SmokeFailure(
            f"server produced no response (exit code {process.poll()}).\n"
            f"stderr:\n{stderr}"
        )
    try:
        return json.loads(line)
    except json.JSONDecodeError as e:
        raise SmokeFailure(f"server wrote non-JSON to stdout: {line!r}") from e


def _send(process: subprocess.Popen, message: dict) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def smoke_test(bundle_path: Path, workdir: Path) -> int:
    """Unpack, launch and handshake. Returns the number of tools listed."""
    bundle_dir = workdir / "bundle"
    with zipfile.ZipFile(bundle_path) as archive:
        archive.extractall(bundle_dir)

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    argv = _resolve_command(manifest, bundle_dir)
    if shutil.which(argv[0]) is None:
        raise SmokeFailure(
            f"the manifest's command {argv[0]!r} is not on PATH. A host that "
            f"supplies its own runtime may still work, but this cannot verify it."
        )
    print(f"launching: {' '.join(argv)}", flush=True)

    process = subprocess.Popen(
        argv,
        cwd=bundle_dir,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
        # Deliberately no Zoho credentials: the server must start and list its
        # tools while unauthenticated, which is exactly the state a fresh
        # install is in. Anything that needs a token belongs behind a tool
        # call, not startup.
        env={**os.environ, "ZOHO_CLIENT_ID": "", "ZOHO_CLIENT_SECRET": ""},
    )

    try:
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "smoke-test", "version": "0"},
                },
            },
        )
        initialized = _read_message(process)
        server_info = initialized["result"]["serverInfo"]
        print(f"initialize ok: {server_info}", flush=True)

        _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = _read_message(process)
        tools = listed["result"]["tools"]

        declared = {tool["name"] for tool in manifest.get("tools", [])}
        served = {tool["name"] for tool in tools}
        if declared != served:
            raise SmokeFailure(
                f"manifest and server disagree on tools.\n"
                f"  only in manifest: {sorted(declared - served)}\n"
                f"  only in server:   {sorted(served - declared)}"
            )
        untitled = sorted(t["name"] for t in tools if not t.get("title"))
        if untitled:
            raise SmokeFailure(f"tools missing a title: {untitled}")
        print(f"tools/list ok: {len(tools)} tools, all titled", flush=True)
    finally:
        if process.stdin is not None:
            process.stdin.close()

    # Closing stdin is the only shutdown signal MCP defines, so a server that
    # ignores it would leave the host to escalate to SIGTERM -- and on Windows
    # would hold its own files open against an uninstall.
    try:
        process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        raise SmokeFailure(
            f"server did not exit within {SHUTDOWN_TIMEOUT_SECONDS}s of stdin closing"
        ) from None
    print(f"clean exit on stdin close, code {process.returncode}", flush=True)
    return len(tools)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    bundle_path = Path(sys.argv[1]).resolve()
    if not bundle_path.is_file():
        print(f"no such bundle: {bundle_path}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        try:
            count = smoke_test(bundle_path, Path(tmp))
        except (SmokeFailure, KeyError, OSError) as e:
            print(f"\nSMOKE TEST FAILED: {e}", file=sys.stderr)
            return 1
    print(f"\nOK -- {bundle_path.name} serves {count} tools on this platform")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
