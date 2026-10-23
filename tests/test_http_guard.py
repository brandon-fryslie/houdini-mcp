"""Who the HTTP server answers: a request naming this machine's loopback, or a name the
user listed, and nothing a LAN client or a DNS-rebinding page would send."""

import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.live_houdini import houdini_mcp_over_http, running_listener

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "guard-test", "version": "0"},
    },
}


@pytest.fixture(scope="module")
def port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    """A houdini-mcp serving HTTP with its defaults, but for the one name it admits."""
    workdir: Path = tmp_path_factory.mktemp("guard")
    env = {
        "HOUDINI_MCP_RESULTS_DIR": str(workdir / "results"),
        "MCP_ALLOWED_HOSTS": "studio-box",
    }
    with running_listener(houdini_mcp_over_http(workdir), env, workdir / "log") as port:
        yield port


def initialize(port: int, **headers: str) -> int:
    """The status the server answers an MCP ``initialize`` with, sent to its loopback
    socket under ``headers``."""
    sent = httpx.post(
        f"http://127.0.0.1:{port}/mcp",
        json=INITIALIZE,
        headers={"Accept": "application/json, text/event-stream", **headers},
    )
    return sent.status_code


def test_a_request_naming_another_machine_is_refused(port: int) -> None:
    """The Host a LAN address or a rebinding page's own domain puts on the request."""
    assert initialize(port, Host=f"192.168.1.50:{port}") == 421
    assert initialize(port, Host=f"evil.example:{port}") == 421


def test_a_page_from_another_origin_is_refused(port: int) -> None:
    assert initialize(port, Host=f"localhost:{port}", Origin="http://evil.example") == 403


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "studio-box"])
def test_loopback_and_a_listed_name_are_answered(port: int, host: str) -> None:
    assert initialize(port, Host=f"{host}:{port}") == 200


def this_machines_lan_address() -> str:
    """The address this machine sends from toward the internet: a non-loopback one of its own."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))  # UDP: names a route, sends nothing
        address: str = probe.getsockname()[0]
    assert not ipaddress.ip_address(address).is_loopback, address
    return address


def test_only_loopback_is_listened_on(port: int) -> None:
    """The bind is what keeps a LAN client out: one that is not a browser sends any Host
    it likes, so it must not reach the socket at all."""
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((this_machines_lan_address(), port), timeout=5).close()
