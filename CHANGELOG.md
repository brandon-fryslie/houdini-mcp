# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Pane types have one source** (houdini-laws-etg.8tg): `list_visible_panes`'s
  `available_types`, the names `capture_pane_screenshot` and
  `capture_multiple_panes` accept, and the list an unknown-type error offers are
  all read from the connected Houdini's `hou.paneTabType`. The hand copy of that
  enum, `houdini_mcp.tools.VALID_PANE_TYPES`, is gone; it already lacked the
  `GenericGraphEditor` and `UsdShadeEditor` panes Houdini 22 ships, and it
  answered in place of the live enum whenever the live read failed. A name that
  is an attribute of the enum class but not a pane type (`thisown`) is now
  refused as unknown instead of being handed to `hou.ui.paneTabOfType`, which
  raised on it.

- **Render tools leave the scene as they found it** (houdini-laws-etg.jfl):
  `render_viewport` and `render_quad_view` destroy the camera rig and render
  ROP they create before returning, on success and on failure alike, instead
  of leaving `_mcp_*` nodes in `/obj` and `/out` for the next call to find.
  Temporary nodes are destroyed by reference, never looked up by name, so a
  user's node that happens to be called `_mcp_render_cam` is no longer
  destroyed by a render. `render_viewport`'s result no longer carries
  `camera_path`: there is no camera after the call.

### Changed

- **Connection failure has one definition each of classification, retry and error**
  (houdini-laws-etg.c3b): the in-use failure set is the one union `TransportException`,
  which is both the check `transport_failure()` runs and the type of the proof it
  returns; opening a link is the one retry policy (`_open_link_with_retries`); and the
  tool decorator's `_tool_error_response` is the one caller-facing error. Removed as
  dead, with their tests: the never-applied `retry_with_backoff` decorator,
  `safe_execute`, `SafeExecutionResult`, `execute_with_timeout`,
  `HoudiniOperationTimeout`, `HoudiniOperationError`, the module thread pool, and the
  unused `DEFAULT_TIMEOUT` / `DEFAULT_OPERATION_TIMEOUT` constants.

- **Parameter read and write have one owner** (houdini-laws-etg.dq2):
  `houdini_mcp/tools/parms.py` resolves a parameter name the way Houdini does
  (`parm()` for a scalar or component, else `parmTuple()` for a vector), reads it
  into one JSON shape (a vector is a list), and writes it with one failure shape.
  `set_parameter`, `create_material`, `assign_material`, `set_render_settings` and
  `create_render_node` all write through it: every batch tool now answers with
  `updated` (`{name, value}`) and `failed` (`{name, reason}`) and status `partial`
  when any failed; `create_material`'s `parameters_set` and `create_render_node`'s
  `settings_applied` are replaced by those, and a parameter the material or ROP
  lacks is reported instead of logged and dropped. `get_parameter_schema`,
  `get_material_info`, `get_render_settings`, `list_render_nodes`, `get_node_info`
  and `serialize_scene` read through it: a present parameter that fails to evaluate
  reads as the one `UNEVALUABLE` marker everywhere. `get_parameter_schema` no longer
  branches on the test double's `_params`. The converter is `parms.json_safe_hou_value`
  (`_common._json_safe_hou_value` is gone), and its cycle guard now tracks only
  containers, so a vector with a repeated component (`[1.0, 0.0, 0.0]`) no longer
  reads its second `0.0` as `<recursion>`.
- **Connection state has one owner** (houdini-laws-etg.bfq): `connection.py`'s
  module globals (`_connection`, `_hou`, `_target`, `_executor`) are replaced by
  `HoudiniConnection`, which holds the target and at most one `Link` (target, host,
  port, connection, `hou`) and writes them only under its lock, so concurrent tool
  calls that find the link gone share one reconnect. `ensure_connected(host, port)`
  reaches the Houdini it names or raises `HoudiniConnectionError` naming both the
  connected and the requested Houdini; it no longer answers a request for one
  Houdini with a link to another. `connect()` returns the `Link` and closes the link
  it replaces only after the new one is proven. `is_connected()` is a read of socket
  state with no `validate` mode and no teardown; `get_connection_info` reports the
  host, port and target actually reached when connected. `use_in_process_houdini()`
  becomes `use_houdini(IN_PROCESS_HOUDINI)`. The sync timeout is set by the rpyc
  opener, and `ping`'s `timeout` now bounds each of its sync requests. The node type cache is
  tied to the `hou` it was read from: a new connection repopulates it on first use,
  and `get_all_types`/`get_categories` drop their unused `host`/`port` arguments.
  Removed as dead: `get_hou`, `quick_health_check`, `_safe_disconnect`, the stub
  `ParameterSchemaCache` and its `parameter_schemas` block in cache stats.
- **Listener reports a dead server as not running** (houdini-laws-etg.qt8):
  `RemoteListener.status()` and `self_test()` report `running: False, stale:
  True` with the endpoint when the held hrpyc server has stopped serving,
  `is_running()` is False, and `start()` lets go of it (logged) and starts
  afresh instead of answering `already_running`; `stop()` reports
  `was_serving` and says when it let go of a server that had already died.
  A stopped listener reports `stale: False`. Remote Status shows the stale
  message as a warning; Stop Remote stops whatever is held. `self_test()`
  carries `message` whether or not a server is held.
  The running state is one frozen `RunningListener` record (server, config,
  policy, bound port) instead of four independently nullable fields, and
  `ListenerConfig` and `SecurityPolicy` are frozen.

- **Input connections have one record shape** (houdini-laws-etg.7xv): every
  tool that reports a wire reports `{input_index, source_node,
  source_output_index}`, built once by `InputConnection.as_record`.
  `list_children` full-mode `inputs` entries rename `index` → `input_index` and
  `output_index` → `source_output_index`. `connect_nodes` returns `node_path`
  and the wire it made as `connection`, replacing `source_node`,
  `destination_node`, `source_output_index` and `destination_input_index`.
  `disconnect_node_input` returns the wire it cut as `disconnected` (or `None`
  when the input was already unwired), replacing `was_connected` and
  `previous_source`; its range check reads the wired span from the same
  connection records instead of `inputs()`. Each `reorder_inputs` reconnection
  is the wire at its new input plus `previous_input_index`, replacing
  `new_input_index` / `old_input_index`. `connect_nodes` reads `connection`
  back from Houdini after the write and resolves `src_path` with `hou.item`,
  so a record naming a subnet indirect input (`/obj/subnet1/1`) goes back as
  reported.

### Fixed

- **A timed-out execute_code worker blocks further mutation**
  (houdini-laws-etg.7kc): after a timeout the caller's program keeps running
  in its thread with its undo group open, and until now any later tool wrote
  the scene alongside it. `WorkerLedger` (`tools/workers.py`) is the one owner
  of "a program is still running against this Houdini": `execute_code` starts
  its thread through it and the thread clears the slot once the program and
  its undo group have ended. `TOOL_POLICIES` (`tools/transactions.py`) now
  classifies every tool, readers included, and the tool boundary looks a
  tool's name up at import, so an unclassified tool cannot exist. Every tool
  whose policy waits for a worker (the scene writers, `execute_code`, and
  `get_geo_summary`, which reads through a program in the same slot) is
  refused while the slot is held, with `error_type: "worker_still_running"`,
  the `operation`, and a `worker` block (`operation`, `code_sha256`, `timeout`,
  `running_for_seconds`); plain reads, `save_scene` and the screenshot tools
  proceed. The timeout response carries the same
  `worker` block and its warning says writes are refused until the worker
  finishes.

- **The token handshake's bound is a deadline, not an idle timeout**
  (houdini-laws-etg.ml7): `HANDSHAKE_TIMEOUT` is counted once, from the moment
  the listener starts reading, for the whole framed token (and likewise for the
  client's wait for the acknowledgement), so a client that keeps one byte
  trickling can no longer hold a listener thread past it. Previously each read
  restarted the timer, so a client announcing a long token and sending it
  slowly pinned the listener's per-connection thread for as long as it kept
  sending.
- **HscriptBatch executes no Python outside code.py's gate**
  (houdini-laws-etg.cg2): the node-type cache used to fill itself by sending a
  Python program over the RPyC connection (`conn.execute`) or `exec`-ing it in
  the server process, through `HscriptBatch._exec_python`, with none of the
  policy scan, bypass gates or audit that `execute_code` applies. The cache now
  reads node types through the `hou` proxy, `HscriptBatch` holds only its
  hscript commands (its four unused exec-backed readers, the `conn` plumbing
  and the convenience wrappers are gone), and the in-process connection no
  longer offers `execute`/`namespace`. The one place a program runs in Houdini
  is the gate in `tools/code.py`.
- **Input connections report the real source output; reorder_inputs preserves
  it** (houdini-laws-etg.kmz): `get_node_info`, `list_children` and
  `reorder_inputs` read the source output index from Houdini's
  `inputConnections()` records. The old `inputConnectors()[idx][1]` read never
  matched HOM's shape, so every index fell back to 0: inspection reported the
  wrong topology and `reorder_inputs` rewired every input from output 0. A
  connection record that cannot be read is now an error naming the node, and
  `reorder_inputs` reads every wire first and then sets each input to its new
  wire in input order, clearing only the inputs left unwired, so no wire is
  unplugged before its replacement is known. `connect_nodes`,
  `disconnect_node_input`, and the `inputs` list and `inputs_count` of
  `get_node_info` read the same records. The source is read as the wire's item
  (`inputItem()` / `inputItemOutputIndex()`), so a node inside a subnet fed from
  one of the subnet's indirect inputs reports that indirect input by path and
  `reorder_inputs` moves it like any other wire; the earlier `inputNode()` read
  looked through to the parent network and was None when nothing was wired
  there. `new_order` must be a permutation of the input indices: a shorter or
  repeated order used to disconnect wires silently and report success, and is
  now rejected before any wire is touched. `list_children` no longer swallows
  errors raised while building a child's record; only a parent refusing to list
  its children is tolerated. The uncalled `HscriptBatch.get_input_connections`
  and `get_output_connections`, second derivations of the same facts with their
  own shapes, are removed.

### Changed

- **Plugin config and lifecycle failures surface with their cause**
  (houdini-laws-etg.o3a): `ListenerConfig.from_env` and the **Start Remote**
  port prompt parse their input through `listener.parse_port` /
  `listener.parse_flag`, which refuse anything that is not a port or a flag with
  a `ValueError` naming the setting and the value; neither falls back to 18811
  or to "off" any more. `self_test()` reports a `hou` that does not import as a
  "Could not read Houdini version" warning with the cause. `status()` and
  `self_test()` of a running listener carry both `local_ip` and
  `local_ip_error`: the address with `local_ip_error: None`, or `local_ip: None`
  beside the cause, in place of the former "localhost" stand-in. `self_test()` lists the machine's address
  among `bound_endpoints` only for a `0.0.0.0` bind, the one bind it is known
  to reach. `RemoteListener.start()`/`reload()` take their config; the
  environment is read by the `remote` facade.

- **`execute_code` read-only is an allowlist**: under `policy="read-only"` the
  program may name only recognized inspection APIs (navigation, parameter
  reads, flags, wiring, geometry reads), a fixed set of pure stdlib modules and
  builtins, and dicts for its own records. Anything else is refused before
  execution, including mutators the old denylist admitted (`changeNodeType`,
  `setExpression`, `setKeyframe`, `deleteAllKeyframes`, `insertInput`,
  `setUserData`, `setComment`), attribute assignment, `with` blocks, attribute
  lookup by string, and any hou API added after this release. The stdlib
  modules are admitted member by member, so `random._os`, `fnmatch.os` and
  `typing.sys` are refused like any other unrecognized name,
  `typing.get_type_hints` is absent because it evaluates string annotations,
  and `hou.expandString` / `hou.text.expandString` are refused because
  backtick expressions run HScript. The refused symbol is named in `mutation_patterns`.
- **`execute_code` dangerous-operation scan reads the parsed program**: the
  `normal` policy now refuses whole modules (`shutil`, `pathlib`, `importlib`,
  `subprocess`, `pickle`, ...) and admits only the read side of `os`, `sys`,
  `io` and `codecs`, closing `os.rename`, `pathlib.Path.unlink`/`write_text`,
  `shutil.copy*` and `importlib.import_module`; archive, database, egress
  and debugger modules (`zipfile`, `tarfile`, `sqlite3`, `smtplib`, `httpx`,
  `pdb`, ...) are refused too, and `tempfile` keeps only its read side. An
  `open()` whose mode is not a string literal, or whose arguments are
  unpacked, is treated as writing. A variable the program binds itself
  (`code = ...`, `signal = 1`) is never mistaken for the module of the same
  name, `import os.path as osp` is judged as `os.path`, and a rename of `hou`
  or a path below it (`hf = hou.hipFile`) keeps its verdict. `open` reached
  by any route but a readable call (`o = open`, `partial(open, ...)`) is
  treated as writing. Heavy-geometry
  methods are judged where they are named, so binding `node.geometry` or
  passing it to `map` is refused like calling it. Text inside comments and
  string literals is no longer flagged: the gate reads the same AST `exec`
  compiles, once per request. Code that does not parse is refused with
  `blocked_reason="syntax_error"`.
- **Listener depends on a declared server contract that real hrpyc satisfies**
  (houdini-laws-etg.zu6): `listener.HrpycModule` and `listener.HrpycServer`
  declare the rpyc surface the listener drives; the test fake and real rpyc are
  both held to it by `tests/plugin/test_hrpyc_contract.py`, which also starts
  the listener on port 0 against real rpyc, over IPv4 and IPv6 (`::1`/`::`
  binds now reach rpyc as IPv6; before, only the fake accepted them). A
  server that fails to close is reported by `stop()` as `error_type:
  "close"` and released. rpyc's per-connection log lines
  now go to `houdini_mcp_plugin.listener.rpyc` (the previous "quiet" switch
  only existed on the fake). The plugin packages are type-checked in CI, and
  that check fails the build.

- **Flags have a cap and an exit plan; the permission decision is one value**
  (houdini-laws-etg.pic):
  - `get_node_info` takes `detail` ("compact" | "standard" | "full", default
    "full") and `cook_info` ("none" | "inspect" | "force_cook", default "none")
    in place of `include_params`, `include_input_details`, `include_errors`,
    `force_cook` and `compact`. Every combination means something: cook info is
    reported at every detail level, and a forced cook always reports.
  - `list_node_types` and `find_nodes` drop the deprecated `max_results` and
    `offset` aliases; `limit` and `cursor` are the paging parameters, as on
    `list_children`. List responses drop the `count` and `next_offset` aliases
    of `returned` and `cursor`.
  - `execute_code` resolves `policy`, `allow_dangerous`, `allow_heavy_geometry`
    and `HOUDINI_MCP_ALLOW_BYPASS` once into one grant, reported as
    `audit.granted`. `allow_dangerous` no longer unlocks heavy geometry, and
    `read-only` now denies dangerous patterns even when a bypass is requested
    and configured. The inert `privileged` policy is removed (it granted nothing
    beyond `normal`); the valid policies are `read-only` and `normal`.
    `get_geo_summary` runs its own bounded program under an internal grant
    instead of a private keyword on `execute_code`.
- **Plugin runtime state has one owner**: the module-level hrpyc API in
  `houdini_mcp_plugin.remote` delegates to a single `RemoteListener` that is
  bound once and never rebound, so `is_hrpyc_running` / `get_hrpyc_status` /
  `self_test_hrpyc` answer from the listener itself instead of a nullable module
  global. `remote.reset_listener` is gone; stop the listener with
  `stop_hrpyc_server()`. The package docstring now describes the two modes that
  exist (stdio and hrpyc remote) without tier or ticket references.
- **One paging contract for list tools**: `list_node_types`, `list_children` and
  `find_nodes` parse `limit`/`cursor` through one enforcer: `limit` is clamped to 1..500 and `cursor` to >= 0 for all
  three, and every response carries the same keys (`total`, `returned`,
  `has_more`, `offset`, plus `cursor`/`warning` when a further page exists);
  `list_node_types` no longer emits `total_matched` (use `total`).

### Fixed

- **Only transport failures are classified as connection loss**
  (houdini-laws-etg.8b3): a `FileNotFoundError`, `PermissionError` or other
  `OSError` raised by a tool (a bad save or render path, a denied file) no longer
  disconnects from Houdini or reports `connection_error` / `recoverable: True`;
  it reaches the caller as its own kind with the connection left standing.
  Connection loss is decided in one place, `connection.transport_failure()`, by
  the installed `HoudiniTarget`: over rpyc it admits only what the channel raises
  (`EOFError`, which is also how rpyc surfaces a reset or broken pipe, and
  `TimeoutError`) and only when Houdini's own code
  did not raise it; in-process (the plugin's stdio server) nothing is connection
  loss. Opening a connection still retries on any socket-layer `OSError`. A
  `TimeoutError` rpyc's sync timeout raised is connection loss (the link stopped
  answering), not an operation error. `CONNECTION_ERRORS` is removed from
  `houdini_mcp.tools`.

- **Listener start no longer races its own serve thread**: `start()` listens
  on the server socket before spawning the accept thread (rpyc's own readiness
  step), so the listener is accepting connections when `start()` returns and
  success no longer depends on thread scheduling. The two-second connect
  poll, its `unreachable` error and the 50ms sleeps are gone; the self-test's
  reachability probe is a single bounded connect. The reported port is
  rpyc's own `server.port`, so `HOUDINI_RPC_BIND_PORT=0` reports the port the
  kernel chose; a failure after the bind closes the server before it is
  reported.
- **Token handshake has one definition** (`houdini_mcp_handshake`, in the plugin tree and shipped with the server): the MCP
  server now presents `HOUDINI_RPC_TOKEN` when connecting, so a listener started
  with a token is reachable from the server (previously only the verify script
  could connect to it). The listener's authenticator and the verify script's
  client side derive from the same module instead of hand-mirrored copies. The
  token is sent length-prefixed and the listener acknowledges a correct one,
  so a client presenting a wrong token gets a distinct, non-retried error
  instead of "Houdini is not running", and a token sent to a listener that
  never answers is a bounded timeout instead of a 30s hang; both sides bound
  the handshake wait, and the listener logs each accepted and rejected
  handshake. A client with no token against a tokened listener is still
  reported as a connection failure, which now names the token as a cause.

### Added

- **Remote hrpyc listener activation** (`houdini_mcp_plugin.listener`): the
  Houdini plugin can now start/stop/reload a remotely reachable hrpyc listener
  with an explicit, configurable bind address/port
  (`HOUDINI_RPC_BIND_HOST` / `HOUDINI_RPC_BIND_PORT`). Start/stop/reload are
  idempotent, and a self-test reports the actual bound endpoints, reachability,
  a loopback-only warning, per-OS firewall guidance, and Houdini/plugin/protocol
  versions. New shelf tools: **Remote Status** and **Remote Self-Test**.
- **Remote listener security policy**: non-loopback binds (`0.0.0.0` or a LAN IP)
  are refused unless explicitly opted into via `HOUDINI_RPC_TRUSTED_NETWORK=1`
  or an authentication token `HOUDINI_RPC_TOKEN`. No code path silently binds
  `0.0.0.0` without auth. Documented in `docs/remote-listener.md` (including
  limitations: token is a shared-secret handshake, not TLS).
- **Opt-in live verification**: `scripts/verify_remote_listener.py` performs a
  read-only reachability/version check against an already-running listener
  (gated by `RUN_LIVE_REMOTE_CHECK=1`); it never starts/stops/restarts Houdini.
- **execute_code safety rails (policy profiles + rollback)**: `execute_code` now
  supports explicit policy profiles (`read-only`, `normal`, `privileged`).
  `read-only` blocks any scene mutation before execution; `privileged` requires
  the server bypass config gate. Bypass flags (`allow_dangerous`,
  `allow_heavy_geometry`) now require BOTH the request flag AND server config
  (`HOUDINI_MCP_ALLOW_BYPASS=true`) and fail closed otherwise. Dangerous-pattern
  detection was expanded to cover network egress (`socket`, `urllib`, `requests`,
  `http`), dynamic execution (`eval`/`exec`/`compile`/`__import__`), and trivial
  whitespace obfuscation. Every call returns a structured `audit` block (policy,
  requested bypasses, detected patterns, code hash, timeout/rollback state) and
  blocked/executed bypass attempts are logged. On timeout the daemon worker and
  its undo group may still be active; the tool deliberately does NOT call
  `hou.undos.performUndo()` because that could undo a previous human action.
  The response reports `rollback.thread_still_running=true` and
  `scene_consistency=unknown`; callers must verify state or restart Houdini and
  must not assume restoration.
- **Heavy geometry guard**: `execute_code` now detects and blocks direct SOP
  geometry access (`node.geometry()`, `geo.points()`, `geo.prims()`,
  `geo.vertices()`, `geo.iterPoints()`, `geo.iterPrims()`) which can stall the
  UI or drop the hrpyc connection on large scenes. Pass `allow_heavy_geometry=True`
  to opt in, or prefer `get_geo_summary()` for bounded inspection.
- **Windows/Codex Desktop setup guide**: `docs/codex-windows-setup.md` documents
  running the stdio MCP server outside Houdini against an `hrpyc` listener.

### Changed

- **BREAKING (safety)**: `execute_code` bypass flags no longer take effect on
  request alone. `allow_dangerous`/`allow_heavy_geometry` now also require the
  server to enable `HOUDINI_MCP_ALLOW_BYPASS`; without it the call fails closed.
- **BREAKING (default behavior)**: `execute_code` rejects heavy geometry access by
  default (see above). Existing callers that rely on raw geometry access must now
  pass `allow_heavy_geometry=True`.
- **stdio transport**: The stdio server no longer passes `host`/`port` and
  suppresses the FastMCP startup banner so stdout stays JSON-RPC clean.
- **uv owns the project**: `pyproject.toml` (dev tools under
  `[dependency-groups]`) and `uv.lock` are the only dependency sources.
  `requirements.txt` is gone; `httpx` and `starlette`, imported directly, are
  now declared, and `python-dotenv`, never imported, is dropped. CI, the Docker image and pre-commit all install from the lock
  (`uv sync --locked`), so every tool runs at one version everywhere.
  Set up with `uv sync && uv run pytest`.
- **BREAKING (floors)**: Python 3.11+ is required; 3.10, end-of-life since
  2026-10, is dropped. `fastmcp>=4.1` and `rpyc>=5.3.1,<6`. The Houdini plugin
  keeps its 3.10 floor (ruff `per-file-target-version`).

### Fixed

- **Plugin stdio server serves the shared MCP server**: the Houdini plugin's
  stdio mode no longer re-implements nine tools with its own (drifted) contract
  and an unguarded `execute_code`. It now runs `houdini_mcp.server` over stdio,
  bound in-process via `houdini_mcp.connection.use_in_process_houdini()`, so
  every tool, safety rail, audit block and response shape is identical across
  transports (`delete_node` returns `deleted_path` in stdio mode too). The
  plugin's own `LocalHoudiniConnection` is gone; `check_connection` and the
  shelf status report a `target` (`host:port` or `this interpreter`). The
  stdio install is `hython -m pip install .`, which also declares `httpx`.
  `houdini_mcp.server.serve()` runs the server without touching process
  logging; `run_server()` remains the CLI entry that configures it.
- **Plugin stdio server lifecycle**: start and running state have one owner,
  `houdini_mcp_plugin.server.StdioServer`. The serving thread is recorded before
  it starts, so two quick "Start MCP" clicks no longer both pass the guard and
  launch two readers on one stdin; a failed foreground serve no longer leaves
  "already running" stuck. `stop_server()` and the **Stop MCP** shelf tool are
  removed: a stdio server ends only when the client closes stdin, and the old
  stop merely flipped a flag while the server kept serving, so the next Start
  ran a second loop. `MCP Status` now reports whether a server is serving.
- **hrpyc remote stop**: The Houdini plugin now starts the listener via
  `hrpyc.ThreadedServer` and stops it through the server object's `close()`,
  replacing the missing `hrpyc.stop_server()` API. The bind host defaults to
  `127.0.0.1` and is configurable via `HOUDINI_RPC_BIND_HOST`.
- **CI**: Added the missing `hypothesis` test dependency (used by the
  `execute_code` safety-rail property/fuzz tests) to the `dev` dependency group.

## [1.0.0] - 2025-12-28

### Added

#### Core Tools
- **Scene Management**: `get_scene_info`, `save_scene`, `load_scene`, `new_scene`, `serialize_scene`
- **Node Operations**: `create_node`, `delete_node`, `get_node_info`, `list_children`, `find_nodes`, `list_node_types`
- **Node Wiring**: `connect_nodes`, `disconnect_node_input`, `reorder_inputs`, `set_node_flags`
- **Parameters**: `set_parameter`, `get_parameter_schema`
- **Geometry**: `get_geo_summary` with point/primitive counts, bounding box, and attribute metadata
- **Materials**: `create_material`, `assign_material`, `get_material_info`
- **Rendering**: `render_viewport`, `render_quad_view` (4 canonical views in one call)
- **Render Configuration**: `list_render_nodes`, `get_render_settings`, `set_render_settings`, `create_render_node`
- **Layout**: `layout_children`, `set_node_position`, `set_node_color`, `create_network_box`
- **Error Introspection**: `find_error_nodes` with cook state and error/warning details
- **Code Execution**: `execute_code` with safety rails and scene diff tracking
- **Documentation**: `get_houdini_help` fetches SideFX documentation for nodes and VEX functions

#### Houdini Plugin
- **stdio MCP mode**: Run MCP server directly inside Houdini without network configuration
- **Shelf tools**: Start MCP, Stop MCP, MCP Status, Start Remote, Stop Remote
- **Package configuration**: Easy installation via Houdini packages system

#### Performance Optimizations
- **Node type caching**: First call fetches and caches all node types; subsequent calls filter from cache (<1ms)
- **Parameter schema caching**: Cached per node type for instant repeated queries
- **Response pagination**: `offset`, `max_results`, `has_more`, `next_offset` for large result sets
- **Response size metadata**: Automatic warnings for large responses with size estimates
- **Parallel execution utilities**: `semaphore_gather`, `batch_items`, `run_in_executor` for bounded concurrency

#### Architecture
- **Modular tools structure**: All tools organized in `houdini_mcp/tools/` with separate modules:
  - `_common.py` - Shared utilities and connection management
  - `cache.py` - In-memory caching infrastructure
  - `code.py` - Python code execution
  - `errors.py` - Error introspection
  - `geometry.py` - Geometry operations
  - `help.py` - Documentation fetching
  - `hscript.py` - HScript execution
  - `layout.py` - Node organization
  - `materials.py` - Material operations
  - `nodes.py` - Node operations
  - `parameters.py` - Parameter operations
  - `rendering.py` - Viewport and ROP rendering
  - `scene.py` - Scene management
  - `summarization.py` - AI-powered summarization
  - `wiring.py` - Node connections

#### Reliability
- **Connection retry with exponential backoff**: Automatic reconnection on connection loss
- **Jitter for thundering herd prevention**: Randomized delays to prevent connection storms
- **Comprehensive error handling**: Graceful handling of RPyC connection errors
- **394 unit tests**: Comprehensive test coverage for all tools

### Changed
- Upgraded from monolithic `tools.py` to modular package structure
- Improved node type validation in `connect_nodes` (validates SOP/OBJ compatibility)
- Enhanced `get_node_info` with optional error introspection and compact mode

### Fixed
- RPyC 6.x compatibility issues (pinned to 5.x for hrpyc compatibility)
- Remote file operations now correctly use RPyC's remote modules

## [0.3.0] - 2025-12-27

### Added
- Initial public release
- Basic MCP server with hrpyc integration
- Core scene and node operations

[1.0.0]: https://github.com/oculairmedia/houdini-mcp/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/oculairmedia/houdini-mcp/releases/tag/v0.3.0
