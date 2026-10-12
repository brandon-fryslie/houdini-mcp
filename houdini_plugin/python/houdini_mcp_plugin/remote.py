"""Remote mode for the Houdini MCP Plugin (module-level facade).

Each module-level function here is one operation on the process's single
:class:`houdini_mcp_plugin.listener.RemoteListener`; the functions exist so
shelf tools and shell users have a stable, import-only entry point.

The manual/direct hrpyc workflow (running ``import hrpyc;
hrpyc.start_server(port=...)`` in Houdini's Python shell) remains fully
supported and untouched — this facade is an *additional*, safer entry point.

Security: binds to ``127.0.0.1`` by default. Non-loopback binds (via
``HOUDINI_RPC_BIND_HOST``) require an explicit trusted-network opt-in
(``HOUDINI_RPC_TRUSTED_NETWORK=1``) or an authentication token
(``HOUDINI_RPC_TOKEN``). See :mod:`houdini_mcp_plugin.listener`.
"""

from __future__ import annotations

from .listener import ListenerConfig, RemoteListener

# [LAW:no-shared-mutable-globals] A Houdini process runs one hrpyc listener, and
# this is it: bound once, never rebound. RemoteListener owns its lifecycle and
# every answer about it, and resolves ``hrpyc`` / ``hou`` on first use, so
# constructing it here imports nothing Houdini-only.
_listener = RemoteListener()


def _config_from_env(port: int | None) -> ListenerConfig:
    """The environment's listener config, with ``port`` overriding ``HOUDINI_RPC_BIND_PORT``."""
    cfg = ListenerConfig.from_env()
    if port is not None:
        cfg.port = port
    return cfg


def start_hrpyc_server(port: int | None = None) -> dict:
    """Start the hrpyc listener for remote connections (backwards-compatible).

    Bind host / security options are read from the environment
    (``HOUDINI_RPC_BIND_HOST``, ``HOUDINI_RPC_TRUSTED_NETWORK``,
    ``HOUDINI_RPC_TOKEN``). ``port`` overrides ``HOUDINI_RPC_BIND_PORT`` when
    provided.
    """
    return _listener.start(_config_from_env(port))


def stop_hrpyc_server() -> dict:
    """Stop the hrpyc listener."""
    return _listener.stop()


def is_hrpyc_running() -> bool:
    """Return True if the hrpyc listener is running."""
    return _listener.is_running()


def get_hrpyc_status() -> dict:
    """Return the current hrpyc listener status."""
    return _listener.status()


def self_test_hrpyc() -> dict:
    """Run a reachability + capability self-test on the hrpyc listener."""
    return _listener.self_test()


def reload_hrpyc_server(port: int | None = None) -> dict:
    """Restart the hrpyc listener with current environment configuration."""
    return _listener.reload(_config_from_env(port))
