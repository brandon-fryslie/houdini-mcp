# Contributing to Houdini MCP

Thank you for your interest in contributing to Houdini MCP!

## Development Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs
  the Python the project pins
- SideFX Houdini 22 (for integration testing)
- Git

### Initial Setup

```bash
# Clone the repository
git clone https://github.com/brandon-fryslie/houdini-mcp.git
cd houdini-mcp

# Create .venv and install runtime + dev dependencies from uv.lock
uv sync

# Install pre-commit hooks
uv run pre-commit install
uv run pre-commit install --hook-type pre-push
```

## Development Workflow

### Running Tests

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov --cov-report=term-missing

# Run specific test file
uv run pytest tests/test_connection.py

# Run specific test
uv run pytest tests/test_tools.py::TestCreateNode::test_create_node_success
```

### Code Quality

```bash
# Format code
uv run ruff format .

# Lint code
uv run ruff check .

# Auto-fix lint issues
uv run ruff check --fix .

# Type checking
uv run mypy

# Security scan
uv run pre-commit run bandit --all-files
```

### Pre-commit Hooks

Pre-commit hooks run automatically on commit. To run manually:

```bash
# Run all hooks on all files
uv run pre-commit run --all-files

# Run specific hook
uv run pre-commit run ruff --all-files
```

## Testing with Live Houdini

The tests in `tests/integration` run against a real Houdini. One command starts a
headless `hython` serving this checkout's plugin listener (never an installed
`houdini_mcp` package's copy) on a free loopback port, runs every live test against
it, and stops it afterwards, whether the tests pass or fail or the run itself is killed:

```bash
uv run pytest -m integration
```

Each test starts from an empty scene. The run exits non-zero when a test fails or
Houdini does not start; the error carries the end of hython's log.

`HYTHON` names the `hython` to run. It defaults to
`/Applications/Houdini/Current/Frameworks/Houdini.framework/Versions/Current/Resources/bin/hython`.

Plain `pytest` deselects the live tests, as CI does with `-m 'not integration'`, so
one live test runs with the marker too:
`uv run pytest -m integration tests/integration/test_live_houdini.py::test_create_and_delete_node`.
A test added under `tests/integration` is a live test without being marked.

## Code Style

- Use [Ruff](https://docs.astral.sh/ruff/) for formatting and linting
- Follow [PEP 8](https://pep8.org/) style guidelines
- Maximum line length: 100 characters
- Use type hints for function signatures
- Write docstrings for public functions

### Example

A tool's function in `houdini_mcp/tools` is its MCP contract: the server serves
its signature and docstring as they are, with `host` and `port` bound to the
server's Houdini and hidden from clients. A new tool declares its policy in its
`@tool` decorator (the policies are in `houdini_mcp/tools/transactions.py`);
every such function in `houdini_mcp/tools` is served. Its name is its
function's name, and the served names are pinned by
`test_the_served_names_are_the_ones_clients_call`, so adding, renaming or
removing a tool is a deliberate change to what clients call. Clients see the docstring's summary
and its `Args`, so document every argument but `host` and `port` there.

```python
@tool(UNDOABLE)
def create_node(
    node_type: str,
    parent_path: str = "/obj",
    name: str | None = None,
    host: str = DEFAULT_TARGET.host,
    port: int = DEFAULT_TARGET.port,
) -> dict[str, Any]:
    """
    Create a new node in the Houdini scene.

    Args:
        node_type: The type of node to create (e.g., "geo", "sphere")
        parent_path: The parent node path (default: "/obj")
        name: Optional name for the new node

    Returns:
        Dict with created node information or error details.
    """
    ...
```

## Pull Request Process

1. **Fork** the repository
2. Create a **feature branch** (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. **Run tests** (`uv run pytest`)
5. **Commit** with descriptive message (`git commit -m 'Add amazing feature'`)
6. **Push** to your fork (`git push origin feature/amazing-feature`)
7. Open a **Pull Request**

### PR Guidelines

- Reference any related issues
- Include tests for new functionality
- Update documentation if needed
- Ensure CI passes

## Reporting Issues

- Use GitHub Issues
- Include Houdini version
- Include Python version
- Provide minimal reproduction steps
- Include full error traceback

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
