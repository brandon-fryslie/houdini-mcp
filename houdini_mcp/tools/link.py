"""The link to Houdini, as tools report it."""

from typing import Any

from ..connection import ping
from ._common import handle_connection_errors, using


@handle_connection_errors("check_connection")
def check_connection(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Check Houdini connection status with detailed info.

    Connects if not already connected, then reports the Houdini reached: its
    version, build and current hip file. When no Houdini can be reached the
    result is the same ``connection_unavailable`` failure every tool reports,
    naming the target and the cause.
    """
    # [LAW:single-enforcer] the scope's ensure is the one check: it reconnects when
    # the link is gone and fails when the live link reaches a different Houdini, and
    # its failure reads like every other tool's. The record is the link it holds,
    # not a re-read of installed state that may have changed since.
    with using(host, port) as link:
        return {"status": "success", **link.info()}


@handle_connection_errors("ping_houdini")
def ping_houdini(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    """
    Quick connectivity test to Houdini.

    Opens a short-lived connection, reads the Houdini reached, and closes it;
    the server's own connection is left as it was. The record is the one
    ``check_connection`` reports, naming the target the probe used. An
    unreachable Houdini is the ``connection_unavailable`` failure, naming the
    target and the cause.
    """
    return {"status": "success", **ping(host, port)}
