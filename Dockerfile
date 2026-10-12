FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /bin/uv

WORKDIR /app

# Dependencies first, from the lock alone, so a source edit does not rebuild this layer.
# The uv download cache lives in a build mount, not in the image.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the package itself, as a regular (non-editable) install into the same .venv.
COPY houdini_mcp/ ./houdini_mcp/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# The .venv is the only interpreter with the dependencies; every `python` in the image means it.
ENV PATH="/app/.venv/bin:$PATH"

# Expose MCP server port
EXPOSE 3055

# Run the server
CMD ["python", "-m", "houdini_mcp"]
