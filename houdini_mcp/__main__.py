"""The ``houdini-mcp`` command, also run as: python -m houdini_mcp"""

import os
from collections.abc import Mapping
from typing import Annotated, TypeVar

from fastmcp.server.server import Transport
from pydantic import Field, TypeAdapter, ValidationError

from .server import run_server

T = TypeVar("T")

Port = Annotated[int, Field(ge=1, le=65535)]


def setting(environ: Mapping[str, str], name: str, kind: TypeAdapter[T], default: str) -> T:
    """The value the environment variable ``name`` holds, or exit naming what it may hold.

    [LAW:parse-dont-validate] the environment is the one place a setting arrives
    as a string; past here it is its type.
    """
    raw = environ.get(name, default)
    try:
        return kind.validate_python(raw)
    except ValidationError as e:
        told = "; ".join(error["msg"] for error in e.errors())
        raise SystemExit(f"{name}: {told}, got {raw!r}") from None


def main() -> None:
    """Serve on the transport and port the environment names."""
    run_server(
        transport=setting(os.environ, "MCP_TRANSPORT", TypeAdapter(Transport), "http"),
        port=setting(os.environ, "MCP_PORT", TypeAdapter(Port), "3055"),
    )


if __name__ == "__main__":
    main()
