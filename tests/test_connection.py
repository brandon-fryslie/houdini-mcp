"""Tests for the Houdini connection manager."""

import socket
from unittest.mock import MagicMock, patch

import pytest

from houdini_mcp.connection import (
    IN_PROCESS_HOUDINI,
    HoudiniConnectionError,
    HoudiniOperationError,
    TransportFailure,
    connect,
    disconnect,
    ensure_connected,
    get_connection,
    get_connection_info,
    get_hou,
    is_connected,
    ping,
    safe_execute,
    transport_failure,
)
from tests.conftest import raised_inside_houdini


class TestConnect:
    """Tests for the connect function."""

    def test_connect_success(self, mock_rpyc_with_reset, mock_hou):
        """Test successful connection to Houdini."""
        connection, hou = connect("localhost", 18811)

        assert connection is not None
        assert hou is not None
        assert hou.applicationVersionString() == "20.5.123"

    def test_connect_validates_version(self, mock_rpyc_with_reset, mock_hou):
        """Test that connect validates the Houdini version."""
        connection, hou = connect("localhost", 18811)

        # Verify version was checked
        assert hou.applicationVersionString() == "20.5.123"

    def test_connect_with_retry_success_on_second_attempt(self, reset_connection_state, mock_hou):
        """Test connection retry logic succeeds on second attempt."""
        from tests.conftest import MockRpycConnection

        mock_conn = MockRpycConnection(mock_hou)

        call_count = [0]

        def mock_connect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("Connection refused")
            return mock_conn

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect = mock_connect
            connection, hou = connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert call_count[0] == 2  # Failed once, succeeded on retry
        assert hou is not None

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
        from tests.conftest import MockRpycConnection

        busy = MockRpycConnection(mock_hou)
        busy.modules = MagicMock()
        busy.modules.hou.applicationVersionString.side_effect = TimeoutError("sync request")

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = busy
            with pytest.raises(HoudiniConnectionError):
                connect("localhost", 18811, max_retries=2, retry_delay=0.01)

        assert mock_rpyc.connect.call_count == 2
        assert busy._closed is True
        assert is_connected() is False
        assert get_connection() is None

    def test_a_close_that_fails_does_not_replace_the_validation_error(
        self, reset_connection_state, mock_hou
    ):
        from tests.conftest import MockRpycConnection

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

    def test_in_process_failure_names_the_remedy(self, reset_connection_state, monkeypatch):
        """Outside hython the in-process open cannot retry, and the one error it raises
        still carries the target's remedy."""
        import houdini_mcp.connection as conn_module

        monkeypatch.setattr(conn_module, "_target", IN_PROCESS_HOUDINI)
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
        from tests.conftest import MockRpycConnection

        mock_conn = MockRpycConnection(mock_hou)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = [exc, mock_conn]
            _, hou = connect("localhost", 18811, max_retries=3, retry_delay=0.01)

        assert mock_rpyc.connect.call_count == 2
        assert hou is not None

    def test_connect_all_retries_fail(self, reset_connection_state):
        """Test connection failure after all retries exhausted."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")

            with pytest.raises(HoudiniConnectionError) as exc_info:
                connect("localhost", 18811, max_retries=2, retry_delay=0.01)

        assert "Failed to connect" in str(exc_info.value)
        assert "after 2 attempts" in str(exc_info.value)

    def test_connect_exponential_backoff(self, reset_connection_state, mock_hou):
        """Test that retry delay doubles each attempt (exponential backoff)."""
        import time

        from tests.conftest import MockRpycConnection

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

        from tests.conftest import MockRpycConnection

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

    def test_connect_custom_host_port(self, mock_rpyc_with_reset):
        """Test connect with custom host and port."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            from tests.conftest import MockHouModule, MockRpycConnection

            mock_hou = MockHouModule()
            mock_conn = MockRpycConnection(mock_hou)
            mock_rpyc.connect.return_value = mock_conn

            connect("192.168.1.100", 19999)

            assert mock_rpyc.connect.call_args.args == ("192.168.1.100", 19999)


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

    def test_is_connected_false_dead_connection(self, reset_connection_state):
        """Test is_connected returns False when RPC validation fails."""
        import houdini_mcp.connection as conn_module

        # Set up a connection that will fail validation
        mock_hou = MagicMock()
        mock_hou.applicationVersion.side_effect = ConnectionError("Dead")
        conn_module._connection = MagicMock()
        conn_module._hou = mock_hou

        # Use validate=True to trigger RPC validation
        result = is_connected(validate=True)

        assert result is False
        assert conn_module._connection is None
        assert conn_module._hou is None

    def test_is_connected_cleans_up_on_failure(self, reset_connection_state):
        """Test is_connected cleans up global state on failure."""
        import houdini_mcp.connection as conn_module

        mock_hou = MagicMock()
        mock_hou.applicationVersion.side_effect = Exception("Connection lost")
        conn_module._connection = MagicMock()
        conn_module._hou = mock_hou

        # Use validate=True to trigger RPC validation
        is_connected(validate=True)

        assert conn_module._connection is None
        assert conn_module._hou is None

    def test_is_connected_fast_path_no_rpc(self, reset_connection_state):
        """Test that is_connected() without validate does no RPC call."""
        import houdini_mcp.connection as conn_module

        mock_hou = MagicMock()
        conn_module._connection = MagicMock()
        conn_module._connection.closed = False  # Explicitly set to False
        conn_module._hou = mock_hou

        # Default is_connected() should not call applicationVersion
        result = is_connected()

        assert result is True
        # Verify no RPC call was made
        mock_hou.applicationVersion.assert_not_called()


class TestDisconnect:
    """Tests for the disconnect function."""

    def test_disconnect_success(self, mock_connection):
        """Test successful disconnection."""
        import houdini_mcp.connection as conn_module

        disconnect()

        assert conn_module._connection is None
        assert conn_module._hou is None

    def test_disconnect_handles_close_error(self, reset_connection_state):
        """Test disconnect handles close errors gracefully."""
        import houdini_mcp.connection as conn_module

        mock_conn = MagicMock()
        mock_conn.close.side_effect = Exception("Close failed")
        conn_module._connection = mock_conn
        conn_module._hou = MagicMock()

        # Should not raise
        disconnect()

        assert conn_module._connection is None
        assert conn_module._hou is None

    def test_disconnect_when_not_connected(self, reset_connection_state):
        """Test disconnect when not connected does nothing."""
        import houdini_mcp.connection as conn_module

        # Should not raise
        disconnect()

        assert conn_module._connection is None
        assert conn_module._hou is None

    def test_disconnect_calls_close(self, reset_connection_state):
        """Test disconnect calls close on connection."""
        import houdini_mcp.connection as conn_module

        mock_conn = MagicMock()
        conn_module._connection = mock_conn
        conn_module._hou = MagicMock()

        disconnect()

        mock_conn.close.assert_called_once()


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
        assert hou is not None

    def test_ensure_connected_uses_provided_host_port(self, reset_connection_state):
        """Test ensure_connected uses provided host and port."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            from tests.conftest import MockHouModule, MockRpycConnection

            mock_hou = MockHouModule()
            mock_conn = MockRpycConnection(mock_hou)
            mock_rpyc.connect.return_value = mock_conn

            ensure_connected("custom-host", 12345)

            assert mock_rpyc.connect.call_args.args == ("custom-host", 12345)


class TestGetHou:
    """Tests for the get_hou function."""

    def test_get_hou_when_connected(self, mock_connection, mock_hou):
        """Test get_hou returns hou when connected."""
        hou = get_hou("localhost", 18811)
        assert hou is mock_hou

    def test_get_hou_connects_if_needed(self, mock_rpyc_with_reset):
        """Test get_hou connects if not connected."""
        hou = get_hou("localhost", 18811)
        assert hou is not None


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

    def test_get_connection_info_handles_error(self, reset_connection_state):
        """Test get_connection_info handles errors gracefully."""
        import houdini_mcp.connection as conn_module

        mock_hou = MagicMock()
        mock_hou.applicationVersionString.side_effect = Exception("Error")
        conn_module._connection = MagicMock()
        conn_module._hou = mock_hou

        # Patch is_connected to return True
        with patch("houdini_mcp.connection.is_connected", return_value=True):
            info = get_connection_info("localhost", 18811)

        assert "error" in info


class TestPing:
    """Tests for the ping function."""

    def test_ping_success(self, mock_hou):
        """Test ping returns True when Houdini is reachable."""
        from tests.conftest import MockRpycConnection

        mock_conn = MockRpycConnection(mock_hou)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            result = ping("localhost", 18811)

        assert result is True
        assert mock_conn._closed is True  # Connection should be closed

    def test_ping_failure(self):
        """Test ping returns False when Houdini is not reachable."""
        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.side_effect = ConnectionError("Connection refused")
            result = ping("localhost", 18811)

        assert result is False

    def test_ping_closes_connection(self, mock_hou):
        """Test ping closes connection after checking."""
        from tests.conftest import MockRpycConnection

        mock_conn = MockRpycConnection(mock_hou)

        with patch("houdini_mcp.connection.handshake") as mock_rpyc:
            mock_rpyc.connect.return_value = mock_conn
            ping("localhost", 18811)

        assert mock_conn._closed is True


class TestHoudiniConnectionError:
    """Tests for the HoudiniConnectionError exception."""

    def test_exception_message(self):
        """Test exception contains useful message."""
        error = HoudiniConnectionError("Test error message")
        assert str(error) == "Test error message"

    def test_exception_inheritance(self):
        """Test exception inherits from Exception."""
        error = HoudiniConnectionError("Test")
        assert isinstance(error, Exception)


class TestHoudiniOperationError:
    """Tests for the HoudiniOperationError exception."""

    def test_exception_message(self):
        """Test exception contains useful message."""
        error = HoudiniOperationError("Operation failed")
        assert str(error) == "Operation failed"

    def test_exception_inheritance(self):
        """Test exception inherits from Exception."""
        error = HoudiniOperationError("Test")
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
            HoudiniOperationError("connection to input 0 was reset"),
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
    def test_in_process_there_is_no_link_to_lose(self, monkeypatch, exc):
        """The stdio plugin runs in Houdini's interpreter: no rpyc, no ``_remote_tb`` tag,
        and no transport, so the same exceptions are the code's own."""
        import houdini_mcp.connection as conn_module

        monkeypatch.setattr(conn_module, "_target", IN_PROCESS_HOUDINI)
        assert transport_failure(exc) is None


class TestSafeExecute:
    """safe_execute tears the connection down only for the failures that broke it."""

    def test_file_error_reaches_caller_as_its_own_kind(self, mock_connection):
        def save():
            raise FileNotFoundError("/renders/missing/frame.exr")

        result = safe_execute(save, operation_name="save")

        assert result.success is False
        assert result.connection_lost is False
        assert result.timed_out is False
        assert result.error_type == "FileNotFoundError"
        assert get_connection() is not None

    def test_error_text_that_mentions_the_connection_is_not_connection_loss(self, mock_connection):
        def wire():
            raise HoudiniOperationError("connection refused: input 0 is already wired")

        result = safe_execute(wire, operation_name="wire")

        assert result.connection_lost is False
        assert result.error_type == "HoudiniOperationError"
        assert get_connection() is not None

    def test_timeout_raised_inside_houdini_is_not_our_deadline(self, mock_connection):
        def cook():
            raise raised_inside_houdini(TimeoutError("cook timed out"))

        result = safe_execute(cook, timeout=5.0, operation_name="cook")

        assert result.timed_out is False
        assert result.connection_lost is False
        assert result.error_type == "TimeoutError"
        assert get_connection() is not None

    def test_rpyc_sync_timeout_is_connection_loss_not_our_deadline(self, mock_connection):
        """rpyc's sync_request_timeout fires inside the operation as a bare TimeoutError:
        the link stopped answering, so it is torn down rather than reported as our own
        deadline, which only a future that never settles can be."""

        def stalled():
            raise TimeoutError("result expired")

        result = safe_execute(stalled, timeout=5.0, operation_name="stalled")

        assert result.timed_out is False
        assert result.connection_lost is True
        assert get_connection() is None

    def test_link_failure_is_connection_loss_and_tears_down(self, mock_connection):
        def read():
            raise EOFError("stream has been closed")

        result = safe_execute(read, operation_name="read")

        assert result.connection_lost is True
        assert result.error_type == "connection_error"
        assert get_connection() is None

    def test_our_deadline_is_a_timeout(self, mock_connection, caplog):
        import threading

        release = threading.Event()

        def hang():
            release.wait(5.0)

        try:
            with caplog.at_level("WARNING", logger="houdini_mcp.connection"):
                result = safe_execute(hang, timeout=0.05, operation_name="hang")
        finally:
            release.set()

        assert result.timed_out is True
        assert result.connection_lost is False
        assert result.error_type == "timeout"
        assert "still running on the link" in caplog.text


class TestExecuteWithTimeout:
    """execute_with_timeout owns one deadline; a TimeoutError the operation raised is not it."""

    def test_timeout_raised_by_the_operation_propagates_as_itself(self):
        from houdini_mcp.connection import HoudiniOperationTimeout, execute_with_timeout

        def cook():
            raise TimeoutError("cook timed out")

        with pytest.raises(TimeoutError) as exc_info:
            execute_with_timeout(cook, timeout=5.0)
        assert not isinstance(exc_info.value, HoudiniOperationTimeout)
        assert "cook timed out" in str(exc_info.value)

    def test_our_deadline_is_houdini_operation_timeout(self):
        import threading

        from houdini_mcp.connection import HoudiniOperationTimeout, execute_with_timeout

        release = threading.Event()
        try:
            with pytest.raises(HoudiniOperationTimeout):
                execute_with_timeout(release.wait, 5.0, timeout=0.05)
        finally:
            release.set()
