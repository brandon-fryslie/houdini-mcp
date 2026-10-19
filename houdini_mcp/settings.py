"""The environment settings an entry point reads when it starts the server.

[LAW:effects-at-boundaries] Nothing here reads the environment on its own: each
entry point hands in the mapping it starts with, so a setting is read when the
server starts, never when a module is imported.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, TypeVar

from pydantic import BeforeValidator, Field, TypeAdapter, ValidationError

T = TypeVar("T")

Port = Annotated[int, Field(ge=1, le=65535)]
Host = Annotated[str, Field(pattern=r"^\S+$")]


def _level_named(name: object) -> int:
    # [LAW:one-source-of-truth] logging owns the level names, WARN and NOTSET too
    levels = logging.getLevelNamesMapping()
    if isinstance(name, str) and name.upper() in levels:
        return levels[name.upper()]
    raise ValueError(f"Input should be one of {', '.join(levels)}")


LogLevel = Annotated[int, BeforeValidator(_level_named)]


class InvalidSetting(ValueError):
    """An environment variable holds a value its setting cannot be."""


def setting(environ: Mapping[str, str], name: str, kind: TypeAdapter[T], default: str) -> T:
    """The value the environment variable ``name`` holds, as its type.

    [LAW:parse-dont-validate] the environment is the one place a setting arrives
    as a string; past here it is its type.

    Raises:
        InvalidSetting: naming the variable, what it may hold, and what it held.
    """
    raw = environ.get(name, default)
    try:
        return kind.validate_python(raw)
    except ValidationError as e:
        told = "; ".join(error["msg"] for error in e.errors())
        raise InvalidSetting(f"{name}: {told}, got {raw!r}") from None


@dataclass(frozen=True)
class Target:
    """The Houdini every served tool reaches: the server's configuration, never the caller's."""

    host: str
    port: int


def houdini_target(environ: Mapping[str, str]) -> Target:
    """The Houdini ``HOUDINI_HOST`` and ``HOUDINI_PORT`` name."""
    return Target(
        host=setting(environ, "HOUDINI_HOST", TypeAdapter(Host), "localhost"),
        port=setting(environ, "HOUDINI_PORT", TypeAdapter(Port), "18811"),
    )


def log_level(environ: Mapping[str, str]) -> int:
    """The logging level ``LOG_LEVEL`` names, in any case."""
    return setting(environ, "LOG_LEVEL", TypeAdapter(LogLevel), "INFO")


def results_dir(environ: Mapping[str, str]) -> Path:
    """Where results too large to return inline are written: ``HOUDINI_MCP_RESULTS_DIR``.

    By default one directory per user, the same for every server process, so a
    server's removals bound what earlier ones left too. The override is for a
    deployment that must name where they land.

    Raises:
        InvalidSetting: when none is named and the user has no home directory to hold one.
    """
    named = environ.get("HOUDINI_MCP_RESULTS_DIR")
    if named:
        return Path(named)
    try:
        home = Path.home()
    except RuntimeError as e:
        raise InvalidSetting(f"HOUDINI_MCP_RESULTS_DIR: unset, and {e}") from None
    return home / ".cache" / "houdini-mcp" / "results"
