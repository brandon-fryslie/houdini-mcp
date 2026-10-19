# Quick Reference Guide

## New Tools Available

### 1. list_children()
**Purpose:** List all child nodes with their connection information

**Basic Usage:**
```python
# List immediate children only
list_children("/obj/geo1")

# List all descendants recursively
list_children("/obj/geo1", recursive=True)

# Control recursion depth
list_children("/obj/geo1", recursive=True, max_depth=3)
```

**Example Output:**
```json
{
  "status": "success",
  "node_path": "/obj/geo1",
  "children": [
    {
      "path": "/obj/geo1/grid1",
      "name": "grid1",
      "type": "grid",
      "inputs": [],
      "outputs": ["/obj/geo1/noise1"]
    },
    {
      "path": "/obj/geo1/noise1",
      "name": "noise1",
      "type": "noise",
      "inputs": [
        {
          "input_index": 0,
          "source_node": "/obj/geo1/grid1",
          "source_output_index": 0
        }
      ],
      "outputs": []
    }
  ],
  "total": 2,
  "returned": 2,
  "has_more": false,
  "offset": 0
}
```

**Parameters:**
- `node_path` (required): Parent node path
- `recursive` (default: False): Traverse children recursively
- `max_depth` (default: 10): Maximum recursion depth
- `max_nodes` (default: 1000): Maximum nodes to return

---

### 2. find_nodes()
**Purpose:** Search for nodes by name pattern or type

**Basic Usage:**
```python
# Find all nodes with "noise" in name
find_nodes("/obj", "noise*")

# Find all sphere nodes
find_nodes("/obj/geo1", "*", node_type="sphere")

# Substring search (no wildcards)
find_nodes("/obj", "grid")
```

**Example Output:**
```json
{
  "status": "success",
  "root_path": "/obj",
  "pattern": "noise*",
  "matches": [
    {
      "path": "/obj/geo1/noise1",
      "name": "noise1",
      "type": "noise"
    },
    {
      "path": "/obj/geo2/noise_deform",
      "name": "noise_deform",
      "type": "noise"
    }
  ],
  "total": 2,
  "returned": 2,
  "has_more": false,
  "offset": 0
}
```

**Parameters:**
- `root_path` (default: "/obj"): Root path to search from
- `pattern` (default: "*"): Glob pattern (supports * and ?)
- `node_type` (optional): Filter by node type
- `limit` (default: 100, max: 500): Results per page
- `cursor` (default: 0): Offset of the next page

---

### 3. get_node_info() - EXTENDED
**Purpose:** Get detailed node information including connection details

**Basic Usage:**
```python
# Get full info: identity, connections and parameters (default)
get_node_info("/obj/geo1/noise1")

# Get a minimal record: path, type and counts
get_node_info("/obj/geo1/noise1", detail="compact")

# Get everything except parameter values
get_node_info("/obj/geo1/noise1", detail="standard")
```

**Example Output (detail="full"):**
```json
{
  "status": "success",
  "path": "/obj/geo1/noise1",
  "name": "noise1",
  "type": "noise",
  "type_description": "Noise SOP",
  "children": [],
  "inputs": ["/obj/geo1/grid1"],
  "outputs": ["/obj/geo1/mountain1"],
  "input_connections": [
    {
      "input_index": 0,
      "source_node": "/obj/geo1/grid1",
      "source_output_index": 0
    }
  ],
  "is_displayed": true,
  "is_rendered": false,
  "parameters": {
    "amp": 1.0,
    "freq": [1.0, 1.0, 1.0]
  }
}
```

**Parameters:**
- `detail` (default: "full"): "compact" (path, type, counts), "standard" (adds identity, connections, flags), "full" (adds parameters)
- `cook_info` (default: "none"): "inspect" reports cook state and errors; "force_cook" cooks first

**Enhanced Return Fields:**
- `input_connections`: Array of connection details with indices

---

## Common Use Cases

### Use Case 1: Understand Node Network
```python
# Get overview of all nodes in geometry container
result = list_children("/obj/geo1")

for child in result["children"]:
    print(f"{child['name']} ({child['type']})")
    if child["inputs"]:
        for inp in child["inputs"]:
            print(f"  ← input from {inp['source_node']}")
```

### Use Case 2: Find Specific Node Types
```python
# Find all noise nodes in scene
result = find_nodes("/obj", "*", node_type="noise")

for match in result["matches"]:
    print(f"Found noise node: {match['path']}")
```

### Use Case 3: Insert Node in Chain
```python
# 1. Get connection info
info = get_node_info("/obj/geo1/mountain1")
first_input = info["input_connections"][0]

print(f"Mountain connected to: {first_input['source_node']}")
print(f"Source output index: {first_input['source_output_index']}")
print(f"Target input index: {first_input['input_index']}")

# 2. Now you can insert a node between them with execute_code
code = f"""
# Create blur node
blur = hou.node("/obj/geo1").createNode("blur", "blur1")

# Get nodes
source = hou.node("{first_input["source_node"]}")
target = hou.node("/obj/geo1/mountain1")

# Disconnect
target.setInput({first_input["input_index"]}, None)

# Connect through blur
blur.setInput(0, source, {first_input["source_output_index"]})
target.setInput({first_input["input_index"]}, blur, 0)
"""

execute_code(code)
```

### Use Case 4: Explore Deep Hierarchy
```python
# Recursively explore all nodes in scene
result = list_children("/obj", recursive=True, max_depth=5)

print(f"Found {result['total']} nodes total")

# Build connection graph
for node in result["children"]:
    if node["inputs"]:
        for inp in node["inputs"]:
            print(f"{inp['source_node']} → {node['path']}")
```

---

## Error Handling

All tools return `{"status": "error", "message": "..."}` on failure:

```python
result = list_children("/obj/nonexistent")

if result["status"] == "error":
    print(f"Error: {result['message']}")
    # Output: "Error: Node not found: /obj/nonexistent"
```

---

## Safety Limits

### Every tool:
- A result whose shorthand text is over 16KB arrives as a pointer: `result_file` (path on the
  server's machine), `result_uri` (`houdini-mcp://results/{name}`, a resource the
  server serves), and `result_bytes`
- The pointer keeps `status`, a failure's fields, any `ai_summary` (whole up to
  8KB), and paging fields (a long string cut to fit and marked); the file
  holds the whole result, in the same shorthand
- `summarized_bytes`, beside `ai_summary`, is how many bytes from the file's
  start the summary was written from; it did not see the rest
- Result files are kept until the same server's newer results fill 64MB; the
  oldest are removed, the newest always kept. A stopped server's results are
  removed when another starts

### list_children():
- `max_depth=10`: Prevents infinite recursion
- `max_nodes=1000`: Prevents memory overflow
- Adds `"warning"` to result when limit reached

### find_nodes():
- `limit=100`: One page per call (max 500); `cursor` fetches the next page
- Adds `"warning"` with the next cursor when more pages exist

### get_node_info():
- `max_params=50`: Limits parameter count in output
- Adds `"_truncated": true` to params when limit reached

---

## Tips & Best Practices

1. **Start non-recursive:** Use `recursive=False` first to understand immediate structure
2. **Use type filters:** Combine pattern + node_type for precise searches
3. **Check status:** Always check `result["status"]` before using data
4. **Adjust limits:** Increase max_nodes, or page with cursor, if you need more data
5. **Connection details:** Read `input_connections` (detail "standard" or "full") when planning to modify connections

---

## Testing

Run tests with:
```bash
# Test new tools only
uv run pytest tests/test_tools.py::TestListChildren -v
uv run pytest tests/test_tools.py::TestFindNodes -v
uv run pytest tests/test_tools.py::TestGetNodeInfoExtended -v

# Test all
uv run pytest tests/test_tools.py -v
```

All tests should pass.
