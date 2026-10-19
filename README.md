# Houdini MCP Server

An MCP (Model Context Protocol) server for controlling SideFX Houdini via `hrpyc`, enabling AI assistants like Claude and Cursor to interact with Houdini sessions.

## Architecture

```
+-----------------+      MCP (HTTP)       +------------------+      hrpyc:18811      +-------------+
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
- **In-memory caching** - Node type cache with TTL for performance

## Prerequisites

1. **Houdini with RPC enabled** - Start Houdini's RPC server:
   - In Houdini: `Windows > Python Shell`, then run:
     ```python
     import hrpyc

     hrpyc.start_server(port=18811)
     ```
   - Or add to your `123.py` startup script for automatic startup

2. **Network access** - The MCP server must be able to reach Houdini's RPC port (default: 18811)

## Installation

For Codex Desktop on Windows, see the dedicated setup notes in
[`docs/codex-windows-setup.md`](docs/codex-windows-setup.md).

### Option 1: Houdini Plugin (stdio mode)

The Houdini plugin runs the MCP server directly inside Houdini, using stdio transport. This is the simplest setup with no network configuration required. It serves the same server as the other modes - every tool, safety rail and response shape in `houdini_mcp` - connected to the Houdini it runs inside instead of over RPyC.

**Installation:**

1. Copy the `houdini_plugin` folder to your Houdini packages directory:
   ```bash
   # Windows
   copy houdini_plugin %USERPROFILE%\Documents\houdini20.5\packages\houdini_mcp

   # Linux/Mac
   cp -r houdini_plugin ~/houdini20.5/packages/houdini_mcp
   ```

2. Copy the package JSON:
   ```bash
   # Windows
   copy houdini_plugin\houdini_mcp.json %USERPROFILE%\Documents\houdini20.5\packages\

   # Linux/Mac
   cp houdini_plugin/houdini_mcp.json ~/houdini20.5/packages/
   ```

3. Install the server into Houdini's Python. This installs `houdini_mcp` and
   every dependency it declares; there is no separate list to keep in step.
   It upgrades Houdini's bundled `rpyc` 4.x to 5.x, which the hrpyc remote
   listener runs on without change:
   ```bash
   # From the repo root, with Houdini's Python
   hython -m pip install .

   # Or from Houdini's Python Shell
   import subprocess, sys
   subprocess.run([sys.executable, "-m", "pip", "install", "/path/to/houdini-mcp"])
   ```

4. Restart Houdini and find the "Houdini MCP" shelf

**Usage:**
- Click "Start MCP" on the shelf to start the server
- Configure your MCP client (Claude Desktop, Cursor, etc.) to use stdio transport
- The server ends when the MCP client closes its stdin; "MCP Status" shows whether it is serving, and the Houdini version and hip file it serves

**MCP Client Configuration (stdio mode):**
```json
{
  "mcpServers": {
    "houdini": {
      "command": "hython",
      "args": ["-c", "from houdini_mcp_plugin import start_server; start_server(use_thread=False)"]
    }
  }
}
```

### Option 2: Docker (Remote mode)

For production use or when Houdini runs on a different machine, use the Docker-based remote mode. This connects to Houdini via hrpyc/RPyC.

**Step 1: Start hrpyc in Houdini**

If you have the Houdini MCP plugin installed:
- Click **"Start Remote"** on the Houdini MCP shelf
- Use **"Remote Status"** / **"Remote Self-Test"** to confirm the actual bound
  endpoint, reachability, and firewall guidance
- By default the listener binds to `127.0.0.1` (loopback-only). To let a
  remote client (e.g. the Docker gateway on another host) connect, set
  `HOUDINI_RPC_BIND_HOST` **and** opt in with `HOUDINI_RPC_TRUSTED_NETWORK=1`
  or `HOUDINI_RPC_TOKEN` (set the same token in the MCP server's environment).
  See [docs/remote-listener.md](docs/remote-listener.md).

Or manually in Houdini's Python Shell. Every tool but `find_nodes` works this
way; `find_nodes` runs its search inside Houdini, so it also needs the plugin
installed as in [Option 1](#option-1-houdini-plugin-stdio-mode), whose package
JSON puts its modules on Houdini's path when Houdini starts:
```python
import hrpyc

hrpyc.start_server(port=18811)
```

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

```json
{
  "mcpServers": {
    "houdini": {
      "url": "http://localhost:3055"
    }
  }
}
```

**Benefits of Remote Mode:**
- Houdini can run on a different machine (e.g., render farm)
- MCP server runs in Docker for easy deployment
- Full tool set with advanced features
- Server-side processing capabilities

### Local Development

```bash
# Clone and install
git clone https://github.com/brandon-fryslie/houdini-mcp.git
cd houdini-mcp
uv sync

# Run against Houdini on this machine (set HOUDINI_HOST to reach another)
uv run python -m houdini_mcp
```

## Configuration

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `HOUDINI_HOST` | `localhost` | Houdini machine IP/hostname |
| `HOUDINI_PORT` | `18811` | hrpyc server port; a value that is not a port stops the server |
| `HOUDINI_RPC_TOKEN` | *(unset)* | Shared secret the Houdini listener enforces; must match the listener's |
| `MCP_PORT` | `3055` | MCP server HTTP port; a value that is not a port stops the server |
| `MCP_TRANSPORT` | `http` | Transport type (http, stdio, sse, streamable-http); any other value stops the server |
| `LOG_LEVEL` | `INFO` | Logging level: any name Python's `logging` knows (DEBUG, INFO, WARNING or WARN, ERROR, CRITICAL or FATAL, NOTSET), any case; any other value stops the server |
| `HOUDINI_MCP_RESULTS_DIR` | `~/.cache/houdini-mcp/results` | The root under which each running server keeps results too large to return inline, in a directory of its own: its newest that fit in 64MB. A stopped server's results are removed when another starts |
| `HOUDINI_MCP_ALLOW_BYPASS` | `false` | Whether `execute_code`'s `allow_dangerous` and `allow_heavy_geometry` can be honored at all: true/yes/on/1/t/y or false/no/off/0/f/n, in any case; any other value stops the server |
| `SUMMARIZATION_ENABLED` | `true` | Whether a result too large to read inline is summarized: true/yes/on/1/t/y or false/no/off/0/f/n, in any case; any other value stops the server |
| `SUMMARIZATION_BASE_URL` | `http://100.66.66.66:8000/v1` | The OpenAI-compatible chat-completions server summaries are asked of; anything but an http(s) URL with a host, and no credentials, stops the server |
| `SUMMARIZATION_API_KEY` | `1234` | Bearer token for that server |
| `SUMMARIZATION_MODEL` | `mlx-community--Qwen3-30B-A3B-Instruct-2507-8bit` | The model summaries are asked of; empty stops the server |

Every setting is read once, when the server starts; changing one takes a restart.

## Tool Categories (43 Tools)

The server is organized into 15 modular tool categories in `houdini_mcp/tools/`:

### Scene & Nodes (`scene.py`, `nodes.py`)
| Tool | Description |
|------|-------------|
| `get_scene_info` | Get current scene info (file, version, nodes) |
| `serialize_scene` | Serialize scene structure for diffs |
| `new_scene` | Replace the scene with an empty one, backing up unsaved work first |
| `save_scene` | Save current scene |
| `load_scene` | Load a .hip file in place of the scene, backing up unsaved work first |
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
| `render_node_network` | Navigate to node and capture network |

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

### Infrastructure (`_common.py`, `cache.py`)
- **Error handling**: `@handle_connection_errors` decorator; every failure is one arm of `ToolFailure` (`tools/failure.py`), discriminated on `kind` with a fixed `retry`
- **Connection retry**: Exponential backoff with jitter
- **Response size**: one bound for every tool, in `houdini_mcp/delivery.py`; a larger result is written whole to a file
- **Caching**: Node type cache with TTL

## Common Patterns

### Creating SOP Chains

Build a complete SOP network from scratch:

```python
# 1. Create geo container
geo = create_node("geo", "/obj", "my_geo")

# 2. Create SOP nodes
sphere = create_node("sphere", geo["node_path"], "sphere1")
xform = create_node("xform", geo["node_path"], "xform1")
color = create_node("color", geo["node_path"], "color1")
out = create_node("null", geo["node_path"], "OUT")

# 3. Wire nodes together
connect_nodes(sphere["node_path"], xform["node_path"])
connect_nodes(xform["node_path"], color["node_path"])
connect_nodes(color["node_path"], out["node_path"])

# 4. Set display flag
set_node_flags(out["node_path"], display=True, render=True)
```

### Inserting Nodes Into Existing Chains

Insert a new node between existing connections:

```python
# 1. Discover existing network
children = list_children("/obj/geo1")
# Find grid and noise nodes from children

# 2. Get current connections
noise_info = get_node_info("/obj/geo1/noise1")
# See that noise is connected to grid

# 3. Create new node
mountain = create_node("mountain", "/obj/geo1", "mountain1")

# 4. Rewire: grid → mountain → noise
disconnect_node_input("/obj/geo1/noise1", 0)  # Break noise ← grid
connect_nodes("/obj/geo1/grid1", "/obj/geo1/mountain1")  # grid → mountain
connect_nodes("/obj/geo1/mountain1", "/obj/geo1/noise1")  # mountain → noise
```

### Setting Parameters Intelligently

Use parameter schema to set values correctly:

```python
# 1. Discover parameter metadata
schema = get_parameter_schema("/obj/geo1/sphere1", parm_name="rad")
param = schema["parameters"][0]

# 2. Check parameter type
if param["type"] == "vector":
    # Set vector parameter correctly
    set_parameter("/obj/geo1/sphere1", "rad", [3.0, 3.0, 3.0])
elif param["type"] == "menu":
    # Use menu items
    first_option = param["menu_items"][0]["value"]
    set_parameter("/obj/geo1/sphere1", "type", first_option)
```

### Verifying Results

Always verify geometry after operations:

```python
# Get comprehensive geometry summary
summary = get_geo_summary("/obj/geo1/OUT", max_sample_points=10, include_attributes=True)

# Check cook state
if summary["cook_state"] != "cooked":
    # Handle errors
    node_info = get_node_info("/obj/geo1/OUT", cook_info="inspect")
    errors = node_info["cook_info"]["errors"]
    # Fix errors...

# Verify geometry metrics
assert summary["point_count"] > 0
assert summary["primitive_count"] > 0

# Check bounding box
bbox = summary["bounding_box"]
# Verify expected size/position
```

## Error Handling Best Practices

### Check Cook State Before Reading Geometry

```python
# 1. Check cook state first
node_info = get_node_info(node_path, cook_info="force_cook")

cook_state = node_info["cook_info"]["cook_state"]

# 2. Handle different states
if cook_state == "error":
    # Examine errors
    errors = node_info["cook_info"]["errors"]
    for err in errors:
        print(f"Error: {err['message']}")
    # Fix errors...
elif cook_state == "cooked":
    # Safe to access geometry
    geo = get_geo_summary(node_path)
```

### Validate Parameter Types

```python
# Always check parameter schema before setting
schema = get_parameter_schema(node_path, parm_name="rad")
param = schema["parameters"][0]

if param["type"] == "vector":
    # Use list/tuple for vector parameters
    set_parameter(node_path, "rad", [5.0, 5.0, 5.0])
else:
    # Use scalar for single parameters
    set_parameter(node_path, "rad", 5.0)
```

### Handle Connection Errors

```python
# Connection validation
result = connect_nodes(src_path, dst_path)

if result["status"] == "error":
    if "incompatible" in result["message"].lower():
        # Different node categories (e.g., SOP vs OBJ)
        print("Can't connect nodes of different types")
    elif "not found" in result["message"].lower():
        # Node doesn't exist
        print("Source or destination node not found")
```

### Debugging with Error Introspection

```python
# Use cook_info="force_cook" to diagnose issues
node_info = get_node_info(node_path, cook_info="force_cook")

cook_info = node_info["cook_info"]

# Check for errors
if cook_info["errors"]:
    print(f"Node has {len(cook_info['errors'])} errors:")
    for error in cook_info["errors"]:
        print(f"  - {error['message']}")

# Check for warnings
if cook_info["warnings"]:
    print(f"Node has {len(cook_info['warnings'])} warnings:")
    for warning in cook_info["warnings"]:
        print(f"  - {warning['message']}")
```

## Example Workflows

Complete working examples are available in the `examples/` directory:

- **`build_from_scratch.py`** - Build sphere → xform → color → OUT from scratch
- **`augment_existing_scene.py`** - Insert mountain between grid → noise
- **`parameter_workflow.py`** - Discover → set → verify parameters
- **`error_handling.py`** - Detect → fix → verify errors

Run examples:

```bash
python examples/build_from_scratch.py
python examples/augment_existing_scene.py
python examples/parameter_workflow.py
python examples/error_handling.py
```

## Usage Examples

### With Claude/Cursor

Add to your MCP client configuration:

```json
{
  "mcpServers": {
    "houdini": {
      "url": "http://localhost:3055"
    }
  }
}
```

### Example Prompts

- "Create a sphere → transform → color → OUT network"
- "Insert a mountain node between the grid and noise"
- "Discover what parameters are available on the sphere node"
- "Check if the noise node has any cook errors"
- "Set the sphere radius to 3.0 using the parameter schema"

## Development

```bash
# Install runtime + dev dependencies from the lockfile
uv sync

# Run tests
uv run pytest

# Run server locally
uv run python -m houdini_mcp
```

## Troubleshooting

### Connection refused
- Verify Houdini is running with hrpyc server started
- Check firewall allows port 18811
- Verify HOUDINI_HOST is correct

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
├── connection.py          # One owner of the RPyC/in-process link, retry/backoff
└── tools/                 # The tools: each function's signature and docstring is its MCP contract
    ├── _common.py         # Shared utilities, error handling
    ├── cache.py           # Node type caching with TTL, manage_cache
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

houdini_plugin/            # Houdini plugin for stdio mode
├── python/houdini_mcp_plugin/
├── toolbar/               # Shelf tools
└── houdini_mcp.json       # Package descriptor

tests/                     # pytest suite
docs/                      # Implementation documentation
examples/                  # Working example scripts
```

## Credits

- Forked from [oculairmedia/houdini-mcp](https://github.com/oculairmedia/houdini-mcp)
- Based on hrpyc integration patterns from [OpenWebUI Houdini Pipeline](https://github.com/oculairmedia/Houdinipipeline)
- Inspired by [capoomgit/houdini-mcp](https://github.com/capoomgit/houdini-mcp)

## License

MIT
