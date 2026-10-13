# Error Handling Patterns in Houdini MCP

## Overview

This document describes the error handling patterns used throughout the Houdini MCP codebase. These patterns ensure consistent, graceful error handling across all 43+ MCP tools.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         MCP Tool                                │
│  @mcp.tool()                                                    │
│  def some_tool(...):                                            │
│      return tools.some_tool(...)                                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Tool Implementation                          │
│  @handle_connection_errors("some_tool")  ◄── Decorator          │
│  def some_tool(...):                                            │
│      hou = ensure_connected(host, port)  ◄── Connection         │
│      # ... actual logic (no try/except needed)                  │
│      return {"status": "success", ...}                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Connection Layer                             │
│  connection.py                                                  │
│  - HoudiniConnection: the one owner of the target and the link  │
│  - connect() with exponential backoff + jitter                  │
│  - ensure_connected() reaches the Houdini named, or fails       │
└─────────────────────────────────────────────────────────────────┘
```

## Key Components

### 1. `@handle_connection_errors` Decorator

**Location:** `handle_connection_errors` in `houdini_mcp/tools/_common.py`

The primary error handling mechanism. Wraps tool functions with a three-tier error pattern:

```python
@handle_connection_errors("get_scene_info")
def get_scene_info(host: str = "localhost", port: int = 18811) -> Dict[str, Any]:
    hou = ensure_connected(host, port)
    # ... actual logic without try/except boilerplate
    return {"status": "success", ...}
```

**Error Tiers** (one definition, `_tool_error_response()`, which the decorator applies):

| Tier | Exception | Handling |
|------|-----------|----------|
| 1 | `HoudiniConnectionError` | Simple error response with message |
| 2 | A `TransportFailure` (see below) | `_handle_connection_error()`: disconnects, then builds the response |
| 3 | Anything else | Log + error response with traceback; the connection stays up |

### 2. `transport_failure()` - the one place connection loss is decided

**Location:** `houdini_mcp/connection.py`

Only a failure of the link itself is connection loss. `transport_failure(exc)` returns a
`TransportFailure` proof, or `None`, and the installed `HoudiniTarget` is what decides:

- Over rpyc (`REMOTE_HOUDINI`), the type must be one the channel raises: `EOFError`
  (peer closed, reset, or broken pipe: rpyc's `SocketStream` wraps every socket error
  it meets into `EOFError`, so no `ConnectionError` crosses a live channel), or
  `TimeoutError` (a sync request never answered; rpyc's `AsyncResultTimeout` is that
  same class). `OSError` is deliberately not admitted: it is also the base of
  `FileNotFoundError` and `PermissionError`, which are the tool's errors, not the link's.
  And the exception must not have been raised inside Houdini: rpyc rebuilds a remote
  exception locally as the same class and tags it with `_remote_tb`; a `TimeoutError`
  carrying that tag came over an intact link and is reported as its own kind.
- In-process (`IN_PROCESS_HOUDINI`, the plugin's stdio server) there is no link to lose,
  so nothing is a transport failure.

Opening the link is a retry decision, not a proof (`HoudiniTarget.open_is_retryable`): before a
connection exists only the socket layer can raise an `OSError`, so at connect time the
whole family - unreachable network, unresolved host, refused or denied socket, a
stalled handshake - is retried. A rejected token raises `HandshakeError` and is not.

A bad save or render path, a denied file, or a Houdini operation error therefore never
tears the session down and never reports `connection_error` / `recoverable: True`.

### 3. `_handle_connection_error()` Function

**Location:** `houdini_mcp/tools/_common.py`

Takes a `TransportFailure` (so it cannot be called for anything else) and:

1. Cleans up the broken connection (calls `disconnect()`)
2. Logs the error
3. Returns a user-friendly error response with context

**Response Format:**
```python
{
    "status": "error",
    "error_type": "connection_error",
    "exception": "TimeoutError",  # Exception class name
    "message": "Operation 'get_scene_info' timed out...",
    "operation": "get_scene_info",
    "recoverable": True,  # Indicates retry is possible
}
```

**Context-Specific Messages:**

| Exception | Message Focus |
|-----------|--------------|
| `TimeoutError` | Houdini may be busy with heavy computation |
| `EOFError` | Houdini crashed, the RPC server stopped, or the connection was reset (rpyc wraps socket errors into `EOFError`) |

### 4. Connection Layer Error Handling

**Location:** `houdini_mcp/connection.py`

#### Retry with Exponential Backoff

Opening a link is the one place anything retries, in `_open_link_with_retries()`,
which `connect()` and `ensure_connected()` both go through. Whether an open-time
failure is worth another attempt is the installed `HoudiniTarget`'s decision
(`open_is_retryable`): over rpyc any socket-layer `OSError`/`EOFError` is, a rejected
token is not, and in-process nothing is. The delay starts at `retry_delay` (1.0s),
doubles each attempt, is capped at `DEFAULT_MAX_DELAY` (30s), and then carries up to
10% jitter on top of the capped value so clients that lost Houdini together do not
reconnect in lockstep, even once they are all at the cap. After `max_retries` (3)
attempts it raises `HoudiniConnectionError` with the last error; `max_retries` below 1
is rejected with `ValueError`.

#### `ensure_connected()` Function

Returns the `hou` of the Houdini `host:port` names, reconnecting when the link is
gone. Connection state has one owner, `HoudiniConnection`: the target in use and at
most one open `Link` (target, host, port, connection, `hou`), written only under its
lock, so concurrent tool calls that find the link gone share one reconnect. A live
link to a *different* Houdini is not an answer: the call raises
`HoudiniConnectionError` naming both, and the link it did not ask for stands.
In-process (the Houdini plugin), every address names this interpreter.

```python
def ensure_connected(host: str = "localhost", port: int = 18811) -> Any:
    return _houdini.ensure(host, port).hou
```

`is_connected()` and `get_connection_info()` are queries: they read the link and
never tear it down. `get_connection_info` reports the host, port and target of the
Houdini actually reached when connected, not the ones it was asked about.

## Standard Response Format

All tools return consistent response dictionaries:

### Success Response
```python
{
    "status": "success",
    # ... operation-specific data
}
```

### Error Response
```python
{
    "status": "error",
    "message": "Human-readable error description",
    # Optional fields:
    "error_type": "connection_error" | "validation_error" | ...,
    "exception": "ExceptionClassName",
    "operation": "tool_name",
    "recoverable": True | False,
    "traceback": "...",  # Only for unexpected errors
}
```

## Validation Helpers

### `validate_resolution()`

**Location:** `houdini_mcp/tools/_common.py:202-230`

Validates render resolution dimensions:

```python
def render_viewport(resolution: Optional[List[int]] = None, ...):
    if resolution is None:
        resolution = [512, 512]

    # Returns error dict if invalid, None if valid
    if error := validate_resolution(resolution):
        return error

    # Continue with valid resolution...
```

## Usage Patterns

### Pattern 1: Simple Tool with Decorator

```python
@handle_connection_errors("list_children")
def list_children(
    node_path: str,
    host: str = "localhost",
    port: int = 18811,
) -> Dict[str, Any]:
    hou = ensure_connected(host, port)

    node = hou.node(node_path)
    if node is None:
        return {"status": "error", "message": f"Node not found: {node_path}"}

    children = [child.name() for child in node.children()]
    return {"status": "success", "children": children}
```

### Pattern 2: Tool with Input Validation

```python
@handle_connection_errors("render_viewport")
def render_viewport(
    resolution: Optional[List[int]] = None,
    ...
) -> Dict[str, Any]:
    hou = ensure_connected(host, port)

    if resolution is None:
        resolution = [512, 512]

    # Validate early, return error if invalid
    if error := validate_resolution(resolution):
        return error

    # ... proceed with rendering
```

### Pattern 3: Tool with Multiple Error Sources

```python
@handle_connection_errors("execute_code")
def execute_code(
    code: str,
    host: str = "localhost",
    port: int = 18811,
) -> Dict[str, Any]:
    # Parse once; a program the gates cannot read is a program that does not run
    try:
        tree = parse_code(code)
    except SyntaxError as exc:
        return {
            "status": "error",
            "message": f"Code does not parse: {exc.msg} (line {exc.lineno})",
        }

    # Judge the parsed program BEFORE connecting
    dangerous = _detect_dangerous_code(tree)
    if dangerous:
        return {
            "status": "error",
            "message": f"Dangerous code pattern detected: {dangerous}",
        }

    hou = ensure_connected(host, port)

    try:
        exec(code, {"hou": hou})
        return {"status": "success"}
    except Exception as e:
        # Decorator handles connection errors
        # This catches code execution errors specifically
        return {
            "status": "error",
            "message": f"Code execution failed: {e}",
        }
```

## Testing Error Handling

### Mock Setup

Tests use `MockHouModule` and `MockHouNode` from `tests/conftest.py`. The
`mock_connection` fixture connects to the double through the connection module's own
API (the handshake seam hands out a `MockRpycConnection`, `ensure_connected` installs
it), so a test sees exactly what a tool call would; `connected_to(hou)` does the same
for any other `hou` double:

```python
def test_reads_my_double(self, connected_to):
    hou = connected_to(MagicMock())
    ...
```

### Testing Connection Errors

```python
def test_handles_connection_timeout(self, mock_connection):
    from houdini_mcp.tools import get_scene_info

    # Simulate timeout
    mock_connection.node.side_effect = TimeoutError("Connection timed out")

    result = get_scene_info()

    assert result["status"] == "error"
    assert result["error_type"] == "connection_error"
    assert result["recoverable"] is True
```

### Testing Validation Errors

```python
def test_validates_resolution(self, mock_connection):
    from houdini_mcp.tools import render_viewport

    result = render_viewport(resolution=[10, 10])  # Too small

    assert result["status"] == "error"
    assert "at least 64x64" in result["message"]
```

## Best Practices

1. **Always use the decorator** - Don't write try/except for connection errors in tool functions
2. **Validate early** - Check inputs before connecting to Houdini
3. **Return dicts, don't raise** - Tools should return error dicts, not raise exceptions
4. **Include `recoverable` flag** - Help clients know if they can retry
5. **Log at appropriate levels** - Use `logger.error()` for unexpected errors, `logger.warning()` for recoverable ones
6. **Clean up connections** - The decorator handles this, but be aware of the pattern

## Files Reference

| File | Purpose |
|------|---------|
| `houdini_mcp/tools/_common.py` | Core error handling utilities |
| `houdini_mcp/connection.py` | Connection management with retry logic |
| `tests/conftest.py` | Mock infrastructure for testing |
| `tests/test_tools.py` | Tests for error handling decorator |

## Related Documentation

- [Error Introspection](./error-introspection.md) - Node-level error/warning detection
- [Connection Module](../reference/quick-reference.md) - Connection API reference
