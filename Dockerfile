FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv

WORKDIR /app

# Install the locked runtime dependency set, then the package itself.
COPY pyproject.toml uv.lock README.md ./
COPY houdini_mcp/ ./houdini_mcp/
RUN uv sync --locked --no-dev --no-editable

# Expose MCP server port
EXPOSE 3055

# Run the server
CMD ["/app/.venv/bin/python", "-m", "houdini_mcp"]
