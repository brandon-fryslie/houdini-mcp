# Error Handling Patterns in Houdini MCP

## Overview

This document describes the error handling patterns used throughout the Houdini MCP codebase. These patterns ensure consistent, graceful error handling across all 43+ MCP tools.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         MCP Server                              │
│  serves tools.SERVED_TOOLS: every @tool function in tools/      │
│  each served from its own signature and docs, with host/port    │
│  bound to the server's target and hidden from clients           │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│               Tool (houdini_mcp/tools)                          │
│  @tool(READ)  ◄── Decorator: the policy; named by the function  │
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
def get_scene_info(host: str = DEFAULT_TARGET.host, port: int = DEFAULT_TARGET.port) -> dict[str, Any]:
    with using(host, port) as link:
        hou = link.hou
        # ... actual logic without try/except boilerplate
        return {"status": "success", ...}
```

**The failure arms** (one definition, `failure_for()` in `houdini_mcp/tools/failure.py`,
which the decorator applies). Every failure is one arm of `ToolFailure`, a union
discriminated on `kind`. Each kind fixes its own `retry`, except the two link arms, whose
`retry` the tool's policy picks:

| Exception raised in the tool | `kind` | `retry` | Also carries |
|------|-----------|----------|------|
| `HoudiniConnectionError` | `connection_unavailable` | `start_houdini` | `target`: the Houdini that was wanted |
| `HoudiniTargetMismatch` (the live link reaches another Houdini) | `connection_mismatch` | `none` | `target`, `connected` |
| A `LinkLost`: a `TransportFailure` raised inside a `using` scope (see below), not a timeout | `connection_lost` | `call_again`, or `check_scene` (below) | `exception`; that call's link was dropped first |
| A `LinkLost` whose failure is a `TimeoutError` | `timeout` | `call_again`, or `check_scene` (below) | `cut_off: "link"`, `exception` |
| `TimedOut` (a request exceeded its bound on an intact link; nothing still runs) | `timeout` | `call_again` | `cut_off: "request"` plus the tool's fields |
| `ProgramTimedOut` (execute_code killed its program's process; the server's link is intact) | `timeout` | `check_scene` | `cut_off: "program"` plus the tool's fields (`stdout`, `timeout`, `audit`, ...) |
| `PluginMissing` (Houdini's path lacks this server's build of the plugin) or `MainThreadRefused` (Houdini's main thread refused the call, which never ran) | `operation_failed` | `none` | nothing more: the message says what to do |
| `InvalidInput` | `invalid_input` | `fix_input` | the tool's fields (`path`, `renderer`, ...) |
| `OperationFailed` | `operation_failed` | `none` | the tool's fields |
| Anything else | `operation_failed` | `none` | `exception`, `traceback`; logged at error with the traceback |

A link that fails mid-call says nothing about whether the call took effect: rpyc's sync
timeout leaves the request running in Houdini, and an EOF can arrive after Houdini ran
it. So the two link arms read `check_scene` under a tool whose policy changes the scene
(`ToolPolicy.changes_scene`: `UNDOABLE`, `CHECKPOINTED`, `PROGRAM`, and `RENDER`, whose
temporary nodes a failed link strands and names in the message), and their message says
to check the scene before calling again. Any other tool reads `call_again`. The tool
boundary passes the policy's fact to `failure_for`; a caller that knows nothing of the
call gets `check_scene`, so forgetting never invites a repeated write.

A tool body that wraps an exception as `raise OperationFailed(...) from e` has stated
its cause, and the link's own failures in that cause outrank the label: a torn link,
an unreachable Houdini or a mismatched one wrapped that way is still its own arm. A
torn link inside a `using` scope drops the link that call used; one outside any scope,
raw or wrapped, is reported as the bug it is (`operation_failed`, logged at error). A served tool that finishes its result
after the tool returned (`find_error_nodes`, `get_geo_summary` summarize, then cap)
is under the same boundary, awaited.

Tool bodies never build an error dict. They raise `InvalidInput`, `OperationFailed`,
`TimedOut` or `ProgramTimedOut` with the fields their caller needs; and `node_at(hou, path, what)`
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
`cut_off: "link"`.

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
    "cut_off": "link",
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
    | "operation_failed",
    "retry": "start_houdini" | "call_again" | "check_scene" | "fix_input" | "none",
    "operation": "tool_name",
    "message": "Human-readable error description",
    # Per kind: target / exception / cut_off (see the table above),
    # then whatever fields the tool attached (a path, a URL, stdout so far, an audit).
}
```

### `timeout` and what it cut off

`cut_off` says what a timeout ended, one value per fact, and the retry follows
from it; the arm refuses any other pair:

| `cut_off` | What ended | `retry` |
|------|------|------|
| `request` | a request exceeded its bound on an intact link; nothing of it still runs | `call_again` |
| `link` | the server's link never answered and was dropped; the next call reconnects | `call_again`, or `check_scene` for a tool that changes the scene |
| `program` | execute_code killed its program's process at a clock the call owns; the server's link is intact | `check_scene` |

A killed program may have changed what its undo cannot reach, so the scene is
always checked before calling again. The program's process owns the clocks on
its own link - its 60-second start deadline, each request's bound, its undo
step's reply - and reports each expiry itself; the call owns the program's
timeout, and kills a process that stops reporting a phase it owns.

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
def delete_node(
    node_path: str, host: str = DEFAULT_TARGET.host, port: int = DEFAULT_TARGET.port
) -> dict[str, Any]:
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
@tool(RENDER)
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
that does not compile, an `import hou`, code outside the inspection allowlist under `read-only`, or a rail the
caller did not unlock.
Once the program's process starts, it raises `OperationFailed` when the program
raised or its process died, `ProgramTimedOut` when the call killed the process,
`ProgramDidNotRun` (an `OperationFailed`) when the program never began, and, as it
came, the exception from a scene read that failed before the program ran. Every
failure is raised; none is a dict the body builds.

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
4. **Resolve paths with `node_at`** - it returns the node or raises `invalid_input` for you;
   a path to a network a tool works inside resolves with `network_at`, which also refuses
   a node that holds no nodes
5. **Write to the scene inside `refused_by_houdini`** - Houdini's refusal of the write
   reaches the caller as `operation_failed` in Houdini's words, a lost link as
   `connection_lost` naming what was asked, and a fault in this process as a bug; create nodes
   with `created_in`, which writes through it. A handler that must see a lost link raw (the
   render's cleanup) raises `houdini_refusal` for what `raised_inside_houdini`, and re-raises
   the rest
6. **Clean up connections** - The decorator handles this, but be aware of the pattern

## Files Reference

| File | Purpose |
|------|---------|
| `houdini_mcp/tools/failure.py` | `ToolFailure`, its arms, what tools raise, and `failure_for` |
| `houdini_mcp/tools/_common.py` | The `@tool` boundary, `node_at`, `network_at`, `houdini_refusal`, `refused_by_houdini` and `created_in` |
| `houdini_mcp/connection.py` | Connection management with retry logic |
| `tests/conftest.py` | Mock infrastructure for testing |
| `tests/test_tools.py` | Tests for error handling decorator |

## Related Documentation

- [Tool Contracts](../tool-contracts.md) - what a caller sees of these failures
