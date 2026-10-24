"""Tests for the Houdini connection manager."""

import logging
import socket
import threading
import time
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from houdini_mcp.connection import (
    HoudiniConnectionError,
    HoudiniTargetMismatch,
    Link,
    TransportFailure,
    get_connection_info,
    houdini,
    is_connected,
    ping,
    remote_houdini,
    transport_failure,
)
from tests.conftest import (
    MockHouModule,
    MockRpycConnection,
    link_failure,
    raised_inside_houdini,
    served_connection,
)


def installed_conn() -> Any:
    """The connection object of the link the process's owner has installed, or None."""
    link = houdini().link()
    return None if link is None else link.conn


class TestConnect:
    """Tests for the connect function."""

    def test_connect_success(self, mock_rpyc_with_reset, mock_hou):
        """Test successful connection to Houdini."""
        link = houdini().connect("localhost", 18811)

        assert link.conn is not None
        assert link.hou is mock_hou
        assert link.hou.applicationVersionString() == "22.0.466"

    def test_connect_replaces_and_closes_the_previous_link(self, mock_rpyc_with_reset, mock_hou):
        """At most one link is open: a fresh connect closes the one it replaces."""
        first = houdini().connect("localhost", 18811)
        second = houdini().connect("localhost", 18811)

        assert second is not first
        assert first.conn.closed is True
        assert installed_conn() is second.conn

    def test_a_failed_connect_leaves_the_installed_link_standing(
        self, mock_rpyc_with_reset, mock_hou
    ):
        """The old link is closed only once the new one is proven, so a reconnect that
        fails costs nothing a concurrent caller was using."""
        first = houdini().connect("localhost", 18811)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            with pytest.raises(HoudiniConnectionError):
                houdini().connect("localhost", 18811, max_retries=1, retry_delay=0.01)

        assert first.conn.closed is False
        assert installed_conn() is first.conn

    def test_connect_with_retry_success_on_second_attempt(self, reset_connection_state, mock_hou):
        """Test connection retry logic succeeds on second attempt."""
        mock_conn = MockRpycConnection(mock_hou)

        call_count = [0]

        def mock_connect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("Connection refused")
            return mock_conn

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect = mock_connect
            link = houdini().connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert call_count[0] == 2  # Failed once, succeeded on retry
        assert link.hou is mock_hou

    def test_connect_does_not_retry_a_rejected_token(self, reset_connection_state):
        """A failure that is not the link itself fails at once, with its cause attached."""
        from houdini_mcp_handshake import HandshakeError

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = HandshakeError("listener closed on the token")

            with pytest.raises(HoudiniConnectionError) as exc_info:
                houdini().connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert mock_rpyc.connect.call_count == 1
        assert "Non-retryable" in str(exc_info.value)
        assert remote_houdini(None).remedy in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, HandshakeError)

    def test_a_link_that_fails_validation_is_closed_and_not_installed(
        self, reset_connection_state, mock_hou, caplog
    ):
        """Houdini answers the socket but not the import of ``hou``: the attempt is
        retried, the half-open link is closed, and nothing is left behind for
        is_connected() - or logged as connected."""
        busy = MockRpycConnection(mock_hou)
        busy.modules = MagicMock()  # a per-instance type, so the property stays here
        type(busy.modules).hou = PropertyMock(side_effect=TimeoutError("sync request"))

        with (
            patch("houdini_mcp.connection.handshake") as mock_rpyc,
            caplog.at_level(logging.INFO, logger="houdini_mcp.connection"),
        ):
            mock_rpyc.connect.return_value = busy
            with pytest.raises(HoudiniConnectionError):
                houdini().connect("localhost", 18811, max_retries=2, retry_delay=0.01)

        assert mock_rpyc.connect.call_count == 2
        assert busy.closed is True
        assert is_connected() is False
        assert installed_conn() is None
        assert "Connected to Houdini at localhost:18811" not in caplog.messages

    def test_a_proven_link_is_logged_by_the_houdini_it_reached(
        self, reset_connection_state, mock_hou, caplog
    ):
        """A connect that succeeds says so, naming its target, apart from the
        attempt the opener announced."""
        with (
            patch("houdini_mcp.connection.handshake") as mock_rpyc,
            caplog.at_level(logging.INFO, logger="houdini_mcp.connection"),
        ):
            mock_rpyc.connect.return_value = MockRpycConnection(mock_hou)
            houdini().connect("render-farm", 19999, max_retries=1)

        assert "Connected to Houdini at render-farm:19999" in caplog.messages

    def test_a_close_that_fails_does_not_replace_the_validation_error(
        self, reset_connection_state, mock_hou
    ):
        busy = MockRpycConnection(mock_hou)
        busy.modules = MagicMock()  # a per-instance type, so the property stays here
        type(busy.modules).hou = PropertyMock(side_effect=TimeoutError("sync request"))
        busy.close = MagicMock(side_effect=ValueError("half-torn channel"))

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = busy
            with pytest.raises(HoudiniConnectionError) as exc_info:
                houdini().connect("localhost", 18811, max_retries=1, retry_delay=0.01)

        assert "sync request" in str(exc_info.value)
        assert "half-torn" not in str(exc_info.value)

    @pytest.mark.parametrize(
        "exc",
        [
            OSError(101, "Network is unreachable"),
            socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
            PermissionError("socket denied"),
            TimeoutError("listener did not acknowledge the token"),
            EOFError("peer closed the connection during the handshake"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_connect_retries_whatever_the_socket_layer_raises(
        self, reset_connection_state, mock_hou, exc
    ):
        """Before the link exists, every OSError is the link's: a Houdini still booting
        or a laptop rejoining the network gets the retry, not a bad-path verdict."""
        mock_conn = MockRpycConnection(mock_hou)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = [exc, mock_conn]
            link = houdini().connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert mock_rpyc.connect.call_count == 2
        assert link.hou is mock_hou

    def test_connect_all_retries_fail(self, reset_connection_state):
        """Test connection failure after all retries exhausted."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")

            with pytest.raises(HoudiniConnectionError) as exc_info:
                houdini().connect("localhost", 18811, max_retries=2, retry_delay=0.01)

        assert "Failed to connect" in str(exc_info.value)
        assert "after 2 attempts" in str(exc_info.value)
        assert remote_houdini(None).remedy in str(exc_info.value)

    def test_connect_rejects_fewer_than_one_attempt(self, reset_connection_state):
        """Zero attempts is refused outright, never reported as an unreachable Houdini."""
        with (
            patch("houdini_mcp.connection.handshake") as mock_handshake,
            pytest.raises(ValueError, match="at least 1"),
        ):
            houdini().connect("localhost", 18811, max_retries=0, retry_delay=0.01)
        assert mock_handshake.mock_calls == []

    def test_connect_exponential_backoff(self, reset_connection_state, mock_hou):
        """Test that retry delay doubles each attempt (exponential backoff)."""
        import time

        mock_conn = MockRpycConnection(mock_hou)
        call_times = []

        def mock_connect(*args, **kwargs):
            call_times.append(time.time())
            if len(call_times) < 3:
                raise ConnectionError("Connection refused")
            return mock_conn

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect = mock_connect
            houdini().connect("localhost", 18811, max_retries=3, retry_delay=0.05, jitter=False)

        assert len(call_times) == 3
        # Second delay should be ~2x the first
        delay1 = call_times[1] - call_times[0]
        delay2 = call_times[2] - call_times[1]
        assert delay2 > delay1 * 1.5  # Allow some tolerance

    def test_connect_with_jitter(self, reset_connection_state, mock_hou):
        """Test that jitter adds randomness to retry delays."""
        import time

        mock_conn = MockRpycConnection(mock_hou)
        delays = []

        def mock_connect(*args, **kwargs):
            delays.append(time.time())
            if len(delays) < 3:
                raise ConnectionError("Connection refused")
            return mock_conn

        # Run multiple times to check jitter introduces variance
        all_delay_diffs = []
        for _ in range(3):
            delays.clear()
            with patch("houdini_mcp.connection.handshake") as mock_rpyc:
                mock_rpyc.connect = mock_connect
                houdini().connect("localhost", 18811, max_retries=3, retry_delay=0.1, jitter=True)

            if len(delays) >= 2:
                all_delay_diffs.append(delays[1] - delays[0])

        # With jitter, delays should vary slightly between runs
        # Check that we got some variance (not all identical)
        if len(all_delay_diffs) >= 2:
            # Just verify the mechanism works - delays should be > base_delay
            assert all(d >= 0.09 for d in all_delay_diffs)  # Allow small timing variance

    def test_connect_custom_host_port(self, reset_connection_state):
        """Test connect with custom host and port."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = MockRpycConnection(MockHouModule())

            link = houdini().connect("192.168.1.100", 19999)

            assert mock_rpyc.connect.call_args.args == ("192.168.1.100", 19999)
        assert (link.host, link.port) == ("192.168.1.100", 19999)
        assert link.name == "192.168.1.100:19999"


class TestNoHoudiniInstalled:
    def test_a_process_that_installs_no_houdini_reaches_none(self) -> None:
        """[LAW:no-silent-failure] a server whose entry point installed no Houdini is told
        so at its first connect, instead of reaching a listener presenting no token."""
        import subprocess
        import sys

        probe = (
            "from houdini_mcp import connection\n"
            "from houdini_mcp.tools._common import using\n"
            "try:\n"
            "    with using('houdini-box', 18811):\n"
            "        pass\n"
            "except connection.HoudiniConnectionError as e:\n"
            "    print(e)\n"
        )
        told = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        ).stdout
        assert "no Houdini is installed" in told
        assert "use_houdini" in told


class TestIsConnected:
    """Tests for the is_connected function."""

    def test_is_connected_true(self, mock_connection):
        """Test is_connected returns True when connected."""
        result = is_connected()
        assert result is True

    def test_is_connected_false_no_connection(self, reset_connection_state):
        """Test is_connected returns False when no connection."""
        result = is_connected()
        assert result is False

    def test_is_connected_false_when_the_link_closed(self, mock_connection):
        """A link whose socket closed under it is not a connection."""
        installed_conn().close()

        assert is_connected() is False

    def test_is_connected_is_a_query_and_does_not_tear_down(self, mock_connection):
        """Finding the link dead changes nothing: the installed connection is still
        there to be read, and the same answer comes back twice. Teardown belongs to
        ``close`` and to the reconnect that ``ensure`` owns."""
        conn = installed_conn()
        conn.close()

        assert is_connected() is False
        assert is_connected() is False
        assert installed_conn() is conn

    def test_is_connected_makes_no_rpc_call(self, connected_to):
        """is_connected reads socket state; it never asks Houdini anything."""
        hou = connected_to(MagicMock())
        hou.reset_mock()

        assert is_connected() is True
        assert hou.method_calls == []


class TestClose:
    """Tests for the owner's close: dropping whatever link is installed, as use() does."""

    def test_close_closes_and_drops_the_link(self, mock_connection):
        conn = installed_conn()

        houdini().close()

        assert conn.closed is True
        assert is_connected() is False
        assert installed_conn() is None

    def test_close_handles_close_error(self, mock_connection):
        """A close that raises still drops the link; the error is logged, not raised."""
        installed_conn().close = MagicMock(side_effect=Exception("Close failed"))

        houdini().close()

        assert installed_conn() is None

    def test_close_when_not_connected(self, reset_connection_state):
        """Closing with nothing installed does nothing."""
        houdini().close()

        assert installed_conn() is None


class TestCutOff:
    """A link ended under a request waiting on it, as execute_code's timeout ends its
    program's."""

    def test_a_request_waiting_on_a_link_cut_off_fails_at_once(self, reset_connection_state):
        """Closed under it, the waiting request slept until its 30s sync timeout on
        macOS. The pause lets the request reach its wait; sent after the cut-off, it
        fails at once too."""
        with served_connection("import time") as conn:
            link = Link("127.0.0.1", 0, conn, hou=None)
            failed: list[BaseException] = []

            def waits() -> None:
                try:
                    conn.modules.time.sleep(4)
                except BaseException as e:  # noqa: BLE001
                    failed.append(e)

            waiting = threading.Thread(target=waits)
            waiting.start()
            time.sleep(0.5)

            houdini().cut_off(link)
            waiting.join(3)

            assert not waiting.is_alive()
            assert [type(e) for e in failed] == [EOFError]
            # rpyc closed it, from the thread that was waiting
            assert conn.closed

    def test_a_cut_off_link_is_uninstalled(self, mock_connection):
        """It reads as open until its user closes it, so the next call opens another."""
        link = houdini().link()

        houdini().cut_off(link)

        assert houdini().link() is None


class TestEnsure:
    """Tests for the owner's ensure: the link a call's scope holds."""

    def test_ensure_already_connected(self, mock_connection, mock_hou):
        """Test ensure returns hou when already connected."""
        hou = houdini().ensure("localhost", 18811).hou
        assert hou is not None
        assert hou.applicationVersionString() == "22.0.466"

    def test_ensure_reconnects(self, mock_rpyc_with_reset):
        """Test ensure reconnects when disconnected."""
        hou = houdini().ensure("localhost", 18811).hou
        assert hou is mock_rpyc_with_reset

    def test_ensure_uses_provided_host_port(self, reset_connection_state):
        """Test ensure uses provided host and port."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = MockRpycConnection(MockHouModule())

            houdini().ensure("custom-host", 12345)

            assert mock_rpyc.connect.call_args.args == ("custom-host", 12345)

    def test_ensure_reaches_the_houdini_it_names_or_fails(self, mock_connection):
        """A live link to one Houdini is not an answer to a call naming another: the
        call fails naming both, and the link it did not ask for is left standing."""
        with pytest.raises(HoudiniTargetMismatch) as exc_info:
            houdini().ensure("render-farm", 19999)

        assert (exc_info.value.requested, exc_info.value.connected) == (
            "render-farm:19999",
            "localhost:18811",
        )
        assert is_connected() is True
        assert houdini().ensure("localhost", 18811).hou is mock_connection

    def test_ensure_reconnects_after_the_link_closed(self, mock_rpyc_with_reset):
        """A link found closed is replaced, once, and the new hou is handed out."""
        houdini().ensure("localhost", 18811)
        first = installed_conn()
        first.close()

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = MockRpycConnection(mock_rpyc_with_reset)
            hou = houdini().ensure("localhost", 18811).hou

        assert hou is mock_rpyc_with_reset
        assert mock_rpyc.connect.call_count == 1
        assert installed_conn() is not first
        assert is_connected() is True

    def test_a_reconnect_is_logged_connected_after_the_lost_link_disconnected(
        self, mock_rpyc_with_reset, caplog
    ):
        """The last word on a target is the link the server now holds, not the
        close of the one it replaced."""
        houdini().ensure("localhost", 18811)
        installed_conn().close()
        caplog.clear()

        with (
            patch("houdini_mcp.connection.handshake") as mock_rpyc,
            caplog.at_level(logging.INFO, logger="houdini_mcp.connection"),
        ):
            mock_rpyc.connect.return_value = MockRpycConnection(mock_rpyc_with_reset)
            houdini().ensure("localhost", 18811)

        link_events = [
            m for m in caplog.messages if m.startswith(("Connected to", "Disconnected from"))
        ]
        assert link_events == [
            "Disconnected from Houdini at localhost:18811",
            "Connected to Houdini at localhost:18811",
        ]

    def test_concurrent_callers_share_one_reconnect(self, reset_connection_state, mock_hou):
        """Eight tool calls arrive with no link: one connection is opened, and every
        caller gets the hou it exposes. Reconnects are serialized by the owner, not
        raced by whoever noticed the gap first."""
        opened: list[MockRpycConnection] = []
        go = threading.Barrier(8)

        def slow_open(*_args, **_kwargs):
            conn = MockRpycConnection(mock_hou)
            opened.append(conn)
            return conn

        results: list[Any] = []

        def call():
            go.wait()
            results.append(houdini().ensure("localhost", 18811).hou)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = slow_open
            threads = [threading.Thread(target=call) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(opened) == 1
        assert results == [mock_hou] * 8
        assert installed_conn() is opened[0]


class TestGetConnectionInfo:
    """Tests for the get_connection_info function."""

    def test_get_connection_info_connected(self, mock_connection, mock_hou):
        """Test get_connection_info when connected."""
        info = get_connection_info("localhost", 18811)

        assert info["connected"] is True
        assert info["houdini_version"] == "22.0.466"
        assert info["houdini_build"]["major"] == 22
        assert info["houdini_build"]["minor"] == 0
        assert info["houdini_build"]["build"] == 466
        assert info["hip_file"] == "/path/to/test.hip"

    def test_get_connection_info_disconnected(self, reset_connection_state):
        """Test get_connection_info when disconnected."""
        info = get_connection_info("localhost", 18811)

        assert info["connected"] is False
        assert info["target"] == "localhost:18811"
        assert info["houdini_version"] is None

    def test_get_connection_info_reports_one_record_shape_in_both_arms(
        self, mock_connection, reset_connection_state
    ):
        """Connected or not, the report has the same fields; only their values say
        which arm it is, so a reader never meets a field that one arm lacks."""
        connected = get_connection_info("localhost", 18811)
        houdini().close()
        disconnected = get_connection_info("localhost", 18811)

        assert (connected["connected"], disconnected["connected"]) == (True, False)
        assert set(connected) == set(disconnected)

    def test_get_connection_info_raises_when_the_link_cannot_answer(self, mock_connection):
        """A link that cannot report its version is a failure, not a success
        carrying an ``error`` field; the tool boundary classifies the raise."""
        mock_connection.applicationVersionString = MagicMock(side_effect=Exception("Error"))

        with pytest.raises(Exception, match="Error"):
            get_connection_info("localhost", 18811)

    def test_get_connection_info_reports_the_houdini_reached_not_the_one_asked_about(
        self, reset_connection_state
    ):
        """Connected to one Houdini, asked about another: the info describes the link
        that exists, so it never labels a connection with an address it did not use."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = MockRpycConnection(MockHouModule())
            houdini().ensure("render-farm", 19999)

        info = get_connection_info("localhost", 18811)

        assert info["connected"] is True
        assert info["target"] == "render-farm:19999"


class TestPing:
    """Tests for the ping function."""

    def test_ping_success(self, mock_hou):
        """A reachable Houdini answers the probe with the record of the Houdini
        reached: the target the probe used, its version, build and hip file."""
        mock_conn = MockRpycConnection(mock_hou)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            result = ping("localhost", 18811)

        assert set(result) == {"target", "houdini_version", "houdini_build", "hip_file"}
        assert result["target"] == "localhost:18811"
        assert result["houdini_version"] == mock_hou.applicationVersionString()
        assert result["houdini_build"]["major"] == mock_hou.applicationVersion()[0]
        assert result["hip_file"] == mock_hou.hipFile.path()
        assert mock_conn.closed is True  # Connection should be closed

    def test_ping_does_not_log_the_servers_link_changing(
        self, reset_connection_state, mock_hou, caplog
    ):
        """The probe's link is never the server's, so neither its open nor its close
        is announced as the server's."""
        with (
            patch("houdini_mcp.connection.handshake") as mock_rpyc,
            caplog.at_level(logging.INFO, logger="houdini_mcp.connection"),
        ):
            mock_rpyc.connect.return_value = MockRpycConnection(mock_hou)
            ping("localhost", 18811)

        assert "Connected to Houdini at localhost:18811" not in caplog.messages
        assert "Disconnected from Houdini at localhost:18811" not in caplog.messages

    def test_ping_bounds_its_requests_by_its_timeout(self, mock_hou):
        """The timeout the probe advertises is the sync bound of the connection it
        opens, so a Houdini that never answers fails the probe instead of hanging it."""
        seen: list[float] = []

        def connect(host: str, port: int, token: str | None, *, sync_request_timeout: float):
            seen.append(sync_request_timeout)
            return MockRpycConnection(mock_hou)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = connect
            ping("localhost", 18811, timeout=2.5)

        assert seen == [2.5]

    def test_ping_failure_when_houdini_cannot_answer_names_the_cause(self, mock_hou):
        """A listener that accepts the link but cannot report its version is not a
        reachable Houdini: the failure names the target and the cause, and the
        probe's link is closed."""
        mock_conn = MockRpycConnection(mock_hou)
        mock_hou.applicationVersionString = MagicMock(
            side_effect=link_failure(EOFError("stream closed"))
        )

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            with pytest.raises(HoudiniConnectionError) as raised:
                ping("localhost", 18811)

        assert raised.value.target == "localhost:18811"
        assert "stream closed" in str(raised.value)
        assert isinstance(raised.value.__cause__, EOFError)
        assert mock_conn.closed is True

    def test_ping_closes_a_link_it_could_not_prove(self, mock_hou):
        """A listener that accepts the link but exposes no ``hou`` is not a reachable
        Houdini, and the socket the probe opened does not outlive the failed proof."""
        mock_conn = MockRpycConnection(mock_hou)
        mock_conn.modules = MagicMock()  # a per-instance type, so the property stays here
        type(mock_conn.modules).hou = PropertyMock(side_effect=EOFError("listener went away"))

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            with pytest.raises(HoudiniConnectionError, match="listener went away"):
                ping("localhost", 18811)

        assert mock_conn.closed is True

    def test_ping_lets_the_reached_houdinis_own_fault_leave_as_itself(self, mock_hou):
        """A proven link whose Houdini raises while answering is not an unreached
        Houdini: the fault leaves as itself for the boundary to classify, and the
        probe's link is still closed."""
        mock_conn = MockRpycConnection(mock_hou)
        mock_hou.hipFile.path = MagicMock(side_effect=RuntimeError("hip path unavailable"))

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            with pytest.raises(RuntimeError, match="hip path unavailable"):
                ping("localhost", 18811)

        assert mock_conn.closed is True

    def test_ping_reports_a_link_that_fails_while_answering_as_unanswered(self, mock_hou):
        """A proven link that drops while answering is the probe's own "no answer",
        naming the target and the cause, not a loss of the server's connection."""
        mock_conn = MockRpycConnection(mock_hou)
        mock_hou.hipFile.path = MagicMock(side_effect=link_failure(EOFError("stream closed")))

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            with pytest.raises(HoudiniConnectionError) as raised:
                ping("localhost", 18811)

        assert raised.value.target == "localhost:18811"
        assert remote_houdini(None).remedy in str(raised.value)
        assert isinstance(raised.value.__cause__, EOFError)
        assert mock_conn.closed is True

    def test_ping_failure_names_the_target_and_the_cause(self):
        """An unreachable Houdini is a failure that says where and why, not False."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            with pytest.raises(HoudiniConnectionError) as raised:
                ping("localhost", 18811)

        assert raised.value.target == "localhost:18811"
        assert "Connection refused" in str(raised.value)
        assert isinstance(raised.value.__cause__, ConnectionError)

    def test_ping_closes_connection(self, mock_hou):
        """Test ping closes connection after checking."""
        mock_conn = MockRpycConnection(mock_hou)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            ping("localhost", 18811)

        assert mock_conn.closed is True


class TestHoudiniConnectionError:
    """Tests for the HoudiniConnectionError exception."""

    def test_exception_message(self):
        """Test exception contains useful message."""
        error = HoudiniConnectionError("Test error message", target="localhost:18811")
        assert str(error) == "Test error message"
        assert error.target == "localhost:18811"

    def test_exception_inheritance(self):
        """Test exception inherits from Exception."""
        error = HoudiniConnectionError("Test", target="localhost:18811")
        assert isinstance(error, Exception)


class TestTransportFailure:
    """Only a failure of the link itself is connection loss."""

    @pytest.mark.parametrize(
        "exc",
        [
            link_failure(EOFError("stream has been closed")),
            link_failure(EOFError("[Errno 54] Connection reset by peer")),
            link_failure(TimeoutError("sync request timed out")),
        ],
        ids=lambda e: f"{type(e).__name__}:{e}",
    )
    def test_link_failures_are_transport_failures(self, exc):
        failure = transport_failure(exc)
        assert isinstance(failure, TransportFailure)
        assert failure.exc is exc

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("/renders/missing/frame.exr"),
            PermissionError("/renders/locked"),
            IsADirectoryError("/renders"),
            OSError("Network is unreachable"),
            # rpyc wraps a reset it meets into EOFError; a bare one came from local code
            ConnectionResetError("reset by peer"),
            BrokenPipeError("broken pipe"),
            RuntimeError("connection to input 0 was reset"),
            ValueError("timeout must be positive"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_file_permission_and_operation_errors_are_not(self, exc):
        assert transport_failure(exc) is None

    @pytest.mark.parametrize(
        "exc",
        [EOFError("no more input"), TimeoutError("my own deadline")],
        ids=lambda e: type(e).__name__,
    )
    def test_the_same_types_raised_in_this_process_are_not(self, exc):
        """A program execute_code runs here, or a disk write, raises these types itself."""
        try:
            raise exc
        except type(exc) as raised:
            assert transport_failure(raised) is None

    @pytest.mark.parametrize(
        "exc",
        [TimeoutError("cook timed out"), ConnectionResetError("render farm reset")],
        ids=lambda e: type(e).__name__,
    )
    def test_an_exception_houdini_raised_is_not_a_link_failure(self, exc):
        remote = raised_inside_houdini(exc)
        assert isinstance(remote, type(exc))  # same class on arrival...
        assert transport_failure(remote) is None  # ...but the link is intact


def _owner() -> Any:
    """An owner whose every connect opens a fresh link to a fresh ``hou`` double."""
    from houdini_mcp.connection import HoudiniConnection, HoudiniTarget

    def open_fresh(host: str, port: int, sync_timeout: float) -> MockRpycConnection:
        return MockRpycConnection(MockHouModule())

    return HoudiniConnection(HoudiniTarget(open_fresh, "start a fresh double"))


class TestATeardownDropsTheLinkThatFailed:
    """[LAW:no-ambient-temporal-coupling] a call's late failure drops the link that
    call used, never one another call has installed since (houdini-laws-etg.gxj)."""

    def test_a_late_failure_on_a_replaced_link_leaves_the_replacement_standing(self, caplog):
        from houdini_mcp.tools._common import held_link
        from houdini_mcp.tools.failure import LinkLost

        owner = _owner()
        with (
            caplog.at_level(logging.INFO, logger="houdini_mcp.connection"),
            pytest.raises(LinkLost) as lost,
            held_link(owner, "localhost", 18811) as first,
        ):
            first.conn.close()  # A's link fails...
            second = owner.ensure("localhost", 18811)  # ...B reconnects...
            raise link_failure(EOFError("stream closed"))  # ...and A's failure arrives late.

        assert lost.value.link is first
        assert owner.link() is second and second.alive
        assert not first.alive
        assert "Dropped failed link to Houdini at localhost:18811 (not installed)" in (
            caplog.messages
        )

    def test_a_failure_on_the_installed_link_uninstalls_it_and_the_next_call_reconnects(
        self, caplog
    ):
        from houdini_mcp.tools._common import held_link
        from houdini_mcp.tools.failure import LinkLost

        owner = _owner()
        with (
            caplog.at_level(logging.INFO, logger="houdini_mcp.connection"),
            pytest.raises(LinkLost) as lost,
            held_link(owner, "localhost", 18811),
        ):
            raise link_failure(TimeoutError("no answer"))

        assert owner.link() is None and not lost.value.link.alive
        assert isinstance(lost.value.lost.exc, TimeoutError)
        assert "Dropped failed link to Houdini at localhost:18811 (was installed)" in (
            caplog.messages
        )
        assert owner.ensure("localhost", 18811) is not lost.value.link

    def test_a_link_failure_keeps_the_notes_it_gathered_on_its_way_out(self):
        """A recovery's note (where the unsaved work was backed up) still reaches the
        caller when the failure it rides on turns out to be the link's."""
        from houdini_mcp.tools._common import held_link
        from houdini_mcp.tools.failure import add_note, failure_for

        owner = _owner()
        with pytest.raises(Exception) as lost, held_link(owner, "localhost", 18811):
            closed = link_failure(EOFError("stream closed"))
            add_note(closed, "The scene's unsaved work is backed up at /tmp/backup.hip.")
            raise closed

        failure = failure_for(lost.value, "load_scene")
        assert failure.kind == "connection_lost"
        assert failure.message.endswith(
            "\nThe scene's unsaved work is backed up at /tmp/backup.hip."
        )

    def test_a_tool_failure_around_a_lost_link_keeps_its_payload(self):
        """``raise OperationFailed(..., path=...) from <the link's failure>`` reads as the
        link's failure, carrying the tool's fields and, as a note, its message."""
        from houdini_mcp.tools._common import held_link
        from houdini_mcp.tools.failure import OperationFailed, failure_for

        owner = _owner()
        with pytest.raises(Exception) as lost, held_link(owner, "localhost", 18811):
            raise OperationFailed(
                "Failed to navigate to /obj/geo1", path="/obj/geo1"
            ) from link_failure(EOFError("stream closed"))

        failure = failure_for(lost.value, "capture_pane_screenshot")
        assert (failure.kind, failure.path) == ("connection_lost", "/obj/geo1")
        assert failure.message.endswith("\nFailed to navigate to /obj/geo1")

    def test_a_lost_link_judged_again_by_an_outer_scope_is_told_once(self):
        """A scope inside another (a checkpointed tool's body) already judged the loss;
        the outer scope passes it on as it is, its fields and notes told once."""
        from houdini_mcp.tools._common import held_link
        from houdini_mcp.tools.failure import OperationFailed, add_note, failure_for

        owner = _owner()
        with (
            pytest.raises(Exception) as lost,
            held_link(owner, "localhost", 18811),
            held_link(owner, "localhost", 18811),
        ):
            failure = OperationFailed("SceneViewer not framed", pane_type="SceneViewer")
            add_note(failure, "The scene's unsaved work is backed up at /tmp/backup.hip.")
            raise failure from link_failure(EOFError("stream closed"))

        told = failure_for(lost.value, "capture_pane_screenshot")
        assert (told.kind, told.pane_type) == ("connection_lost", "SceneViewer")
        assert told.message.count("SceneViewer not framed") == 1
        assert told.message.count("backed up at /tmp/backup.hip") == 1

    def test_a_tool_failure_raised_from_a_judged_loss_adds_its_payload(self):
        """execute_code's worker judges its program's failure on the link; the call
        then raises its own failure, with what the program left, from that loss."""
        from houdini_mcp.tools._common import held_link
        from houdini_mcp.tools.failure import OperationFailed, failure_for, link_lost

        owner = _owner()
        with pytest.raises(Exception) as lost, held_link(owner, "localhost", 18811) as link:
            judged = link_lost(owner, link, link_failure(TimeoutError("result expired")))
            assert judged is not None and not link.alive
            raise OperationFailed("TimeoutError: result expired", stdout="before\n") from judged

        failure = failure_for(lost.value, "execute_code")
        assert (failure.kind, failure.connection_reset) == ("timeout", True)
        assert failure.stdout == "before\n"
        assert failure.message.count("TimeoutError: result expired") == 1

    def test_a_tool_field_the_link_arm_declares_is_refused_as_the_bug_it_is(self):
        from houdini_mcp.tools._common import held_link
        from houdini_mcp.tools.failure import OperationFailed, failure_for

        owner = _owner()
        with pytest.raises(TypeError) as refused, held_link(owner, "localhost", 18811):
            raise OperationFailed("Render failed", exception="EOFError") from link_failure(
                EOFError("stream closed")
            )

        assert "exception" in str(refused.value)
        assert failure_for(refused.value, "render_viewport").kind == "operation_failed"

    def test_a_failure_that_is_not_the_links_drops_nothing(self):
        from houdini_mcp.tools._common import held_link

        owner = _owner()
        refused = ValueError("not the link's")
        with pytest.raises(ValueError) as raised, held_link(owner, "localhost", 18811) as link:
            raise refused

        assert raised.value is refused
        assert owner.link() is link and link.alive

    def test_a_reconnect_that_failed_inside_a_scope_is_unavailable_and_drops_nothing(self):
        """An inner reconnect raising HoudiniConnectionError from EOFError is the open's
        failure, not the held link's: it passes the outer scope as itself."""
        from houdini_mcp.tools._common import held_link
        from houdini_mcp.tools.failure import failure_for

        owner = _owner()
        with (
            pytest.raises(HoudiniConnectionError) as raised,
            held_link(owner, "localhost", 18811) as link,
        ):
            raise HoudiniConnectionError(
                "no answer reopening", target="localhost:18811"
            ) from EOFError("closed")

        assert owner.link() is link and link.alive
        failure = failure_for(raised.value, "get_scene_info")
        assert (failure.kind, failure.target) == ("connection_unavailable", "localhost:18811")

    def test_a_raw_link_failure_outside_any_scope_is_reported_as_the_bug_it_is(self, caplog):
        from houdini_mcp.tools.failure import failure_for

        with caplog.at_level(logging.ERROR, logger="houdini_mcp.tools"):
            failure = failure_for(EOFError("stream closed"), "get_scene_info")

        assert (failure.kind, failure.exception) == ("operation_failed", "EOFError")
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_a_wrapped_link_failure_outside_any_scope_is_the_bug_it_is_not_the_tools_label(
        self, caplog
    ):
        from houdini_mcp.tools._common import OperationFailed
        from houdini_mcp.tools.failure import failure_for

        try:
            raise OperationFailed("Failed to enumerate panes") from link_failure(
                EOFError("stream closed")
            )
        except OperationFailed as wrapped:
            with caplog.at_level(logging.ERROR, logger="houdini_mcp.tools"):
                failure = failure_for(wrapped, "list_visible_panes")

        assert failure.kind == "operation_failed"
        assert "EOFError: stream closed" in failure.traceback
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_a_tool_whose_link_failed_after_a_reconnect_leaves_the_new_link_standing(
        self, mock_connection, monkeypatch
    ):
        from houdini_mcp.tools import get_scene_info

        replacement: list[Any] = []

        def lost_after_another_call_reconnected() -> str:
            installed_conn().close()
            with patch("houdini_mcp.connection.handshake") as seam:
                seam.connect.return_value = MockRpycConnection(MockHouModule())
                replacement.append(houdini().ensure("localhost", 18811))
            raise link_failure(EOFError("stream closed"))

        monkeypatch.setattr(mock_connection.hipFile, "path", lost_after_another_call_reconnected)

        result = get_scene_info(host="localhost", port=18811)

        assert (result["kind"], result["retry"]) == ("connection_lost", "call_again")
        assert houdini().link() is replacement[0] and replacement[0].alive


def test_a_link_is_reached_only_through_a_scope():
    """The removed acquisitions stay removed: ``using`` is the one way in."""
    from houdini_mcp import connection

    removed = ("ensure_connected", "ensure_link", "connect", "get_connection", "disconnect")
    assert [name for name in removed if hasattr(connection, name)] == []
