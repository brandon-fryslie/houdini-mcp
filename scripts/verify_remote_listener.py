#!/usr/bin/env python3
"""Opt-in live verification of a Houdini hrpyc remote listener.

This script connects to an *already running* Houdini hrpyc listener (started
via the "Start Remote" shelf tool or the manual ``import hrpyc;
hrpyc.start_server(...)`` workflow) and verifies end-to-end reachability from
the machine running this script (for example, the Docker gateway).

IMPORTANT:
- This script is **opt-in** and read-only. It NEVER starts, stops, restarts,
  or otherwise mutates a Houdini session. It only *connects and reads*.
- It must be explicitly enabled with ``RUN_LIVE_REMOTE_CHECK=1`` so it cannot
  run accidentally in CI.

Usage:
    RUN_LIVE_REMOTE_CHECK=1 \
    HOUDINI_HOST=127.0.0.1 HOUDINI_PORT=18811 \
    uv run python scripts/verify_remote_listener.py

Environment (read as the MCP server reads it, through houdini_mcp.settings):
    RUN_LIVE_REMOTE_CHECK   must be on (1/true/yes/on/t/y, any case) to run (safety gate)
    HOUDINI_HOST            listener host (default DEFAULT_TARGET.host in houdini_mcp.settings)
    HOUDINI_PORT            listener port (default DEFAULT_TARGET.port in houdini_mcp.settings)
    HOUDINI_RPC_TOKEN       optional shared secret (presented before the rpyc stream)

Exit codes:
    0  listener reachable and responsive
    1  verification failed
    2  not enabled / misconfigured (safety gate or bad env)
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

# Runs as a file, not a module: make houdini_mcp and the plugin tree's handshake
# package importable.
_REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_REPO), str(_REPO / "houdini_plugin" / "houdini_mcp" / "python")]

import houdini_mcp_handshake as handshake  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402

from houdini_mcp.settings import InvalidSetting, houdini_target, rpc_token, setting  # noqa: E402


def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> bool:
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    try:
        with socket.create_connection((connect_host, port), timeout=timeout):
            return True
    except OSError as exc:
        print(f"[verify] TCP probe to {connect_host}:{port} failed: {exc}")
        return False


# How long one read of the version or hip file may wait: a live listener answers
# these at once, so a longer wait is a Houdini too busy to be verified now.
RPC_TIMEOUT = 10.0


def _rpyc_check(host: str, port: int, token: str | None) -> bool:
    """Connect via rpyc.classic and read the Houdini version (read-only)."""
    # [LAW:one-source-of-truth] the client side of the token handshake is the
    # handshake package's, not a copy kept here.
    try:
        conn = handshake.connect(host, port, token=token, sync_request_timeout=RPC_TIMEOUT)
        hou = conn.modules.hou
        version = hou.applicationVersionString()
        hip = hou.hipFile.path()
        print(f"[verify] Connected. Houdini version: {version}")
        print(f"[verify] Current hip file: {hip}")
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] RPC check failed: {exc}")
        return False


def main() -> int:
    # [LAW:parse-dont-validate] every setting is parsed here, before anything is reached.
    try:
        enabled = setting(os.environ, "RUN_LIVE_REMOTE_CHECK", TypeAdapter(bool), "false")
        target = houdini_target(os.environ)
        token = rpc_token(os.environ)
    except InvalidSetting as e:
        print(f"[verify] Misconfigured: {e}")
        return 2
    if not enabled:
        print(
            "[verify] Live remote check is disabled. "
            "Set RUN_LIVE_REMOTE_CHECK=1 to run it explicitly."
        )
        return 2

    host, port = target.host, target.port
    print(f"[verify] Target listener: {host}:{port}")

    if not _tcp_probe(host, port):
        print("[verify] FAIL: listener is not reachable at the TCP level.")
        print(
            "[verify] Guidance: confirm the listener is bound to a routable "
            "address (not loopback-only) and that the firewall lets it through: "
            "Remote Self-Test in Houdini gives the command for that OS."
        )
        return 1

    print("[verify] TCP reachable.")

    if not _rpyc_check(host, port, token):
        print("[verify] FAIL: RPC handshake/version read did not succeed.")
        return 1

    print("[verify] OK: listener reachable and responsive (read-only check).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
