# Error Handling Patterns in Houdini MCP

## Overview

This document describes the error handling patterns used throughout the Houdini MCP codebase. These patterns ensure consistent, graceful error handling across all 43+ MCP tools.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         MCP Server                              │
│  server.py: SERVED_TOOLS = (tools.some_tool, ...)               │
│  each served from its own signature and docs, with host/port    │
│  bound to the server's target and hidden from clients           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│               Tool (houdini_mcp/tools)                          │
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

**The failure arms** (one definition, `failure_for()` in `houdini_mcp/tools/failure.py`,
which the decorator applies). Every failure is one arm of `ToolFailure`, a union
discriminated on `kind`; each kind fixes its own `retry`:

| Exception raised in the tool | `kind` | `retry` | Also carries |
|------|-----------|----------|------|
| `HoudiniConnectionError` | `connection_unavailable` | `start_houdini` | `target`: the Houdini that was wanted |
| `HoudiniTargetMismatch` (the live link reaches another Houdini) | `connection_mismatch` | `none` | `target`, `connected` |
| A `TransportFailure` (see below) that is not a timeout | `connection_lost` | `call_again` | `exception`; the link is torn down first |
| A `TransportFailure` that is a `TimeoutError` | `timeout` | `call_again` | `connection_reset: true`, `exception` |
| `TimedOut` (a request exceeded its bound on an intact link; nothing still runs) | `timeout` | `call_again` | `connection_reset: false` plus the tool's fields |
| `ProgramStillRunning` (the program this call launched outlived its bound and holds the scene) | `worker_still_running` | `wait_for_worker` | `worker` plus the tool's fields (`stdout`, `rollback`, `audit`, ...) |
| `InvalidInput` | `invalid_input` | `fix_input` | the tool's fields (`path`, `renderer`, ...) |
| `OperationFailed` | `operation_failed` | `none` | the tool's fields |
| `WorkerStillRunning` | `worker_still_running` | `wait_for_worker` | `worker` |
| Anything else | `operation_failed` | `none` | `exception`, `traceback`; logged at error with the traceback |

A tool body that wraps an exception as `raise OperationFailed(...) from e` has stated
its cause, and the link's own failures in that cause outrank the label: a torn link,
an unreachable Houdini or a mismatched one wrapped that way is still its own arm, and
a torn link still tears the connection down. A served tool that finishes its result
after the tool returned (`find_error_nodes`, `get_geo_summary` summarize, then cap)
is under the same boundary, awaited.

Tool bodies never build an error dict. They raise `InvalidInput`, `OperationFailed`,
`TimedOut` or `ProgramStillRunning` with the fields their caller needs; a failure
something else already classified by kind (a program Houdini ran, a ledger of
parameter writes) becomes its exception through `tool_error(kind, ...)`; and `node_at(hou, path, what)`
resolves a path or raises `InvalidInput` for them. A caller parses any failure back
through `FAILURE` (a pydantic `TypeAdapter` over the union) and lands in exactly one
arm; the contract tests do exactly that.

### 2. `transport_failure()` - the one place connection loss is decided

**Location:** `houdini_mcp/connection.py`

Only a failure of the link itself is connection loss. `transport_failure(exc)` returns a
`TransportFailure` proof, or `None`, and the installed `HoudiniTarget` is what decides:

- Over rpyc (`remote_houdini(token)`), the type must be one the channel raises: `EOFError`
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
tears the session down and is never `connection_lost` or `timeout` with
`connection_reset: true`.

### 3. Connection loss as the caller reads it

A `TransportFailure` is the one thing that tears the connection down: `failure_for`
calls `disconnect()` so the next call reconnects, then builds the arm.

```python
{
    "status": "error",
    "kind": "timeout",
    "retry": "call_again",
    "connection_reset": True,
    "exception": "TimeoutError",
    "message": "Operation 'get_scene_info' timed out...",
    "operation": "get_scene_info",
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
never tear it down. `get_connection_info` reports the target of the Houdini
actually reached when connected, not the one it was asked about.

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
    "kind": "connection_unavailable"
    | "connection_mismatch"
    | "connection_lost"
    | "timeout"
    | "invalid_input"
    | "operation_failed"
    | "worker_still_running",
    "retry": "start_houdini" | "call_again" | "fix_input" | "wait_for_worker" | "none",
    "operation": "tool_name",
    "message": "Human-readable error description",
    # Per kind: target / exception / connection_reset / worker (see the table above),
    # then whatever fields the tool attached (a path, a URL, stdout so far, an audit).
}
```

### `worker_still_running`

`execute_code` runs the caller's program in a thread the server cannot kill.
When it times out, the program keeps running with its undo group open, and
`WorkerLedger` (`houdini_mcp/tools/workers.py`) records it. Until that thread
ends, every tool whose `ToolPolicy.access` is `EXCLUSIVE` (see
`TOOL_POLICIES` in `tools/transactions.py`: the scene writers,
`get_geo_summary`, which reads through a program in the same worker slot, and
the render tools, which add a camera and a ROP for the call) is refused at
the tool boundary:

```python
{
    "status": "error",
    "kind": "worker_still_running",
    "retry": "wait_for_worker",
    "operation": "create_node",
    "message": "create_node refused: a program launched by execute_code (sha256 ...) is still running ...",
    "worker": {
        "operation": "execute_code",
        "code_sha256": "...",
        "timeout": 30,
        "running_for_seconds": 41.2,
    },
}
```

`worker.operation` is the tool that launched the program and `worker.code_sha256`
matches `audit.code_sha256` of the timeout response that started it. Plain reads
and file writers (`save_scene`, the screenshot tools) proceed. Recovery: wait for the program to finish; a program that uses
`hou` ends when Houdini restarts, one that never touches Houdini ends only with
the MCP server. Nothing clears the slot while the thread lives, because a cleared
slot with a live thread would be a lie.

## Input Validation

Input shape is not checked inside a tool. It is stated in the tool's parameter
types, which the server publishes as the tool's JSON schema and enforces at the
MCP boundary before the call reaches Houdini. The render inputs are the model
(`houdini_mcp/tools/_common.py`, `Resolution` and `Vec3`): a pair of integers in
64–4096 and a triple of numbers. The server validates strictly
(`strict_input_validation` in `houdini_mcp/server.py`), so it refuses exactly
what the published schema refuses. A call that fails the schema is a protocol
error to the client, never an error result. `tests/test_tool_contracts.py`
asserts the schema and the server's refusals agree.

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

### Pattern 2: Tool with Constrained Input

```python
@handle_connection_errors("render_viewport")
def render_viewport(
    resolution: Resolution = DEFAULT_RESOLUTION,
    ...
) -> dict[str, Any]:
    hou = ensure_connected(host, port)
    width, height = resolution
    # ... proceed with rendering: the type already refused anything else
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
    assert result["kind"] == "timeout"
    assert result["connection_reset"] is True
```

`tests/test_failure.py` parses every arm the boundary emits back through `FAILURE`
and refuses a response relabelled as another kind or as success.

### Testing Input Refusal

Input refusal is a fact of the served contract, so it is tested through the
MCP client, not by calling the tool function. See
`TestServedSchemas.test_render_resolution_bounds_are_served` in
`tests/test_tool_contracts.py`.

## Best Practices

1. **Always use the decorator** - Don't write try/except for connection errors in tool functions
2. **Validate early** - Check inputs before connecting to Houdini
3. **Raise, don't build error dicts** - `InvalidInput`, `OperationFailed` or `TimedOut`
   with the fields the caller needs; the boundary shapes the response, so every
   tool's failure reads the same and a new tool cannot invent a shape
4. **Resolve paths with `node_at`** - it returns the node or raises `invalid_input` for you
5. **Clean up connections** - The decorator handles this, but be aware of the pattern

## Files Reference

| File | Purpose |
|------|---------|
| `houdini_mcp/tools/failure.py` | `ToolFailure`, its arms, what tools raise, and `failure_for` |
| `houdini_mcp/tools/_common.py` | The `@handle_connection_errors` boundary and `node_at` |
| `houdini_mcp/connection.py` | Connection management with retry logic |
| `tests/conftest.py` | Mock infrastructure for testing |
| `tests/test_tools.py` | Tests for error handling decorator |

## Related Documentation

- [Error Introspection](./error-introspection.md) - Node-level error/warning detection
- [Connection Module](../reference/quick-reference.md) - Connection API reference
