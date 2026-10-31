# Houdini MCP Server

An MCP (Model Context Protocol) server for controlling SideFX Houdini via `hrpyc`, enabling AI assistants like Claude and Cursor to interact with Houdini sessions.

## Architecture

```
+-----------------+   MCP (stdio/HTTP)    +------------------+      hrpyc:18811      +-------------+
|  Claude/Cursor  | <------------------> |   MCP Server     | <------------------> |   Houdini   |
|                 |                      |  (Python/FastMCP)|                      |   Session   |
+-----------------+                      +------------------+                      +-------------+
```

The server runs in your Python environment. The plugin runs inside Houdini and starts
the hrpyc listener the server connects to.

## Architecture Decisions

Architecture Decision Records (ADRs) live in [`docs/adr/`](docs/adr/):

- [ADR 0001: Language and Process Boundaries](docs/adr/0001-language-and-process-boundaries.md)
  — why the server stays Python for `hou` semantics and the FastMCP gateway, the
  module boundaries and allowed dependency direction, and the measured thresholds
  that would justify a future non-Python sidecar.

## Features

- **42 MCP tools** covering scene and node editing, parameters, wiring, materials,
  renders, pane screenshots, code execution and connection checks. See
  [Tool Categories](#tool-categories-42-tools).
- **Full `hou` access**: `execute_code` runs Python with `hou` available, in a process
  of its own that is killed at the program's timeout.
- **One-command install**: `houdini-mcp install` sets up the Houdini plugin and every
  MCP client on the machine, or `houdini-mcp install --project` one project. No clone
  is needed.
- **The listener starts itself**: in a graphical Houdini the plugin starts the hrpyc
  listener once the UI is up, with no shelf click.
- **Renders leave undo alone**: `render_viewport` and `render_quad_view` add no steps to
  the artist's undo history.
- **Build check**: the server refuses a Houdini whose plugin is another build, and
  names the plugin to install, instead of failing later on some call.
- **Scene management**: create, load, save and clear scenes; `execute_code` can report
  what its run changed under `/obj`.
- **Pane screenshots**: capture NetworkEditor, SceneViewer and other panes.
- **Consistent failures**: every failure has a `kind` and a `retry`, with recovery hints.
- **Large results**: a result too large to return inline is written to a file, and
  summarized when summarization is on.

## Installation

You need Houdini 22, Python 3.11 or newer, and
[uv](https://docs.astral.sh/uv/getting-started/installation/). macOS on Apple Silicon
is the supported platform. Linux is untested, and [Windows is not supported](#windows).

```bash
uv tool install git+https://github.com/brandon-fryslie/houdini-mcp
houdini-mcp install
```

Then restart Houdini and your MCP client, and ask the client to build something in
Houdini.

Install with `uv tool install`, not `uvx`: Houdini and the clients run the server and
the plugin from where they are installed, and `uvx` installs them into a cache that
`uv cache clean` removes.

`houdini-mcp install` prints a line for each step it took:

- **Houdini plugin**: it writes `houdini_mcp.json` into Houdini's `packages` directory
  (`~/Library/Preferences/houdini/22.0/packages` on macOS). That file points Houdini at
  the plugin where uv installed it, so upgrading the server upgrades the plugin Houdini
  loads.
- **MCP clients**: it registers the server over stdio with each of Claude Code (user
  scope), Claude Desktop, Cursor and Codex that it finds on this machine. For any other
  client, put the `houdini` entry the installer prints in that client's config.

A step that cannot finish says why, and the command exits 1. Running the command again
is safe: it keeps settings you added to a client's entry, such as `HOUDINI_HOST`. An
entry that points at a URL, as for [Docker](#docker-houdini-on-another-machine), is left
as it is.

To upgrade, run `uv tool upgrade houdini-mcp`, then `houdini-mcp install` again. To
stay on one version, add a commit or a release tag to the URL (`v1.0.0` predates this
installer):
`uv tool install git+https://github.com/brandon-fryslie/houdini-mcp@<commit-or-tag>`.
The installer prints the version and plugin build it set up. From a clone of this
repository, `uv run houdini-mcp install` points Houdini and the clients at the clone, so
your edits take effect at their next restart.

### Installing into one project

To give one project its own locked version in Claude Code, and leave every client's
own config alone, add the server to the project and register it there. In a folder with
no `pyproject.toml`, run `uv init --bare` first.

```bash
cd your-project
uv add --dev git+https://github.com/brandon-fryslie/houdini-mcp
uv run houdini-mcp install --project
```

`uv.lock` holds the commit. The installer adds a `houdini` entry to the project's
`.mcp.json`, keeping the entries already there, and that entry runs the project's
`.venv` by its absolute path, so keep `.mcp.json` out of version control, and run the
command again on each machine and after moving the project. Claude Code asks you to
approve a new `.mcp.json` server the first time it opens the project. If the project
already has a local-scope `houdini` entry, which Claude Code would run instead, the
step fails and names the command that removes it. Other clients do not read
`.mcp.json`: give them the `houdini` entry the installer prints.

Houdini has no notion of a project, so the plugin step still applies to the whole
machine: Houdini loads the build of the last install, and a project on another build is
[refused](#houdini-imports-another-build-of-the-houdini_mcp-plugin) until you run its
install again.

To upgrade the project, run `uv lock --upgrade-package houdini-mcp`, then `uv sync`,
then `uv run houdini-mcp install --project` again.

### Starting the listener yourself

In a graphical Houdini the plugin starts the listener on `127.0.0.1:18811`, or on the
port `HOUDINI_RPC_BIND_PORT` names. The listener accepts connections on `127.0.0.1`
only. To let another machine reach it, set the variables in
[docs/remote-listener.md](docs/remote-listener.md). The shelf's **Start Remote** starts
the listener on a port you choose when none is running, **Stop Remote** stops it, and
**Remote Status** and **Remote Self-Test** report where it listens. A second Houdini
finds the port taken and says so in a dialog.

A headless `hython` runs no UI, so it starts the listener itself, then takes the
editing tools' and renders' calls on its main thread until `main_thread.stop()` is
called from another thread:

```python
from houdini_mcp_plugin import main_thread, start_hrpyc_server

start_hrpyc_server()
main_thread.serve()
```

A Houdini without the plugin can run `import hrpyc; hrpyc.start_server(port=18811)` in
its Python Shell. That listener accepts anyone who can reach the port, from any
interface, and serves only the tools that need nothing inside Houdini: not the ones
that edit the scene as one undo step (`create_node`, `delete_node`, `set_parameter`,
the Wiring & Layout tools, `create_material`, `assign_material`, `set_render_settings`,
`create_render_node`), the pane captures (`capture_pane_screenshot`,
`capture_multiple_panes`, `render_node_network`), the renders (`render_viewport`,
`render_quad_view`), `find_nodes` or `get_parameter_schema`.

### Docker (Houdini on another machine)

To run the server in Docker, on Houdini's machine or another one:

**Step 1: Install the plugin and let the server reach it**

On Houdini's machine, install the plugin from the commit the image is built from: in a
clone at that commit, run `uv run houdini-mcp install`. The server refuses a Houdini
whose plugin is another build, so a plugin from `uv tool install` works only when it is
that same commit.

The listener binds to `127.0.0.1`. For a server in a container or on another host, set
`HOUDINI_RPC_BIND_HOST` **and** opt in with `HOUDINI_RPC_TRUSTED_NETWORK=1` or
`HOUDINI_RPC_TOKEN` (set the same token in the server's environment). See
[docs/remote-listener.md](docs/remote-listener.md); the shelf's **Remote Self-Test**
reports the bound endpoint, reachability and firewall guidance.

**Step 2: Run the Docker MCP server**

```bash
# Clone the repository
git clone https://github.com/brandon-fryslie/houdini-mcp.git
cd houdini-mcp

# Copy and configure environment
cp .env.example .env
# Edit .env: HOUDINI_HOST is the address of the machine running Houdini

# Run with Docker Compose
docker compose up -d
```

**Step 3: Configure your MCP client**

The server answers only clients on this machine; to admit one on another, see
[Admitting a client on another machine](#admitting-a-client-on-another-machine).

```json
{
  "mcpServers": {
    "houdini": {
      "url": "http://localhost:3055/mcp"
    }
  }
}
```

`houdini-mcp install` leaves a URL entry like this one alone.

### Linux

On Linux, `houdini-mcp install` writes to `~/houdini22.0`, or to the directory
`HOUDINI_USER_PREF_DIR` names when it holds `__HVER__`, as Houdini requires. This has
not been tested on Linux with Houdini 22.

### Windows

Windows is not supported.

## Configuration

Environment variables for the server:

| Variable | Default | Description |
|----------|---------|-------------|
| `HOUDINI_HOST` | `localhost` | Houdini machine IP/hostname |
| `HOUDINI_PORT` | `18811` | hrpyc server port; a value that is not a port stops the server |
| `HOUDINI_RPC_TOKEN` | *(unset)* | Shared secret the Houdini listener enforces; must match the listener's |
| `MCP_HOST` | `127.0.0.1` | The address on this machine the HTTP server answers at: `0.0.0.0` for every interface, or one of its IP addresses; a value that is not an IP address stops the server. Under Docker Compose, the address the container's port is published at, empty meaning the default |
| `MCP_PORT` | `3055` | MCP server HTTP port; a value that is not a port stops the server. Under Docker Compose, the port on this machine the container's port is published at |
| `MCP_ALLOWED_HOSTS` | *(empty)* | Comma-separated names or addresses a client may call the HTTP server by, beyond `localhost`, `127.0.0.1` and `::1`, each as the client's URL spells its host: a name, an IPv4 address, or an IPv6 address in brackets, with no port. A request calling the server by a name it does not admit is refused with 421. Any other entry, such as an empty one, `*`, `box:3055` or `http://box`, stops the server |
| `MCP_TRANSPORT` | `http` | Transport type (http, stdio, sse, streamable-http); any other value stops the server. `http` and `streamable-http` serve MCP at `/mcp`, `sse` at `/sse`: point the client's URL at the path the transport serves. The clients `houdini-mcp install` registers run `stdio` |
| `LOG_LEVEL` | `INFO` | Logging level: any name Python's `logging` knows (DEBUG, INFO, WARNING or WARN, ERROR, CRITICAL or FATAL, NOTSET), any case; any other value stops the server |
| `HOUDINI_MCP_RESULTS_DIR` | `~/.cache/houdini-mcp/results` | The root under which each running server keeps results too large to return inline, in a directory of its own: its newest that fit in 64MB. A stopped server's results are removed when another starts |
| `HOUDINI_MCP_ALLOW_BYPASS` | `false` | Whether `execute_code`'s `allow_dangerous` and `allow_heavy_geometry` can be honored at all: true/yes/on/1/t/y or false/no/off/0/f/n, in any case; any other value stops the server |
| `SUMMARIZATION_ENABLED` | `true` | Whether a result too large to read inline is summarized: true/yes/on/1/t/y or false/no/off/0/f/n, in any case; any other value stops the server |
| `SUMMARIZATION_BASE_URL` | `http://100.66.66.66:8000/v1` | The OpenAI-compatible chat-completions server summaries are asked of, which must honor the `chat_template_kwargs` request field (vLLM and omlx do); anything but an http(s) URL with a host, and no credentials, stops the server |
| `SUMMARIZATION_API_KEY` | `1234` | Bearer token for that server |
| `SUMMARIZATION_MODEL` | `Qwen3.8-Flash-Next-Sushi` | The model summaries are asked of; empty stops the server |

Every setting is read once, when the server starts; changing one takes a restart.
Under `MCP_TRANSPORT=stdio`, `MCP_HOST`, `MCP_PORT` and `MCP_ALLOWED_HOSTS` are not read.

Environment variables for the Houdini plugin's listener:

| Variable | Default | Description |
|----------|---------|-------------|
| `HOUDINI_RPC_BIND_HOST` | `127.0.0.1` | The address the listener binds. Another address needs `HOUDINI_RPC_TRUSTED_NETWORK=1` or `HOUDINI_RPC_TOKEN` |
| `HOUDINI_RPC_BIND_PORT` | `18811` | The port the listener binds |
| `HOUDINI_RPC_TRUSTED_NETWORK` | off | Opts in to binding a non-loopback address without a token |
| `HOUDINI_RPC_TOKEN` | *(unset)* | Shared secret the listener enforces |

A listener variable the plugin cannot use is shown in a dialog in Houdini.

### Admitting a client on another machine

By default the HTTP server answers only clients on this machine. To admit one on
another machine, set two settings, in the environment under `uv run` or in `.env`
under Docker Compose:

1. `MCP_HOST` to the interface that client reaches: `0.0.0.0` for every interface,
   or one LAN address of this machine, where a client on this machine must then
   call that address rather than `localhost`.
2. `MCP_ALLOWED_HOSTS` to the name or address that client puts in its URL. Under
   Compose this includes a client calling this machine by its IP address: list
   that IP.

```bash
MCP_HOST=0.0.0.0
MCP_ALLOWED_HOSTS=studio-box.local,192.168.1.10
```

The server has no authentication. Any client that can reach that address can call
every tool, `execute_code` included.

## Tool Categories (42 Tools)

The tools are grouped by module in `houdini_mcp/tools/`. Each tool's parameters and
failure kinds are in [docs/tool-contracts.md](docs/tool-contracts.md).

### Scene & Nodes (`scene.py`, `nodes.py`)
| Tool | Description |
|------|-------------|
| `get_scene_info` | Get current scene info (file, version, nodes) |
| `serialize_scene` | Serialize scene structure for diffs |
| `new_scene` | Replace the scene with an empty one, backing up unsaved work first |
| `save_scene` | Save current scene |
| `load_scene` | Load a .hip file in place of the scene, backing up unsaved work first; a load Houdini warned about carries its warnings |
| `create_node` | Create a new node |
| `delete_node` | Delete a node by path |
| `get_node_info` | Get node details, parameters, connections, errors |
| `list_children` | List child nodes with connection details |
| `find_nodes` | Find nodes by name pattern or type |
| `list_node_types` | List the node types Houdini offers, by category, read on each call |

### Wiring & Layout (`wiring.py`, `layout.py`)
| Tool | Description |
|------|-------------|
| `connect_nodes` | Wire nodes together |
| `disconnect_node_input` | Break a connection |
| `reorder_inputs` | Reorder node inputs |
| `set_node_flags` | Set display/render/bypass flags |
| `layout_children` | Auto-layout child nodes |
| `set_node_position` | Set node position in network |
| `set_node_color` | Set node color |
| `create_network_box` | Create network box around nodes |

### Parameters (`parameters.py`)
| Tool | Description |
|------|-------------|
| `set_parameter` | Set a parameter value |
| `get_parameter_schema` | Get parameter metadata (types, ranges, menus) |

### Geometry & Materials (`geometry.py`, `materials.py`)
| Tool | Description |
|------|-------------|
| `get_geo_summary` | Get geometry statistics and metadata |
| `create_material` | Create a new material |
| `assign_material` | Assign material to geometry |
| `get_material_info` | Get material parameters and shaders |

### Rendering (`rendering.py`)
| Tool | Description |
|------|-------------|
| `render_viewport` | Render viewport with camera control |
| `render_quad_view` | Render Front/Left/Top/Perspective views |
| `list_render_nodes` | List all ROPs in /out |
| `get_render_settings` | Get ROP configuration |
| `set_render_settings` | Modify ROP settings |
| `create_render_node` | Create new ROP with settings |

A render runs on Houdini's main thread with undo disabled, so the artist's undo history
is left as it was.

### Pane Screenshots (`pane_screenshot.py`)
| Tool | Description |
|------|-------------|
| `capture_pane_screenshot` | Capture any Houdini pane as PNG |
| `list_visible_panes` | List capturable panes |
| `capture_multiple_panes` | Batch capture multiple panes |
| `render_node_network` | Navigate into a network and capture it |

### Code Execution (`code.py`)
| Tool | Description |
|------|-------------|
| `execute_code` | Execute Python with `hou` available, in a process of its own, killed at its `timeout` |
| `get_last_scene_diff` | The scene diff of the last `execute_code` run that asked for `capture_diff` |

### Error Handling (`errors.py`)
| Tool | Description |
|------|-------------|
| `find_error_nodes` | Find all nodes with cook errors |

### Connection (`link.py`)
| Tool | Description |
|------|-------------|
| `check_connection` | Connect if needed, then report the Houdini reached: its version, build and hip file |
| `ping_houdini` | Quick connectivity test over a short-lived connection |

### Help & Summarization (`help.py`, `summarization.py`)
| Tool | Description |
|------|-------------|
| `get_houdini_help` | Get help for node types |
| `get_summarization_status` | Get summarization config (server, model, the bound a summary stands in past) |

`find_error_nodes`, `get_geo_summary` and `serialize_scene` summarize a result too large
to read inline by default, so a result that arrives as a file carries its summary beside
the pointer; `summarize=False` sends nothing to the summarizer. The summarizer reads the
first 8KB of the file's text and is told that is part of the result, and
`summarized_bytes` says how many bytes from the file's start the summary covers.

### Infrastructure (`_common.py`)
- **Error handling**: `@tool(policy)` decorator, the tool named by its function; every failure is one arm of `ToolFailure` (`tools/failure.py`), discriminated on `kind`, each arm stating its `retry`
- **Connection retry**: Exponential backoff with jitter
- **Response size**: one bound for every tool, in `houdini_mcp/delivery.py`; a larger result is written whole to a file

## Usage Examples

### Example Prompts

- "Create a sphere → transform → color → OUT network"
- "Insert a mountain node between the grid and noise"
- "Discover what parameters are available on the sphere node"
- "Check if the noise node has any cook errors"
- "Set the sphere radius to 3.0 using the parameter schema"
- "Check the OUT node's point count and bounding box"

## Development

```bash
git clone https://github.com/brandon-fryslie/houdini-mcp.git
cd houdini-mcp

# Install runtime + dev dependencies from the lockfile
uv sync

# Run tests
uv run pytest

# Run the server against Houdini on this machine (set HOUDINI_HOST to reach another)
uv run houdini-mcp
```

[CONTRIBUTING.md](CONTRIBUTING.md) covers linting, the pre-commit hooks and the
tests that run against a live Houdini.

## Troubleshooting

### Connection refused
- Verify Houdini is running. A graphical Houdini starts the listener once its UI is up; a Houdini without the plugin needs the listener started by hand
- Run **Remote Self-Test** in Houdini for this OS's firewall command (on macOS it allows the Houdini executable, not the port)
- Verify HOUDINI_HOST is correct

### Houdini imports another build of the houdini_mcp plugin
- The server refuses to connect, naming the plugin directory Houdini imported and
  both builds. Replace that install with the plugin the server was built with
  (`houdini-mcp install` from the server's install;
  `uv run houdini-mcp install --project` in a project with its own install; for a
  server in Docker, from a clone at the commit its image was built from), then
  restart Houdini. Two
  installs from different commits are different builds even when both state the
  same release
- An older `houdini-mcp install` left in Houdini's preferences `packages`
  directory loads ahead of `HOUDINI_PACKAGE_DIR`, so the directory named may not be
  the plugin you meant Houdini to load

### Tools time out while `ping_houdini` answers
- Close any modal dialog open in Houdini and try again

### Authentication errors
- hrpyc uses no authentication by default
- Ensure you're on a trusted network
- The plugin refuses to bind a non-loopback address unless you explicitly opt
  in (`HOUDINI_RPC_TRUSTED_NETWORK=1`) or set a shared secret
  (`HOUDINI_RPC_TOKEN`). See [docs/remote-listener.md](docs/remote-listener.md)
  for the full security model and its limitations.

## Project Structure

```
houdini_mcp/
├── __main__.py            # The `houdini-mcp` command: serve, or `install`
├── install.py             # `houdini-mcp install`: the plugin pointer and each MCP client
├── server.py              # FastMCP server: serves the tool functions in tools/
├── connection.py          # One owner of the RPyC link, with retry/backoff
├── settings.py            # The environment settings, read once when the server starts
├── delivery.py            # Results too large to return inline, written to files
└── tools/                 # The tools: each function's signature and docstring is its MCP contract

houdini_plugin/            # Houdini plugin, laid out as a packages directory
├── houdini_mcp.json       # Package descriptor
└── houdini_mcp/
    ├── python/            # The hrpyc listener and the modules both sides share
    ├── python3.13libs/    # uiready.py: starts the listener when Houdini's UI is up
    └── toolbar/           # Shelf tools

tests/                     # pytest suite
docs/                      # Tool contracts, remote listener setup, design notes
```

## Credits

- Forked from [oculairmedia/houdini-mcp](https://github.com/oculairmedia/houdini-mcp)
- Based on hrpyc integration patterns from [OpenWebUI Houdini Pipeline](https://github.com/oculairmedia/Houdinipipeline)
- Inspired by [capoomgit/houdini-mcp](https://github.com/capoomgit/houdini-mcp)

## License

MIT
