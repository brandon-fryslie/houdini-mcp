# Houdini MCP Server

An MCP (Model Context Protocol) server for controlling SideFX Houdini via `hrpyc`, enabling AI assistants like Claude and Cursor to interact with Houdini sessions.

## Architecture

```
+-----------------+   MCP (stdio/HTTP)    +------------------+      hrpyc:18811      +-------------+
|  Claude/Cursor  | <------------------> |   MCP Server     | <------------------> |   Houdini   |
|                 |                      |  (Python/FastMCP)|                      |   Session   |
+-----------------+                      +------------------+                      +-------------+
```

## Architecture Decisions

Architecture Decision Records (ADRs) live in [`docs/adr/`](docs/adr/):

- [ADR 0001: Language and Process Boundaries](docs/adr/0001-language-and-process-boundaries.md)
  — why the server stays Python for `hou` semantics and the FastMCP gateway, the
  module boundaries and allowed dependency direction, and the measured thresholds
  that would justify a future non-Python sidecar.

## Features

- **43 MCP tools** across 15 modular categories
- **Full hou module access** - Execute any Houdini Python code remotely
- **Scene management** - Create, load, save scenes
- **Node operations** - Create, delete, modify nodes and parameters
- **Rendering** - Viewport renders, quad views, Karma GPU/CPU support
- **Pane screenshots** - Capture NetworkEditor, SceneViewer, and other panes
- **Scene serialization** - Diff scene states before/after operations
- **Connection management** - Auto-reconnect with exponential backoff + jitter
- **Error handling** - Consistent error responses with recovery hints
- **Response optimization** - Result text in a dense shorthand of its JSON, text over 16KB delivered whole as a file, AI summarization

## Installation

You need Houdini 22 and [uv](https://docs.astral.sh/uv/getting-started/installation/).
macOS on Apple Silicon is the primary platform; [Windows and Linux](#windows-and-linux)
follow the same steps. Install the server, then let it set up Houdini and your MCP
clients:

```bash
uv tool install git+https://github.com/brandon-fryslie/houdini-mcp
houdini-mcp install
```

Restart Houdini and your MCP client, and ask the client to build something in Houdini.
Install with `uv tool install`, not `uvx`: the clients and Houdini run the server and
the plugin from where they are installed, and `uvx` installs them into a cache that
`uv cache clean` removes.

`houdini-mcp install` prints a line for everything it set up:

- **Houdini**: it writes `houdini_mcp.json` into Houdini's `packages` directory
  (`~/Library/Preferences/houdini/22.0/packages` on macOS), pointing Houdini at the
  plugin where uv installed it. The plugin starts the listener the server reaches
  Houdini through, on `127.0.0.1:18811`, as soon as Houdini's UI is up, and adds the
  "Houdini MCP" shelf.
- **MCP clients**: it registers the server with each of Claude Code (user scope),
  Claude Desktop, Cursor and Codex that it finds on this machine. For any other
  client, put the `houdini` entry the installer prints in its config.

Running it again is safe, and keeps anything you added to the server's environment
in a client, such as `HOUDINI_HOST`; an entry you pointed at a URL, as for
[Docker](#docker-houdini-on-another-machine), is left as it is. A step that cannot finish says why, and the
command exits non-zero. To upgrade, run `uv tool upgrade houdini-mcp`, then
`houdini-mcp install` again. From a clone of this repository, `uv run houdini-mcp install`
points Houdini and the clients at the clone, so your edits take effect at their next
restart.

### Starting the listener yourself

The plugin's listener accepts connections on `127.0.0.1` only; to let another
machine reach it, set the variables in [docs/remote-listener.md](docs/remote-listener.md).
The shelf's **Start Remote** starts it on a port you choose when none is running, and
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

Run `houdini-mcp install` on Houdini's machine, as [above](#installation). The
listener binds to `127.0.0.1`, so for a server in a container or on another host set
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
# Edit .env with your Houdini host IP (from Step 1)

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

### Windows and Linux

Windows is not supported.

On Linux, `houdini-mcp install` writes to `~/houdini22.0`, or to the directory
`HOUDINI_USER_PREF_DIR` names when it holds `__HVER__`, as Houdini requires. This has
not been tested on Linux with Houdini 22.

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `HOUDINI_HOST` | `localhost` | Houdini machine IP/hostname |
| `HOUDINI_PORT` | `18811` | hrpyc server port; a value that is not a port stops the server |
| `HOUDINI_RPC_TOKEN` | *(unset)* | Shared secret the Houdini listener enforces; must match the listener's |
| `MCP_HOST` | `127.0.0.1` | The address on this machine the HTTP server answers at: `0.0.0.0` for every interface, or one of its IP addresses; a value that is not an IP address stops the server. Under Docker Compose, the address the container's port is published at, empty meaning the default |
| `MCP_PORT` | `3055` | MCP server HTTP port; a value that is not a port stops the server. Under Docker Compose, the port on this machine the container's port is published at |
| `MCP_ALLOWED_HOSTS` | *(empty)* | Comma-separated names or addresses a client may call the HTTP server by, beyond `localhost`, `127.0.0.1` and `::1`, each as the client's URL spells its host: a name, an IPv4 address, or an IPv6 address in brackets, with no port. A request calling the server by a name it does not admit is refused with 421. Any other entry, such as an empty one, `*`, `box:3055` or `http://box`, stops the server |
| `MCP_TRANSPORT` | `http` | Transport type (http, stdio, sse, streamable-http); any other value stops the server. `http` and `streamable-http` serve MCP at `/mcp`, `sse` at `/sse`: point the client's URL at the path the transport serves |
| `LOG_LEVEL` | `INFO` | Logging level: any name Python's `logging` knows (DEBUG, INFO, WARNING or WARN, ERROR, CRITICAL or FATAL, NOTSET), any case; any other value stops the server |
| `HOUDINI_MCP_RESULTS_DIR` | `~/.cache/houdini-mcp/results` | The root under which each running server keeps results too large to return inline, in a directory of its own: its newest that fit in 64MB. A stopped server's results are removed when another starts |
| `HOUDINI_MCP_ALLOW_BYPASS` | `false` | Whether `execute_code`'s `allow_dangerous` and `allow_heavy_geometry` can be honored at all: true/yes/on/1/t/y or false/no/off/0/f/n, in any case; any other value stops the server |
| `SUMMARIZATION_ENABLED` | `true` | Whether a result too large to read inline is summarized: true/yes/on/1/t/y or false/no/off/0/f/n, in any case; any other value stops the server |
| `SUMMARIZATION_BASE_URL` | `http://100.66.66.66:8000/v1` | The OpenAI-compatible chat-completions server summaries are asked of, which must honor the `chat_template_kwargs` request field (vLLM and omlx do); anything but an http(s) URL with a host, and no credentials, stops the server |
| `SUMMARIZATION_API_KEY` | `1234` | Bearer token for that server |
| `SUMMARIZATION_MODEL` | `Qwen3.8-Flash-Next-Sushi` | The model summaries are asked of; empty stops the server |

Every setting is read once, when the server starts; changing one takes a restart.
Under `MCP_TRANSPORT=stdio`, `MCP_HOST`, `MCP_PORT` and `MCP_ALLOWED_HOSTS` are not read.

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

## Tool Categories (43 Tools)

The server is organized into 15 modular tool categories in `houdini_mcp/tools/`:

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
| `list_node_types` | List available node types by category |

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
| `execute_code` | Execute Python with `hou` available |

### Error Handling (`errors.py`)
| Tool | Description |
|------|-------------|
| `find_error_nodes` | Find all nodes with cook errors |

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
- Verify Houdini is running with hrpyc server started
- Run **Remote Self-Test** in Houdini for this OS's firewall command (on macOS it allows the Houdini executable, not the port)
- Verify HOUDINI_HOST is correct

### Houdini imports the houdini_mcp plugin from another release
- The server refuses to connect, naming the plugin file Houdini imported. Replace
  that install with the plugin from the server's release, then restart Houdini
- An older `houdini-mcp install` left in Houdini's preferences `packages`
  directory loads ahead of `HOUDINI_PACKAGE_DIR`, so the file named may not be
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
├── server.py              # FastMCP server: serves the tool functions in tools/
├── connection.py          # One owner of the RPyC link, with retry/backoff
└── tools/                 # The tools: each function's signature and docstring is its MCP contract
    ├── _common.py         # Shared utilities, error handling
    ├── code.py            # Python execution
    ├── errors.py          # Error node detection
    ├── geometry.py        # Geometry introspection
    ├── help.py            # Houdini help access
    ├── layout.py          # Node layout tools
    ├── link.py            # Connection probes
    ├── materials.py       # Material creation/assignment
    ├── nodes.py           # Node CRUD operations
    ├── pane_screenshot.py # Pane capture tools
    ├── parameters.py      # Parameter get/set
    ├── rendering.py       # Viewport/Karma rendering
    ├── scene.py           # Scene management
    ├── snapshot.py        # Scene snapshot and diff
    ├── summarization.py   # AI response summarization
    └── wiring.py          # Node connection tools

houdini_plugin/            # Houdini plugin, laid out as a packages directory
├── houdini_mcp.json       # Package descriptor
└── houdini_mcp/
    ├── python/            # The hrpyc remote listener, its handshake, the read vocabulary
    │                      # both sides share, and find_nodes', get_parameter_schema's
    │                      # and get_geo_summary's reads
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
