"""Allow running as: python -m houdini_mcp"""

import os
from typing import get_args

from fastmcp.server.server import Transport
from pydantic import TypeAdapter, ValidationError

from .server import run_server


def parse_transport(name: str) -> Transport:
    """The transport ``MCP_TRANSPORT`` names, or exit naming the legal ones.

    [LAW:parse-dont-validate] the environment is the one place a transport
    arrives as a string; past here it is a Transport.
    """
    try:
        return TypeAdapter(Transport).validate_python(name)
    except ValidationError:
        legal = ", ".join(get_args(Transport))
        raise SystemExit(f"MCP_TRANSPORT must be one of {legal}, got {name!r}") from None


if __name__ == "__main__":
    port = int(os.getenv("MCP_PORT", "3055"))
    run_server(transport=parse_transport(os.getenv("MCP_TRANSPORT", "http")), port=port)
