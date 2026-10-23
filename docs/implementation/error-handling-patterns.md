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
│  @tool(READ)  ◄── Decorator: the policy; the name is the function│
│  def some_tool(...):                                            │
│      with using(host, port) as link:  ◄── Connection            │
│          hou = link.hou                                         │
│          # ... actual logic (no try/except needed)              │
│          return {"status": "success", ...}                      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Connection Layer                             │
│  connection.py                                                  │
│  - HoudiniConnection: the one owner of the target and the link  │
│  - ensure() reaches the Houdini named, or fails                 │
│  - opening a link retries with exponential backoff + jitter     │
│  - drop(link) tears down the link a failed call used            │
└─────────────────────────────────────────────────────────────────┘
```

## Key Components

### 1. `@tool(policy)` Decorator

**Location:** `tool` in `houdini_mcp/tools/_common.py`

It takes the tool's `ToolPolicy` (`READ`, `UNDOABLE`, ... in `tools/transactions.py`).
The tool's name is its function's: the `operation` its failures carry and the name it is
served as are both that name.

The primary error handling mechanism. Wraps tool functions with a three-tier error pattern:

```python
@tool(READ)
def get_scene_info(host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    with using(host, port) as link:
        hou = link.hou
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
| A `LinkLost`: a `TransportFailure` raised inside a `using` scope (see below), not a timeout | `connection_lost` | `call_again` | `exception`; that call's link was dropped first |
| A `LinkLost` whose failure is a `TimeoutError` | `timeout` | `call_again` | `connection_reset: true`, `exception` |
| `TimedOut` (a request exceeded its bound on an intact link; nothing still runs) | `timeout` | `call_again` | `connection_reset: false` plus the tool's fields |
| `ProgramStillRunning` (the program this call launched outlived its bound and holds the scene) | `worker_still_running` | `wait_for_worker` | `worker` plus the tool's fields (`stdout`, `rollback`, `audit`, ...) |
| `InvalidInput` | `invalid_input` | `fix_input` | the tool's fields (`path`, `renderer`, ...) |
| `OperationFailed` | `operation_failed` | `none` | the tool's fields |
| `WorkerStillRunning` | `worker_still_running` | `wait_for_worker` | `worker` |
| Anything else | `operation_failed` | `none` | `exception`, `traceback`; logged at error with the traceback |

A tool body that wraps an exception as `raise OperationFailed(...) from e` has stated
its cause, and the link's own failures in that cause outrank the label: a torn link,
an unreachable Houdini or a mismatched one wrapped that way is still its own arm. A
torn link inside a `using` scope drops the link that call used; one outside any scope,
raw or wrapped, is reported as the bug it is (`operation_failed`, logged at error). A served tool that finishes its result
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

Only a failure of the link itself is connection loss. `transport_failure(exc)` is the one
classification: it returns a `TransportFailure` proof when both of these hold, and `None`
otherwise.

- The type is one the channel raises: `EOFError` (peer closed, reset, or broken pipe:
  rpyc's `SocketStream` wraps every socket error it meets into `EOFError`, so no
  `ConnectionError` crosses a live channel), or `TimeoutError` (a sync request never
  answered; rpyc's `AsyncResultTimeout` is that same class). `OSError` is deliberately
  not admitted: it is also the base of `FileNotFoundError` and `PermissionError`, which
  are the tool's errors, not the link's.
- The exception was not raised inside Houdini. rpyc rebuilds a remote exception locally
  as the same class and tags it with `_remote_tb`; a `TimeoutError` carrying that tag
  came over an intact link and is reported as its own kind.

Opening the link is a retry decision, not a proof, and one rule makes it: any `OSError`
or `EOFError` that Houdini did not raise is retried. Before a connection exists only the
socket layer can raise an `OSError`, so the whole family - unreachable network,
unresolved host, refused or denied socket, a stalled handshake - is the link's. A
rejected token raises `HandshakeError` and is not retried.

A bad save or render path, a denied file, or a Houdini operation error therefore never
tears the session down and is never `connection_lost` or `timeout` with
`connection_reset: true`.

### 3. Connection loss as the caller reads it

A `TransportFailure` raised inside a tool's `using` block is the one thing that tears
the connection down. `failed_on` drops the link that call used, so the next call
reconnects, and raises a `LinkLost` in its place; the classifier builds the
`connection_lost` or `timeout` arm from that `LinkLost` alone. A transport failure
that reaches the boundary raw came from Houdini work done outside any `using` scope,
and is reported as the bug it is: `operation_failed`, logged at error.

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
which the owner's `connect` runs, as does `ensure` when it finds no live link. The
one retry rule above
decides whether an open-time failure is worth another attempt: any `OSError` or
`EOFError` that Houdini did not raise is, and a rejected token is not. The delay starts
at `retry_delay` (1.0s), doubles each attempt, is capped at `DEFAULT_MAX_DELAY` (30s),
and then carries up to 10% jitter on top of the capped value so clients that lost
Houdini together do not reconnect in lockstep, even once they are all at the cap. After
`max_retries` (3) attempts it raises `HoudiniConnectionError` with the last error;
`max_retries` below 1 is rejected with `ValueError`.

#### `using()`: how a tool gets a link

`using(host, port)` in `houdini_mcp/tools/_common.py` is the one way a tool gets a
link: a context manager yielding the `Link` (`link.hou`, `link.conn`) that the
connection owner's `ensure(host, port)` returns, reconnecting when the link is gone.
Connection state has one owner, `HoudiniConnection`, and `houdini()` returns the
process's: the target in use and at most one open `Link` (host, port, `conn`, `hou`),
written only under its lock, so concurrent tool calls that find the link gone share
one reconnect. A live link to a *different* Houdini is not an answer: `ensure` raises
`HoudiniTargetMismatch` naming both, and the link it did not ask for stands.

`held_link(owner, host, port)` is the same scope over any owner. When the block
raises, the exception goes through `failed_on(owner, link, e)`, the one place a
failure is judged the link's: a transport failure, or for a `ToolError` the cause it
was raised from. Then `owner.drop(link)` closes that link and uninstalls it only if it
is still the installed one, so a call whose link failed after another call reconnected
never drops the replacement, and `failed_on` returns a `LinkLost` carrying the link
and the transport failure. Any other exception comes back unchanged and propagates as
it was raised.

```python
@contextmanager
def held_link(owner: HoudiniConnection, host: str, port: int) -> Iterator[Link]:
    link = owner.ensure(host, port)
    try:
        yield link
    except Exception as e:
        failed = failed_on(owner, link, e)
        if failed is e:
            raise
        raise failed from e


def using(host: str, port: int) -> AbstractContextManager[Link]:
    return held_link(houdini(), host, port)
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
ends, every tool whose `ToolPolicy.access` is `EXCLUSIVE` (the policy
its `@tool` declares, from `tools/transactions.py`: the scene writers,
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
what the published schema refuses. A call that fails the schema never reaches the
tool: the client gets a result with MCP's `isError` set and the validation message
as its text, not one of the arms under Key Components. `tests/test_tool_contracts.py`
asserts the schema and the server's refusals agree.

## Usage Patterns

### Pattern 1: Simple Tool with Decorator

`delete_node` (`houdini_mcp/tools/nodes.py`) is the whole shape: the decorator,
`using`, `node_at` raising `invalid_input` for a path with no node,
and a body that returns only success.

```python
@tool(UNDOABLE)
def delete_node(node_path: str, host: str = "localhost", port: int = 18811) -> dict[str, Any]:
    with using(host, port) as link:
        hou = link.hou

        node = node_at(hou, node_path)

        node_name = node.name()
        node.destroy()

        return {
            "status": "success",
            "message": f"Deleted node: {node_name}",
            "deleted_path": node_path,
        }
```

### Pattern 2: Tool with Constrained Input

```python
@tool(HOLDS_SCENE)
def render_viewport(
    resolution: Resolution = DEFAULT_RESOLUTION,
    ...
) -> dict[str, Any]:
    with using(host, port) as link:
        prepared = _prepare(link, resolution, renderer, karma_engine)
        # ... proceed with rendering: _prepare unpacks `width, height = resolution`,
        # and the type already refused anything else
```

### Pattern 3: Tool with Multiple Error Sources

`execute_code` (`houdini_mcp/tools/code.py`) refuses a program before it
connects: it raises `_blocked(...)`, which builds the `InvalidInput`, for a program
that does not parse, an `import hou`, a mutation under `read-only`, or a rail the
caller did not unlock.
Once the program runs, it raises `OperationFailed` when the program raised,
`ProgramStillRunning` when the program outlived its timeout, and, as it came, the
exception from a scene read that failed before the program ran or from a worker
that died. Every failure is
raised; none is a dict the body builds.

## Testing Error Handling

### Mock Setup

Tests use `MockHouModule` and `MockHouNode` from `tests/conftest.py`. The
`mock_connection` fixture connects to the double through the connection module's own
API (the handshake seam hands out a `MockRpycConnection`, the owner's `connect` installs
it), so a test sees exactly what a tool call would; `connected_to(hou)` does the same
for any other `hou` double:

```python
def test_reads_my_double(self, connected_to):
    hou = connected_to(MagicMock())
    ...
```

### Testing Connection Errors

`TestConnectionErrorHandling` in `tests/test_tools.py` raises each exception from a
stub tool under the decorator and reads the arm it returns, including the errors
that leave the connection standing. `test_get_scene_info_handles_timeout` raises a
timeout from a Houdini read inside a real tool's `using` block.

`tests/test_failure.py` parses every arm the boundary emits back through `FAILURE`
and refuses a response relabelled as another kind or as success.

### Testing Input Refusal

Input refusal is a fact of the served contract, so it is tested through the
MCP client, not by calling the tool function. See
`TestServedSchemas.test_render_resolution_bounds_are_served` in
`tests/test_tool_contracts.py`.

## Best Practices

1. **Always use the decorator** - Don't write try/except for connection errors in tool functions
2. **Refuse before connecting** - a check that needs no Houdini, like `execute_code`'s
   program gates, runs before `using`; input shape belongs in the
   parameter types (see Input Validation)
3. **Raise, don't build error dicts** - one of the exceptions under Key Components,
   with the fields the caller needs; the boundary shapes the response, so every
   tool's failure reads the same and a new tool cannot invent a shape
4. **Resolve paths with `node_at`** - it returns the node or raises `invalid_input` for you
5. **Clean up connections** - The decorator handles this, but be aware of the pattern

## Files Reference

| File | Purpose |
|------|---------|
| `houdini_mcp/tools/failure.py` | `ToolFailure`, its arms, what tools raise, and `failure_for` |
| `houdini_mcp/tools/_common.py` | The `@tool` boundary and `node_at` |
| `houdini_mcp/connection.py` | Connection management with retry logic |
| `tests/conftest.py` | Mock infrastructure for testing |
| `tests/test_tools.py` | Tests for error handling decorator |

## Related Documentation

- [Tool Contracts](../tool-contracts.md) - what a caller sees of these failures
