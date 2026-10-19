# Tool Contracts and Common Pitfalls

This document clarifies the input/output contracts for Houdini MCP tools and documents common mistakes discovered through usage.

## Every Tool: Large Results Arrive as a File

A result whose text (the shorthand below) is over 16KB is not returned inline,
and the file holds all of it in the same shorthand. The call returns its
`status`, a failure's fields, any `ai_summary` (whole up to 8KB), a page's
paging fields (a long string cut to fit and marked), and a pointer to the whole
result: `result_file`, its path on the server's machine; `result_uri`, which
reads it through the server (`houdini-mcp://results/{name}`) for a client on
another machine; and `result_bytes`. A `render_viewport` image is usually over
the bound, so its `image_base64` is in the file.

Read the file soon. A result file is kept until the same server's newer
results fill 64MB; then the oldest are removed. The newest result is always
kept, and none is removed while it is still being written. A server's
results are removed after it stops, when another server starts under the same
`HOUDINI_MCP_RESULTS_DIR`. Reading a `result_uri` the server no longer holds
fails and gives this rule.

## Every Tool: The Text Is a Shorthand of the JSON

A result's structured content is its JSON. Its text content, the part a model
reads, is the same value in a shorthand that names each list's keys once and
writes each element as one row. A tree's rows indent under their parent, and a
path is written from where its parent's ends (`~`):

```
structure:
 path: /obj
 children[1]{path,type,name}/children:
  ~/geo1,geo,geo1
   ~/box1,box,box1
```

Every client is told how to read it at initialize. The shorthand is lossless:
it reads back as exactly the JSON.

## execute_code

### The `hou` Module is Pre-Injected

**IMPORTANT:** The `hou` module is pre-injected as a global variable via RPyC. **Do NOT** use `import hou` or `from hou import ...` in your code.

#### ❌ Wrong:
```python
execute_code(
    code="""
import hou  # This will fail with ModuleNotFoundError
node = hou.node('/obj')
"""
)
```

#### ✅ Correct:
```python
execute_code(
    code="""
# hou is already available - use it directly
node = hou.node('/obj')
geo = node.createNode('geo', 'my_geo')
"""
)
```

### Why This Happens

The `execute_code` tool runs your code remotely in Houdini via RPyC. It injects the remote `hou` object directly into your execution namespace. The `hou` module is not installed in the Python environment where the MCP server runs, so attempting to import it will fail.

If you try to import `hou`, you'll now receive a helpful error message:

```json
{
  "status": "error",
  "message": "Code attempts to import hou module, but hou is already available",
  "hint": "The 'hou' module is pre-injected as a global variable by execute_code via RPyC. You can use 'hou' directly without importing it...",
  "suggested_fix": "# import hou  # Not needed - hou is pre-injected\n..."
}
```

### Timeouts Leave a Running Worker

A program that exceeds `timeout` cannot be stopped; it keeps running with its
undo group open. The response is the `worker_still_running` arm, since waiting
for that worker is the remedy, with `timeout`, a `warning`,
`rollback.scene_consistency: "unknown"`, and the `worker` block:

```python
{
    "status": "error",
    "kind": "worker_still_running",
    "retry": "wait_for_worker",
    "timeout": 30,
    "worker": {
        "operation": "execute_code",
        "code_sha256": "...",
        "timeout": 30,
        "running_for_seconds": 30.0,
    },
    "audit": {"timed_out": True, "code_sha256": "..."},
}
```

Until that program ends, every scene-writing tool, another `execute_code`,
`get_geo_summary` (which reads through a program in the same worker slot), and
`render_viewport` and `render_quad_view` (which add a camera and a ROP for
the call) return
`kind: "worker_still_running"` (with `retry: "wait_for_worker"`) naming the same
worker; plain reads,
`save_scene` and screenshots still work. See `docs/implementation/error-handling-patterns.md`.

### Concurrent Calls Wait Their Turn

Those same tools hold the scene while they work in Houdini, so two of them
never run at once; `get_geo_summary` releases it before its AI summary. A call that arrives while another one is running waits for it to
finish, then runs. Its response, success or error, carries `scene_wait`,
which names the calls that held the scene while it waited (in the order they
held it) and how long it waited:

```python
{"status": "success", "scene_wait": {"waited_for": ["set_parameter"], "seconds": 0.42}}
```

A call that finds the scene free has no `scene_wait`. A call still waiting
when a timed-out program takes over the scene is refused with
`worker_still_running`, as above. A call cancelled while it waits (its client
timed out or gave up) leaves the queue and never runs. A call cancelled after
it started still finishes its write.

## render_viewport

### Parameter Schema

The `render_viewport` tool accepts specific parameters. Do not use intuitive-but-wrong parameter names from prose descriptions.

#### ❌ Wrong Parameters:
```python
# The server refuses these: they are not in render_viewport's schema
render_viewport(width=512, height=512)  # Use resolution=[512, 512]
render_viewport(camera_path="/obj/cam1")  # Use camera_position or camera_rotation
```

#### ✅ Correct Parameters:
```python
# Use resolution as a list
render_viewport(resolution=[512, 512])

# For camera control, use camera_position and/or camera_rotation; the rotation aims it
render_viewport(camera_position=[0.0, 0.0, 10.0], camera_rotation=[0, 0, 0])  # Facing the origin

# Or use look_at to focus on a node
render_viewport(look_at="/obj/geo1")
```

### Complete Parameter List

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `camera_position` | `[float, float, float]` | [x, y, z] world position of the camera | Where `camera_rotation` looks at the center of what is framed, from far enough back to see all of it |
| `camera_rotation` | `[float, float, float]` | [rx, ry, rz] world rotation of the camera in degrees, turned about X, then Y, then Z. It alone sets where the camera looks, wherever the camera stands: [0, 0, 0] looks down -Z | [-30, 45, 0] |
| `look_at` | `str` | Path of an object or SOP to frame instead of the scene's; one with no geometry is framed where it stands; an unknown path, any other node, or a SOP outside any object, is an error | None |
| `resolution` | `[int, int]`, each 64–4096 | [width, height] in pixels | [512, 512] |
| `renderer` | enum | Exactly "opengl" or "karma" | "opengl" |
| `output_format` | enum | Exactly "png", "jpg", or "exr" | "png" |
| `auto_frame` | `bool` | Auto-frame visible geometry | True |
| `orthographic` | `bool` | Use orthographic projection | False |
| `karma_engine` | enum | Exactly "cpu" or "gpu"; always checked, used by Karma only | "cpu" |

### Minimal Valid Examples

```python
# Most minimal: defaults, auto-frame everything
render_viewport()

# Explicit resolution
render_viewport(resolution=[1024, 768])

# Focus on specific node
render_viewport(look_at="/obj/geo1")

# Orthographic front view
render_viewport(camera_rotation=[0, 0, 0], orthographic=True)

# Top-down view
render_viewport(camera_rotation=[-90, 0, 0])

# Fast Karma GPU render
render_viewport(renderer="karma", karma_engine="gpu", resolution=[512, 512])
```

## render_quad_view

### Parameter Schema

Similar to `render_viewport`, but renders 4 canonical views (Front, Left, Top, Perspective) in one call.

| Parameter | Type | Description | Default |
|-----------|------|-------------|---------|
| `resolution` | `[int, int]`, each 64–4096 | [width, height] for each view | [512, 512] |
| `renderer` | enum | Exactly "opengl" or "karma" | "opengl" |
| `output_format` | enum | Exactly "png", "jpg", or "exr" | "png" |
| `orthographic` | `bool` | Use ortho for Front/Left/Top | True |
| `include_perspective` | `bool` | Include perspective view | True |
| `karma_engine` | enum | Exactly "cpu" or "gpu"; always checked, used by Karma only | "cpu" |

### Minimal Valid Examples

```python
# Default: 4 views with orthographic projection
render_quad_view()

# Only 3 orthographic views (no perspective)
render_quad_view(include_perspective=False)

# Higher resolution with Karma
render_quad_view(resolution=[1024, 1024], renderer="karma")

# Fast GPU rendering
render_quad_view(renderer="karma", karma_engine="gpu")
```

## Values Houdini Would Not Read

Any response field Houdini refused to supply holds `{"unread": "<ExcType>: <message>"}`
where its value would be: a parameter value, a cook state, a node's errors, a
default, a bounding box, an attribute in its list. It is never `[]`, `0`, `null`,
`"unknown"` or a placeholder string, so an empty error list means the node has
no errors. A walk that could not enter part of the tree (`list_children`) lists
those nodes in `unwalked` as `{path, unread}` and is `"partial"`.

## Schema Validation

Every tool publishes a JSON schema for its arguments, and the server validates each call against it. An unexpected argument name or a wrong type is refused at the MCP boundary, before any RPC call to Houdini.

This is intentional - it catches typos and misunderstandings early, with clear error messages.

## Resolution Constraints

Both `render_viewport` and `render_quad_view` enforce resolution limits:

- **Minimum:** 64x64 pixels
- **Maximum:** 4096x4096 pixels

Attempting to render outside these bounds will return an error immediately.

## Common Patterns

### Using execute_code for Complex Operations

When other tools don't cover your use case, `execute_code` is the escape hatch. Remember:

1. `hou` is pre-injected - don't import it
2. Use `print()` for output - it will appear in `result["stdout"]`
3. Heavy geometry operations are blocked by default; `allow_heavy_geometry=True` unlocks them only under policy "normal" with `HOUDINI_MCP_ALLOW_BYPASS` set server-side (prefer `get_geo_summary`)
4. Dangerous operations are blocked by default; `allow_dangerous=True` unlocks only them, under the same two conditions
5. `policy="read-only"` blocks every mutation and grants nothing to either flag; the resolved decision is reported in `audit.granted`
6. A refusal for either flag lists `unmet_requirements` - each of `policy`, `request` (the flag), `config` (`HOUDINI_MCP_ALLOW_BYPASS`), or `source` (a server-authored program, which no input admits) still missing - and the `hint` asks for exactly those; `audit.unmet` reports the same per capability on every run

```python
result = execute_code(
    code="""
# Create a complete SOP chain
obj = hou.node('/obj')
geo = obj.createNode('geo', 'my_network')
sphere = geo.createNode('sphere')
xform = geo.createNode('xform')
xform.setFirstInput(sphere)
print(f"Created: {xform.path()}")
"""
)

print(result["stdout"])  # "Created: /obj/my_network/xform"
```

### Rendering the Current Scene

For quick previews, just call `render_viewport()` with defaults:

```python
# Auto-frame and render with defaults
result = render_viewport()
if result["status"] == "success":
    image_data = result["image_base64"]
    # Use the base64-encoded image
```

### Focusing on Specific Geometry

Use `look_at`, without `camera_position`, to center the camera on a specific node:

```python
result = render_viewport(look_at="/obj/my_geo", orthographic=True, resolution=[1920, 1080])
```

## Testing Your Code

Before using a tool, check:

1. **Parameter names** - Use the exact names from the tool signature
2. **Parameter types** - Lists for resolution/position/rotation, not separate arguments
3. **Parameter values** - Within valid ranges (e.g., resolution 64-4096)

The served schema refuses incorrect parameter names before the call reaches Houdini.
