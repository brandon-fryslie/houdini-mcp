# .python-version decides the interpreter: uv uses this one when it matches and
# installs the declared one when it does not.
FROM python:3.13-slim
# Must satisfy [tool.uv].required-version in pyproject.toml - uv checks it and fails otherwise.
COPY --from=ghcr.io/astral-sh/uv:0.13.2 /uv /bin/uv
# The cache is a build mount on another filesystem, so hardlinks cannot work; compile
# bytecode at build time rather than on every container start.
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1

WORKDIR /app

# Dependencies first, from the lock alone, so a source edit does not rebuild this layer.
# The uv download cache lives in a build mount, not in the image.
COPY .python-version pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Then the package itself (README.md is its long_description), as a regular
# (non-editable) install into the same .venv.
COPY README.md ./
COPY houdini_mcp/ ./houdini_mcp/
# The token handshake is defined once, in the plugin tree; pyproject ships it from there.
COPY houdini_plugin/python/houdini_mcp_handshake/ ./houdini_plugin/python/houdini_mcp_handshake/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# The .venv is the only interpreter with the dependencies; every `python` in the image means it.
ENV PATH="/app/.venv/bin:$PATH"

# Expose MCP server port
EXPOSE 3055

# Run the server
CMD ["python", "-m", "houdini_mcp"]
