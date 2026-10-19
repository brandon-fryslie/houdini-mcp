# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **BREAKING (Python API)**: importing `houdini_mcp.server` builds no server
  and reads none of `HOUDINI_HOST`, `HOUDINI_PORT`, `LOG_LEVEL`, `MCP_TRANSPORT`
  or `MCP_PORT` (houdini-laws-etg.y04). The `houdini-mcp` command and
  `python -m houdini_mcp` resolve them when they start, and the plugin resolves
  `HOUDINI_HOST` and `HOUDINI_PORT` when Start MCP runs (Houdini owns its
  logging), so a value set after import is the one served. A `HOUDINI_PORT` that
  is not a port, an empty or blank `HOUDINI_HOST`, or a `LOG_LEVEL` that
  `logging` does not name stops the `houdini-mcp` command
  and `python -m houdini_mcp` naming the variable, where before a non-integer
  port raised at import, `99999` was accepted, and an unknown level ran as
  INFO; in Houdini, Start MCP shows the same error. `mcp`, `TARGET`,
  `HOUDINI_HOST` and `HOUDINI_PORT` are gone from `houdini_mcp.server`:
  `build_server(target)` returns the server, `serve(target, ...)` and
  `run_server(target, transport, port, log_level)` take the target, and
  `houdini_mcp.settings.houdini_target(os.environ)` resolves it.
  `houdini_mcp_plugin.serving_target()` names the target the plugin's stdio
  server was started with.

- **BREAKING (wire, Python API)**: closed-domain parameters publish their
  legal values (houdini-laws-etg.4id): `renderer`, `output_format` and
  `karma_engine` on the render tools, `manage_cache`'s `action`, and
  `get_houdini_help`'s `help_type` are enums in the served schema, and the
  server refuses any other value before the tool runs. A tool called from
  Python refuses what the server would, before it runs, as `invalid_input`
  naming the parameter and what it may hold. A Python value is parsed as the
  JSON it would be sent as, strictly: a tuple is accepted as the array it is
  sent as, but `"yes"` is not a boolean and `10.0` is not an integer on either
  path. Matching is exact: `"OpenGL"` was accepted before and is refused now.
  An unknown `execute_code` policy is `invalid_input` too, not an audited
  `unknown_policy` block; `VALID_POLICIES`, `DETAIL_LEVELS` and
  `COOK_INFO_MODES` are gone (`get_args` of `Policy`, `Detail`, `CookInfo`).
  An unknown `MCP_TRANSPORT` or a non-port `MCP_PORT` stops the `houdini-mcp`
  command and `python -m houdini_mcp` with what the variable may hold, instead
  of serving http; the `houdini-mcp` command reads both variables, where it
  ignored them before.

- **Replacing the scene backs up its unsaved work first**
  (houdini-transactions-75y): `new_scene` and `load_scene` save the current
  scene's unsaved work with Houdini's own backup, into the `backup` folder
  beside its hip file, before replacing it, and return the backup's path as
  `checkpoint`. Graphical Houdini no longer asks whether to save first. A scene
  that has never been saved cannot be backed up, so one holding any node is
  refused until `save_scene(file_path)` saves it, and so is a scene whose backup
  Houdini cannot write. A failed load says where the backup is. `execute_code`
  takes no backup.

- **A result's text is a dense shorthand of its JSON**
  (houdini-laws-spine-cqp.kx6): a list of objects names its keys once and
  gives each object one line of values, and a tree's rows indent under their
  parent, each path written from where its parent's ends. The server explains
  the shorthand once, at initialize. Structured content still carries the
  JSON. The 16KB bound measures this text, and a result over it is written to
  its file in the same shorthand.

- **Large results arrive whole, as a file** (houdini-laws-etg.6op): every
  tool's result passes one bound, 16KB. A result over it is written
  whole to a file under `HOUDINI_MCP_RESULTS_DIR`, and the call returns
  its `status` (and a failure's declared fields, any `ai_summary`, and a page's
  paging fields, each string previewed to 2KB), `result_file`,
  `result_bytes`, and `result_uri`, which reads the file through the server
  for a client on another machine. Before, most tools cut
  an oversized result down to a prefix marked `_truncated`, dropping fields
  such as `image_base64`, while `serialize_scene`, renders, screenshots and
  others were not bounded at all. `execute_code`'s `max_stdout_size` now
  takes effect past 8KB. The
  `_response_size_bytes`, `_response_size_note` and `_response_size_warning`
  fields are gone.

- **Scene-writing calls take turns** (houdini-laws-etg.qxp): a tool that
  writes the scene, `execute_code`, `get_geo_summary`, `render_viewport` and
  `render_quad_view` hold the scene while they work in Houdini
  (`get_geo_summary` releases it before its AI summary). Before, two
  concurrent calls could interleave, and a writer could run inside a program's
  open undo group. A call that arrives while another is running waits for it,
  and its response's `scene_wait` names what it waited for and for how long.
  A call cancelled while it waits, because its client timed out, never runs.
  A call that arrives while a timed-out program still runs is refused with
  `worker_still_running` as before. An `execute_code` that arrives while
  another is still running inside its own call now waits for it instead of
  being refused.

- **`find_nodes` searches inside Houdini** (houdini-search-jtw): the match runs
  in `houdini_mcp_search`, a module in the plugin tree, so a search is one
  request to Houdini whatever the scene size, where it was several per node,
  and only the requested page crosses back. The pattern, type and page cross
  as arguments, never as source. A Houdini without this release's plugin -
  including a hand-started `hrpyc` with no plugin installed - fails
  `find_nodes` with `operation_failed` naming the plugin to install.

- **A bypass refusal's hint names only what is still missing**
  (houdini-execute-code-d6q): when `execute_code` refuses dangerous or
  heavy-geometry code, the `hint` lists each of `policy='normal'`, the request
  flag, and `HOUDINI_MCP_ALLOW_BYPASS` that is not yet met, and none that is.
  It named the server setting when it was already enabled, and the flag when
  it was already set.
  The refusal also carries `unmet_requirements` (`policy`, `request`,
  `config`, or `source`), and every audit reports `audit.unmet` per capability. A server-authored program refused dangerous access is
  told that no request input admits it, rather than told to set a flag its tool
  does not have.

- **A scene diff is its own run's alone** (houdini-laws-etg.c91): `execute_code`
  reads the scene before and after the program, and computes the diff, inside
  the program's worker, so no other program can run between the two reads; a
  diff could include another run's changes. Other writing tools are refused
  while those reads run. `get_last_scene_diff` answers for the capture run
  launched last, and has nothing while that run has no diff (still running,
  raised, or its scene could not be read). A run that timed out leaves its
  diff there once its program ends. A call refused because another program is
  still running is not a run and leaves the last diff as it was; it cleared it.
  `timeout` still bounds the program alone, not the reads around it.

- **A program that raises `SystemExit` or `KeyboardInterrupt` fails**
  (houdini-laws-etg.c91): `execute_code` reports it as `operation_failed` with
  its traceback, like any other exception; it returned success. The message of
  any exception a program raises now leads with its type (`ValueError: boom`);
  it was the bare text, which for these is empty or `0`.

- **BREAKING (wire)**: `list_node_types` and `manage_cache("warmup")` fail with
  `operation_failed` when Houdini refuses the node-type walk
  (houdini-laws-etg.29y); they returned success with no types. Nothing is
  cached after a failed read, so the next call reads Houdini again. A Houdini
  that reports no node types is now a cached answer; it was read again on every
  call. `_cache_info.hit` says whether this call was served from the cache; it
  was true for any call once the cache had ever been hit. A node type's
  description Houdini would not give is `{"unread": "<ExcType>: <message>"}`;
  it was `""`.

- **BREAKING (wire)**: a value Houdini would not read is
  `{"unread": "<ExcType>: <message>"}` where the value would be, never a value
  it might have been (houdini-laws-etg.82a). A parameter that will not evaluate
  or convert was `"<unevaluable>"`; `get_node_info`'s errors and warnings were
  `[]` and its cook state `"unknown"`; a forced cook that raised replaced all
  three with a made-up error. `get_geo_summary`'s cook, bounding box (was
  `null`), attributes, groups and sample values (were dropped) take it in place.
  `get_parameter_schema` reports the defaults, ranges and menus Houdini gives:
  a menu's or toggle's default was `null`, a failed tuple default was `0.0`, and
  a parameter whose details raised was dropped; a ramp has no `default` (Houdini's
  is its count of default points). Pane and desktop names and a
  network's child count were `"<unknown>"`, `null` and `0`; `list_visible_panes`
  keeps a pane it cannot measure (it was dropped), and whether a pane is on the
  current desktop or capturable, and `capturable_count`, are unread when what
  they depend on is (they were `false` and a count). `list_children`
  lists the nodes whose children would not list in `unwalked` as
  `{path, unread}` and is `"partial"` (an error when the parent itself will not
  list), and says when `max_depth` stopped it above deeper nodes, or above nodes
  that would not say. A capped response keeps a bounded `unwalked` and
  `uninspected` beside its page. A link lost
  during any of these reads is `connection_lost`, not an unread value. The lint
  and security gates (ruff SIM105, bandit B110/B112) now flag a swallowed
  exception.

- **BREAKING (wire)**: `find_error_nodes` says when it could not inspect part of
  the scene (houdini-laws-etg.4ts). A node whose errors or warnings Houdini would
  not read is listed in `uninspected` as `{path, kind, reason}` and the call is
  `"partial"`; it was skipped, and the call said `"success"` with an
  `error_count` that left it out. `path` is null for a node deleted mid-scan.
  Uninspected nodes count toward `max_results`, which is refused below 1.
  A multi-item tool's nothing-done error names its first failure and how many
  more; the response lists them all. A failure trimmed by the response cap still
  parses as its `kind`. A call that inspected no node, or could not
  list the nodes under `root_path`, is an `operation_failed` error. A link lost
  mid-scan is `connection_lost` (or `timeout`) and resets the connection, where
  it read as a clean scan. The root node is inspected when `root_path` is `"/"`
  too.

- **BREAKING (wire)**: a multi-item tool never says `"success"` over an item it
  did not do (houdini-laws-etg.tmx). `render_quad_view` and
  `capture_multiple_panes` return `status: "partial"` when a view or pane failed,
  as the parameter-writing tools already did. A multi-item call that did none of
  its items is an error carrying its report, its message starting "Nothing was
  done:": `invalid_input` when every item was refused as the caller's input,
  else `operation_failed`. That now includes `set_render_settings` and
  `assign_material` when no write landed, which were `"partial"`; a node the call
  created still makes it `"partial"`. `capture_multiple_panes` captures a pane
  named twice once, and a pane `save_dir` cannot take is that pane's failure
  instead of aborting the call. `set_node_color` and `create_network_box` take `color` as
  three finite numbers in the served schema, so any other length is refused on
  the wire; `create_network_box` no longer creates the box uncolored on a bad
  color, and reports the color it applied.
- **BREAKING (Python API)**: each tool is declared once (houdini-laws-etg.5a0):
  a tool's function in `houdini_mcp/tools` is its MCP contract - signature,
  defaults and docs - and the server serves it with the Houdini address bound,
  so the published schema cannot drift from the function that runs.
  `get_node_info` publishes `max_params` (default 50) and `serialize_scene`
  publishes `max_depth` (default 10), limits that were fixed before; a negative
  one is refused on the wire.
  `list_children` documents `limit` and `cursor`. `find_error_nodes`,
  `get_geo_summary` and `serialize_scene` are coroutines whose `summarize`,
  `host` and `port` are keyword-only, and which summarize a large result for a
  Python caller as they do for an MCP one; `check_connection`, `ping_houdini`
  and `manage_cache` are importable from `houdini_mcp.tools`.

- **BREAKING (wire)**: a render's resolution has one default and no null spelling
  (houdini-laws-etg.o4l): `render_viewport` and `render_quad_view` publish
  `[512, 512]` as the `resolution` default in their schemas, and an omitted
  `resolution` renders at it. `"resolution": null` is now refused on the wire
  like any other value that is not two integers from 64 to 4096; omit the
  argument instead.

- **Connection probes report the Houdini they reached, under bounds they own**
  (houdini-laws-etg.eig): `ping_houdini` returns the same record as
  `check_connection` (`target`, `houdini_version`, `houdini_build`, `hip_file`),
  whose `target` names the Houdini the probe actually used - in-process, this
  interpreter. Both records drop `host` and `port`, which in-process named a
  socket never used, and `connected`, which a success always set; `target`
  carries the host and port for a remote Houdini, and `get_connection_info`
  keeps `connected` to tell its two arms apart. Opening a link proves it by
  importing `hou` across it and reads nothing more, so a fault Houdini raises
  while answering - the version read included - is its own failure
  (`operation_failed` in-process) from every probe and tool, not a connection
  remedy. The probe opens through the same connect path as every tool, one attempt. The TCP connect to a listener is one
  attempt bounded by the handshake deadline (5s) and fails naming the step,
  instead of rpyc's six backoff attempts under a 3s deadline of its own beneath
  the connection module's retries. `hip_file` is what Houdini reports, from
  `get_scene_info` too; no name is invented for it.

- **`execute_code` captures only its own program's output** (houdini-laws-etg.og9):
  while a program runs, prints and stderr writes from other threads reach their
  own streams instead of the program's `stdout`/`stderr` capture, so a log line
  written meanwhile is no longer swallowed. Capture follows the program's thread:
  output from threads the program starts reaches the process's streams. A stream
  the program swaps in is replaced by the prior one when it ends, with a warning.

- **A scene has one snapshot** (houdini-laws-etg.6z7, houdini-laws-etg.l5c): the
  `execute_code` diff, `get_last_scene_diff` and `serialize_scene` all read the
  scene through two hscript calls into one record of every node's path and type.
  Diffs now name a node added or removed inside a network at its own path, in
  place of marking the top-level network "modified", and carry the real type of
  every node in place of `"unknown"` below `/obj`; `modified` means the path now
  holds a node of another type. Hidden nodes are included. A root that does not
  exist, or a node deleted while the scene was read, is an `operation_failed`
  error in place of an empty scene. When the scene cannot be read after the code
  ran, `execute_code` still reports the run and adds `scene_changes_error`.
  `serialize_scene` builds its tree from the same snapshot. `max_diff_nodes`
  caps each list in the diff (`added`, `removed`, `modified`, `added_nodes`),
  and `get_last_scene_diff` returns that capped diff as the last capture returned
  it, or no diff when that capture produced none. `scene_changes_error` survives
  the response size cap.

- **A failed quad view and a failed cache call are arms of the one result type**
  (houdini-laws-etg.60v): a render whose picture Houdini's machine does not
  produce is `operation_failed` in both render tools (with the cause's
  `exception` when one was raised), in place of an unclassified error with a
  traceback. In `render_quad_view` such a
  view is that failure arm beside its `name` and `rotation`, and a view that
  rendered is marked `status: "success"`; anything else (a dead link, a bug)
  fails the whole call. When no view rendered, the call is itself an
  `operation_failed` failure carrying the same `views`, `renderer`,
  `bounding_box` and `total_render_time_ms`, in place of a hand-built
  `status: "error"` with no `kind`. A failure's `message` now ends with the
  notes added on the way out, such as a temporary node that would not go.
  `manage_cache` now enters through the tool boundary: an unknown `action` is
  `invalid_input`, and a warmup that cannot reach Houdini is the connection arm
  that says so, not a bare error message.

- **AI summarization runs by default against the local OpenAI-compatible server**
  (houdini-tuneup-ef3.ktv): summaries are requested over OpenAI chat completions
  from `http://100.66.66.66:8000/v1` with model
  `mlx-community--Qwen3-30B-A3B-Instruct-2507-8bit`, in place of the upstream
  org's Claude proxy at `localhost:8082`, where nothing listened, so
  summarization ran for the first time in this fork. `CLAUDE_PROXY_URL` is
  replaced by `SUMMARIZATION_BASE_URL`, and `SUMMARIZATION_API_KEY` is new;
  `get_summarization_status` returns `base_url` in place of `proxy_url`. A
  summary that could not be made (a cut-off or empty completion, a server
  error with the server's own message, an unreachable server) puts its reason
  in `ai_summary_error` instead of `ai_summary`, and each summary, finished or
  failed, logs the model, the server and how long it took. An unreachable
  server gives up after 3s rather than the 60s request budget, and a
  `SUMMARIZATION_BASE_URL` that is not an http(s) URL with a host, or that
  carries credentials, stops the server at startup. `find_error_nodes`
  summarizes on request or past the size threshold, as the other tools do,
  rather than whenever more than 5 errors are found.

- **Every tool failure is one arm of one result type** (houdini-laws-etg.2m5):
  an error result carries `kind` (`connection_unavailable`, `connection_mismatch`,
  `connection_lost`, `timeout`, `invalid_input`, `operation_failed` or
  `worker_still_running`) and
  the `retry` that kind fixes (`start_houdini`, `call_again`, `fix_input`,
  `wait_for_worker`, `none`), in place of the ad-hoc `error_type` and the
  hard-coded `recoverable: True`. A Houdini that cannot be reached names its
  `target`; a program that outlives its `timeout` is `worker_still_running`
  (wait for that worker), while `timeout` is reserved for a request or link
  that never answered; a live link to a different Houdini than the one named says which
  (`connection_mismatch`, not "start Houdini"); a timeout says whether the
  connection was reset. `capture_multiple_panes` reports
  every pane, each slot a capture or that pane's failure arm. `check_connection`
  and `ping_houdini` report that same failure instead of `status: "disconnected"`
  or `reachable: false`, and a tool result the server cannot serialize is an
  error, not a response marked `_truncated`.

- **Render inputs are refused by the served schema** (houdini-laws-etg.ga7):
  `render_viewport` and `render_quad_view` publish `resolution` as a pair of
  integers in 64–4096 and the camera vectors as three numbers, so a
  one-element or out-of-range resolution, or a two-component camera vector,
  is a schema error at the MCP boundary instead of an error result after the
  call reached Houdini. The server validates every tool's arguments strictly,
  so the string `"512"` or `"yes"` is refused as the schema says, not coerced.

### Removed

- `TransactionManager`, `TransactionRecord`, `TransactionConflict` and
  `RECOVERABLE_TOOL_POLICIES` (houdini-transactions-75y): no tool ever called
  them.

- `houdini_mcp.tools.VALID_PANE_TYPES`, the hand copy of `hou.paneTabType`
  (houdini-laws-etg.8tg). `list_visible_panes` returns the live list under
  `available_types`.

### Fixed

- **Reloading the remote listener with a refused config leaves it serving**
  (houdini-listener-5od): `reload_hrpyc_server()` stopped the running listener
  before the security policy checked the new config, so a refused one (say
  `HOUDINI_RPC_BIND_HOST=0.0.0.0` with no trusted-network opt-in or token) left
  nothing serving. The policy now refuses it before anything is stopped, and
  the error names the listener still serving. `start()` checks the policy
  first too: a refused config beside a running listener is that error, not
  `already_running`. A refusal no longer carries the refused `host` and
  `port`, which read as a live bind beside `running: true`; its reason names
  the host.

- **A note on a Houdini exception no longer replaces it**
  (houdini-transactions-75y): rpyc copies a Houdini exception's `add_note` onto
  it as a string. So when a render tool's temporary node would not go, the
  failure came back as `'str' object is not callable` instead of what Houdini
  raised. It now comes back as Houdini's own failure, with the note.

- **MCP Status shows the Houdini being served from Start** (houdini-laws-etg.58u):
  starting the stdio server opens its in-process link, so the shelf's status
  dialog reports this Houdini's version and hip file before any tool call
  instead of `None`, and an interpreter whose `hou` cannot be imported fails
  at Start instead of on a client's first call. With no link open, MCP Status
  says so instead of printing `None`.

- **Pane types have one source** (houdini-laws-etg.8tg): `list_visible_panes`'s
  `available_types`, the names `capture_pane_screenshot` and
  `capture_multiple_panes` accept, and the list an unknown-type error offers are
  all read from the connected Houdini's `hou.paneTabType`. The hand copy of that
  enum (see Removed) already lacked the `GenericGraphEditor` and
  `UsdShadeEditor` panes Houdini 22 ships, and it answered in place of the live
  enum whenever the live read failed. A name that
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

- **Render tools share one implementation of framing, rig and ROP configuration**
  (houdini-laws-etg.arl): `render_viewport` and `render_quad_view` derive the
  camera pivot, distance, orthographic width and reported `bounding_box` from
  one `Framing` of the displayed geometry's world-space box, build one camera
  rig, and configure the ROP from one per-renderer parm table, instead of each
  carrying its own copy. `look_at` now frames the named node's geometry in
  world space, the same way the scene is framed, and reports that box as
  `bounding_box`; it accepts any object or SOP, frames one with no geometry
  where it stands, and a path that names no node, or a node outside any object,
  is an error instead of being ignored. A SOP that did not cook is left out of
  the frame with a warning naming it. `camera_position` and `camera_rotation`
  that are not three numbers are an error before anything is created. A
  displayed object that contains objects, such as an object subnet, is framed
  by what it displays. A single point is framed as if ten units across instead
  of opening an orthographic view to nothing. `render_quad_view` reports
  `status: error` with a message when no view rendered. A temporary render
  file that cannot be removed is logged, and the image already read is still
  returned. A
  `karma_engine` other than `cpu` or `gpu` is an error instead of silently
  rendering on the CPU. A render that leaves its output file empty is reported
  as producing no image. A node whose displayed geometry cannot be read fails
  the render instead of being silently left out of the frame; a ROP missing a
  parm the renderer table names fails the render naming the parm. Each render
  logs what it framed and what it set on the ROP.

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
  whose policy waits for a worker (the scene writers, `execute_code`,
  `get_geo_summary`, which reads through a program in the same slot, and the
  render tools, which add a camera rig and a ROP for the call) is
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
