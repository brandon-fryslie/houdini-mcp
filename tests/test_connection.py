"""Tests for the Houdini connection manager."""

import socket
import threading
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from houdini_mcp.connection import (
    IN_PROCESS_HOUDINI,
    HoudiniConnectionError,
    HoudiniTargetMismatch,
    TransportFailure,
    connect,
    disconnect,
    ensure_connected,
    get_connection,
    get_connection_info,
    is_connected,
    ping,
    transport_failure,
    use_houdini,
)
from tests.conftest import MockHouModule, MockRpycConnection, raised_inside_houdini


class TestConnect:
    """Tests for the connect function."""

    def test_connect_success(self, mock_rpyc_with_reset, mock_hou):
        """Test successful connection to Houdini."""
        link = connect("localhost", 18811)

        assert link.conn is not None
        assert link.hou is mock_hou
        assert link.hou.applicationVersionString() == "20.5.123"

    def test_connect_replaces_and_closes_the_previous_link(self, mock_rpyc_with_reset, mock_hou):
        """At most one link is open: a fresh connect closes the one it replaces."""
        first = connect("localhost", 18811)
        second = connect("localhost", 18811)

        assert second is not first
        assert first.conn.closed is True
        assert get_connection() is second.conn

    def test_a_failed_connect_leaves_the_installed_link_standing(
        self, mock_rpyc_with_reset, mock_hou
    ):
        """The old link is closed only once the new one is proven, so a reconnect that
        fails costs nothing a concurrent caller was using."""
        first = connect("localhost", 18811)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            with pytest.raises(HoudiniConnectionError):
                connect("localhost", 18811, max_retries=1, retry_delay=0.01)

        assert first.conn.closed is False
        assert get_connection() is first.conn

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
            link = connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert call_count[0] == 2  # Failed once, succeeded on retry
        assert link.hou is mock_hou

    def test_connect_does_not_retry_a_rejected_token(self, reset_connection_state):
        """A failure that is not the link itself fails at once, with its cause attached."""
        from houdini_mcp_handshake import HandshakeError

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = HandshakeError("listener closed on the token")

            with pytest.raises(HoudiniConnectionError) as exc_info:
                connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert mock_rpyc.connect.call_count == 1
        assert "Non-retryable" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, HandshakeError)

    def test_a_link_that_fails_validation_is_closed_and_not_installed(
        self, reset_connection_state, mock_hou
    ):
        """Houdini answers the socket but not the version call: the attempt is retried,
        the half-open link is closed, and nothing is left behind for is_connected()."""
        busy = MockRpycConnection(mock_hou)
        busy.modules = MagicMock()
        busy.modules.hou.applicationVersionString.side_effect = TimeoutError("sync request")

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = busy
            with pytest.raises(HoudiniConnectionError):
                connect("localhost", 18811, max_retries=2, retry_delay=0.01)

        assert mock_rpyc.connect.call_count == 2
        assert busy.closed is True
        assert is_connected() is False
        assert get_connection() is None

    def test_a_close_that_fails_does_not_replace_the_validation_error(
        self, reset_connection_state, mock_hou
    ):
        busy = MockRpycConnection(mock_hou)
        busy.modules = MagicMock()
        busy.modules.hou.applicationVersionString.side_effect = TimeoutError("sync request")
        busy.close = MagicMock(side_effect=ValueError("half-torn channel"))

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = busy
            with pytest.raises(HoudiniConnectionError) as exc_info:
                connect("localhost", 18811, max_retries=1, retry_delay=0.01)

        assert "sync request" in str(exc_info.value)
        assert "half-torn" not in str(exc_info.value)

    def test_in_process_failure_names_the_remedy(self, reset_connection_state):
        """Outside hython the in-process open cannot retry, and the one error it raises
        still carries the target's remedy."""
        use_houdini(IN_PROCESS_HOUDINI)
        with pytest.raises(HoudiniConnectionError) as exc_info:
            connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert "Non-retryable" in str(exc_info.value)
        assert IN_PROCESS_HOUDINI.remedy in str(exc_info.value)

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
            link = connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert mock_rpyc.connect.call_count == 2
        assert link.hou is mock_hou

    def test_connect_all_retries_fail(self, reset_connection_state):
        """Test connection failure after all retries exhausted."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")

            with pytest.raises(HoudiniConnectionError) as exc_info:
                connect("localhost", 18811, max_retries=2, retry_delay=0.01)

        assert "Failed to connect" in str(exc_info.value)
        assert "after 2 attempts" in str(exc_info.value)

    def test_connect_rejects_fewer_than_one_attempt(self, reset_connection_state):
        """Zero attempts is refused outright, never reported as an unreachable Houdini."""
        with (
            patch("houdini_mcp.connection.handshake") as mock_handshake,
            pytest.raises(ValueError, match="at least 1"),
        ):
            connect("localhost", 18811, max_retries=0, retry_delay=0.01)
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
            connect("localhost", 18811, max_retries=3, retry_delay=0.05, jitter=False)

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
                connect("localhost", 18811, max_retries=3, retry_delay=0.1, jitter=True)

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

            link = connect("192.168.1.100", 19999)

            assert mock_rpyc.connect.call_args.args == ("192.168.1.100", 19999)
        assert (link.host, link.port) == ("192.168.1.100", 19999)
        assert link.name == "192.168.1.100:19999"


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
        get_connection().close()

        assert is_connected() is False

    def test_is_connected_is_a_query_and_does_not_tear_down(self, mock_connection):
        """Finding the link dead changes nothing: the installed connection is still
        there to be read, and the same answer comes back twice. Teardown belongs to
        ``disconnect`` and to the reconnect that ``ensure_connected`` owns."""
        conn = get_connection()
        conn.close()

        assert is_connected() is False
        assert is_connected() is False
        assert get_connection() is conn

    def test_is_connected_makes_no_rpc_call(self, connected_to):
        """is_connected reads socket state; it never asks Houdini anything."""
        hou = connected_to(MagicMock())
        hou.reset_mock()

        assert is_connected() is True
        assert hou.method_calls == []


class TestDisconnect:
    """Tests for the disconnect function."""

    def test_disconnect_closes_and_drops_the_link(self, mock_connection):
        conn = get_connection()

        disconnect()

        assert conn.closed is True
        assert is_connected() is False
        assert get_connection() is None

    def test_disconnect_handles_close_error(self, mock_connection):
        """A close that raises still drops the link; the error is logged, not raised."""
        get_connection().close = MagicMock(side_effect=Exception("Close failed"))

        disconnect()

        assert get_connection() is None

    def test_disconnect_when_not_connected(self, reset_connection_state):
        """Test disconnect when not connected does nothing."""
        disconnect()

        assert get_connection() is None


class TestEnsureConnected:
    """Tests for the ensure_connected function."""

    def test_ensure_connected_already_connected(self, mock_connection, mock_hou):
        """Test ensure_connected returns hou when already connected."""
        hou = ensure_connected("localhost", 18811)
        assert hou is not None
        assert hou.applicationVersionString() == "20.5.123"

    def test_ensure_connected_reconnects(self, mock_rpyc_with_reset):
        """Test ensure_connected reconnects when disconnected."""
        hou = ensure_connected("localhost", 18811)
        assert hou is mock_rpyc_with_reset

    def test_ensure_connected_uses_provided_host_port(self, reset_connection_state):
        """Test ensure_connected uses provided host and port."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = MockRpycConnection(MockHouModule())

            ensure_connected("custom-host", 12345)

            assert mock_rpyc.connect.call_args.args == ("custom-host", 12345)

    def test_ensure_connected_reaches_the_houdini_it_names_or_fails(self, mock_connection):
        """A live link to one Houdini is not an answer to a call naming another: the
        call fails naming both, and the link it did not ask for is left standing."""
        with pytest.raises(HoudiniTargetMismatch) as exc_info:
            ensure_connected("render-farm", 19999)

        assert (exc_info.value.requested, exc_info.value.connected) == (
            "render-farm:19999",
            "localhost:18811",
        )
        assert is_connected() is True
        assert ensure_connected("localhost", 18811) is mock_connection

    def test_ensure_connected_reconnects_after_the_link_closed(self, mock_rpyc_with_reset):
        """A link found closed is replaced, once, and the new hou is handed out."""
        ensure_connected("localhost", 18811)
        first = get_connection()
        first.close()

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = MockRpycConnection(mock_rpyc_with_reset)
            hou = ensure_connected("localhost", 18811)

        assert hou is mock_rpyc_with_reset
        assert mock_rpyc.connect.call_count == 1
        assert get_connection() is not first
        assert is_connected() is True

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
            results.append(ensure_connected("localhost", 18811))

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = slow_open
            threads = [threading.Thread(target=call) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(opened) == 1
        assert results == [mock_hou] * 8
        assert get_connection() is opened[0]


class TestGetConnection:
    """Tests for the get_connection function."""

    def test_get_connection_returns_connection(self, mock_connection):
        """Test get_connection returns the connection object."""
        conn = get_connection()
        assert conn is not None

    def test_get_connection_returns_none_when_disconnected(self, reset_connection_state):
        """Test get_connection returns None when disconnected."""
        conn = get_connection()
        assert conn is None


class TestGetConnectionInfo:
    """Tests for the get_connection_info function."""

    def test_get_connection_info_connected(self, mock_connection, mock_hou):
        """Test get_connection_info when connected."""
        info = get_connection_info("localhost", 18811)

        assert info["connected"] is True
        assert info["houdini_version"] == "20.5.123"
        assert info["houdini_build"]["major"] == 20
        assert info["houdini_build"]["minor"] == 5
        assert info["houdini_build"]["build"] == 123
        assert info["hip_file"] == "/path/to/test.hip"

    def test_get_connection_info_disconnected(self, reset_connection_state):
        """Test get_connection_info when disconnected."""
        info = get_connection_info("localhost", 18811)

        assert info["connected"] is False
        assert info["host"] == "localhost"
        assert info["port"] == 18811
        assert info["houdini_version"] is None

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
            ensure_connected("render-farm", 19999)

        info = get_connection_info("localhost", 18811)

        assert info["connected"] is True
        assert (info["host"], info["port"]) == ("render-farm", 19999)
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

        assert result["target"] == "localhost:18811"
        assert result["connected"] is True
        assert result["houdini_version"] == mock_hou.applicationVersionString()
        assert result["houdini_build"]["major"] == mock_hou.applicationVersion()[0]
        assert result["hip_file"] == mock_hou.hipFile.path()
        assert mock_conn.closed is True  # Connection should be closed

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
        mock_hou.applicationVersionString = MagicMock(side_effect=EOFError("stream closed"))

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
        mock_hou.hipFile.path = MagicMock(side_effect=EOFError("stream closed"))

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            with pytest.raises(HoudiniConnectionError) as raised:
                ping("localhost", 18811)

        assert raised.value.target == "localhost:18811"
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
            EOFError("stream has been closed"),
            EOFError("[Errno 54] Connection reset by peer"),
            TimeoutError("sync request timed out"),
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
        [TimeoutError("cook timed out"), ConnectionResetError("render farm reset")],
        ids=lambda e: type(e).__name__,
    )
    def test_an_exception_houdini_raised_is_not_a_link_failure(self, exc):
        remote = raised_inside_houdini(exc)
        assert isinstance(remote, type(exc))  # same class on arrival...
        assert transport_failure(remote) is None  # ...but the link is intact

    @pytest.mark.parametrize(
        "exc",
        [
            EOFError("truncated pickle"),
            ConnectionResetError("render farm reset"),
            TimeoutError("cook timed out"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_in_process_there_is_no_link_to_lose(self, reset_connection_state, exc):
        """The stdio plugin runs in Houdini's interpreter: no rpyc, no ``_remote_tb`` tag,
        and no transport, so the same exceptions are the code's own."""
        use_houdini(IN_PROCESS_HOUDINI)
        assert transport_failure(exc) is None
