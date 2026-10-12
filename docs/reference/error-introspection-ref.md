# HDMCP-8 Quick Reference: Node Error Introspection

## Overview
Extended `get_node_info()` to optionally include cook state and error/warning information.

## Usage

### Basic (Backward Compatible)
```python
# Default - no error info
info = get_node_info("/obj/geo1/sphere1")
# Returns: standard node info (no cook_info)
```

### With Error Checking
```python
# Include cook state and errors
info = get_node_info("/obj/geo1/sphere1", cook_info="inspect")

# Access cook info
print(info["cook_info"]["cook_state"])  # "cooked", "error", "dirty", "uncooked"
print(info["cook_info"]["errors"])  # List of error dicts
print(info["cook_info"]["warnings"])  # List of warning dicts
```

### Force Cook First
```python
# Cook node before checking errors
info = get_node_info("/obj/geo1/sphere1", cook_info="force_cook")
# Returns: fresh cook info with last_cook_time
```

## Return Format

### With `cook_info="none"` (default)
```json
{
  "status": "success",
  "path": "/obj/geo1/sphere1",
  "type": "sphere",
  "name": "sphere1",
  "children": [],
  "inputs": [],
  "outputs": []
}
```

### With `cook_info="inspect"`
```json
{
  "status": "success",
  "path": "/obj/geo1/sphere1",
  "type": "sphere",
  "cook_info": {
    "cook_state": "error",
    "errors": [
      {
        "severity": "error",
        "message": "Invalid parameter value",
        "node_path": "/obj/geo1/sphere1"
      }
    ],
    "warnings": [
      {
        "severity": "warning",
        "message": "Performance warning",
        "node_path": "/obj/geo1/sphere1"
      }
    ]
  }
}
```

### With `cook_info="force_cook"`
```json
{
  "cook_info": {
    "cook_state": "cooked",
    "errors": [],
    "warnings": [],
    "last_cook_time": 1234567890.123
  }
}
```

## Cook States

| State | Meaning |
|-------|---------|
| `"cooked"` | Node successfully cooked |
| `"error"` | Cook failed with errors |
| `"dirty"` | Needs to be recooked |
| `"uncooked"` | Never been cooked |

## Common Patterns

### Check for Errors
```python
info = get_node_info(node_path, cook_info="inspect")
if info["cook_info"]["cook_state"] == "error":
    for err in info["cook_info"]["errors"]:
        print(f"Error: {err['message']}")
```

### Check for Warnings
```python
info = get_node_info(node_path, cook_info="inspect")
for warn in info["cook_info"]["warnings"]:
    print(f"Warning: {warn['message']}")
```

### Force Fresh Cook
```python
# Cook and immediately check for errors
info = get_node_info(node_path, cook_info="force_cook")
```

### Combined with Parameters
```python
# Get params AND cook info
info = get_node_info(node_path, cook_info="inspect")
# Returns both parameters and cook_info
```

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `node_path` | str | Required | Full path to node |
| `detail` | "compact" / "standard" / "full" | "full" | How much of the node to return |
| `cook_info` | bool | False | Include cook state/errors |
| `cook_info="force_cook"` | bool | False | Cook before checking errors |

## Edge Cases

- **Empty errors/warnings**: Returns `[]` not `null`
- **Node not found**: Returns `{"status": "error", ...}`
- **Any `detail` level**: `cook_info` is reported at every level, including "compact"
- **Unknown cook state**: Falls back to lowercase string
- **Special characters**: Preserved in error messages

## Test Coverage

- ✓ 15 new tests for error introspection
- ✓ 24 total get_node_info tests (all passing)
- ✓ 98 total tests in test suite
- ✓ 100% backward compatibility

## Files Modified

- `houdini_mcp/tools.py` - Core implementation
- `houdini_mcp/server.py` - MCP tool exposure
- `tests/conftest.py` - Mock support
- `tests/test_node_errors.py` - New tests

## Demo

Run the demo script:
```bash
python3 hdmcp8_demo.py
```
