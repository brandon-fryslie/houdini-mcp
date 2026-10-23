"""The environment settings an entry point reads when it starts the server.

[LAW:effects-at-boundaries] Nothing here reads the environment on its own: each
entry point hands in the mapping it starts with, so a setting is read when the
server starts, never when a module is imported.
"""

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Annotated, Literal, TypeVar

import houdini_mcp_handshake as handshake
import httpx
from fastmcp.server.server import Transport
from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    IPvAnyAddress,
    TypeAdapter,
    ValidationError,
)

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


def _api_base(raw: str) -> str:
    """An OpenAI API base such as ``http://host:8000/v1``, as its URL spells it."""
    try:
        url = httpx.URL(raw)
    except httpx.InvalidURL:
        # httpx's reason quotes parts of the value, a password among them; the refusal
        # shows the value once, masked.
        raise ValueError("not a URL") from None
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError("must be an http(s) URL with a host")
    # The URL is logged and reported by get_summarization_status, so it holds no secret.
    if url.userinfo:
        raise ValueError("carries credentials; set SUMMARIZATION_API_KEY")
    return str(url)


ApiBase = Annotated[str, AfterValidator(_api_base)]


def _masked(url: str) -> str:
    """``url`` as an error may show it: all of it before its last ``@`` masked, but
    a leading ``scheme://``.

    A refused URL may not parse, so where its credentials end is unknown; whatever
    precedes an ``@`` may be one.
    """
    return repr(re.sub(r"^(\w+://)?.*@", r"\1***@", url, flags=re.DOTALL))


class InvalidSetting(ValueError):
    """An environment variable holds a value its setting cannot be."""


def setting(
    environ: Mapping[str, str],
    name: str,
    kind: TypeAdapter[T],
    default: str,
    shown: Callable[[str], str] = repr,
) -> T:
    """The value the environment variable ``name`` holds, as its type.

    [LAW:parse-dont-validate] the environment is the one place a setting arrives
    as a string; past here it is its type.

    Raises:
        InvalidSetting: naming the variable, what it may hold, and what it held,
            as ``shown`` renders it.
    """
    raw = environ.get(name, default)
    try:
        return kind.validate_python(raw)
    except ValidationError as e:
        told = "; ".join(error["msg"] for error in e.errors())
        raise InvalidSetting(f"{name}: {told}, got {shown(raw)}") from None


@dataclass(frozen=True)
class Target:
    """The Houdini every served tool reaches: the server's configuration, never the caller's."""

    host: str
    port: int


@dataclass(frozen=True)
class Summarizer:
    """The OpenAI-compatible completions server a summary is asked of, and whether any is."""

    enabled: bool
    base_url: str
    # Hidden from repr, so a logged setting never carries the key.
    api_key: str = field(repr=False)
    model: str


@dataclass(frozen=True)
class ServerSettings:
    """What every served tool runs under: the server's configuration, never the caller's."""

    target: Target
    summarizer: Summarizer
    # HOUDINI_MCP_ALLOW_BYPASS: the configuration half of execute_code's bypass opt-in.
    bypass_config_enabled: bool

    def bound(self) -> dict[str, object]:
        """Each value a served tool takes from the server, by the parameter it takes it as."""
        return {
            "host": self.target.host,
            "port": self.target.port,
            "summarizer": self.summarizer,
            "bypass_config_enabled": self.bypass_config_enabled,
        }


def houdini_target(environ: Mapping[str, str]) -> Target:
    """The Houdini ``HOUDINI_HOST`` and ``HOUDINI_PORT`` name."""
    return Target(
        host=setting(environ, "HOUDINI_HOST", TypeAdapter(Host), "localhost"),
        port=setting(environ, "HOUDINI_PORT", TypeAdapter(Port), "18811"),
    )


def summarizer(environ: Mapping[str, str]) -> Summarizer:
    """The summarizer the ``SUMMARIZATION_*`` variables name: by default, the local omlx
    server on the tailnet, so summarization works with no configuration.

    The SUMMARIZATION_ prefix keeps an OPENAI_* key or URL set for some other tool in
    the user's shell from redirecting these summaries.
    """
    return Summarizer(
        enabled=setting(environ, "SUMMARIZATION_ENABLED", TypeAdapter(bool), "true"),
        base_url=setting(
            environ,
            "SUMMARIZATION_BASE_URL",
            TypeAdapter(ApiBase),
            "http://100.66.66.66:8000/v1",
            shown=_masked,
        ),
        api_key=setting(environ, "SUMMARIZATION_API_KEY", TypeAdapter(str), "1234"),
        # An instruct model with no thinking phase: measured 2026-10-15 on that server at
        # 12-15s per summary including a cold load, where the reasoning models took 56s warm.
        model=setting(
            environ,
            "SUMMARIZATION_MODEL",
            TypeAdapter(Annotated[str, Field(min_length=1)]),
            "mlx-community--Qwen3-30B-A3B-Instruct-2507-8bit",
        ),
    )


def server_settings(environ: Mapping[str, str]) -> ServerSettings:
    """The configuration a server's tools run under, as the environment names it."""
    return ServerSettings(
        target=houdini_target(environ),
        summarizer=summarizer(environ),
        bypass_config_enabled=setting(
            environ, "HOUDINI_MCP_ALLOW_BYPASS", TypeAdapter(bool), "false"
        ),
    )


def rpc_token(environ: Mapping[str, str]) -> str | None:
    """The shared secret ``HOUDINI_RPC_TOKEN`` holds for the Houdini listener, or None.

    Raises:
        InvalidSetting: naming the variable and what is wrong, never the secret itself.
    """
    try:
        return handshake.token_from(environ)
    except ValueError as e:
        raise InvalidSetting(str(e)) from None


def log_level(environ: Mapping[str, str]) -> int:
    """The logging level ``LOG_LEVEL`` names, in any case."""
    return setting(environ, "LOG_LEVEL", TypeAdapter(LogLevel), "INFO")


# The host part of the URL a client calls this server by: a name, an IPv4 address, or an
# IPv6 address in brackets. [LAW:types-are-the-program] no port, which the guard ignores,
# and no * or ?, which fastmcp would read as a wildcard: a bare * admits every Host and
# reopens the server to a rebinding page.
AllowedHost = Annotated[
    str, Field(pattern=r"^(?:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*|\[[0-9A-Fa-f:.]+\])$")
]


def _entries(raw: object) -> object:
    """A comma-separated setting as its entries, with the spaces around each trimmed."""
    return [entry.strip() for entry in raw.split(",")] if isinstance(raw, str) and raw else []


AllowedHosts = Annotated[tuple[AllowedHost, ...], BeforeValidator(_entries)]


@dataclass(frozen=True)
class HttpListener:
    """Where an HTTP transport listens, and the names a request may call it by besides
    loopback and the address the request arrived at."""

    host: IPv4Address | IPv6Address
    port: int
    allowed_hosts: tuple[str, ...]


@dataclass(frozen=True)
class Stdio:
    """Serve on this process's stdin and stdout."""


@dataclass(frozen=True)
class Http:
    """Serve one of fastmcp's HTTP transports, listening as ``listener`` says."""

    transport: Literal["http", "sse", "streamable-http"]
    listener: HttpListener


# [LAW:types-are-the-program] only an HTTP transport has a listener, so stdio reads no
# listener setting and no stdio branch holds one it ignores.
Serving = Stdio | Http


def serving(environ: Mapping[str, str]) -> Serving:
    """The transport ``MCP_TRANSPORT`` names, and for HTTP, where ``MCP_HOST`` and
    ``MCP_PORT`` say it listens and the further names ``MCP_ALLOWED_HOSTS`` admits."""
    transport = setting(environ, "MCP_TRANSPORT", TypeAdapter(Transport), "http")
    if transport == "stdio":
        return Stdio()
    return Http(
        transport=transport,
        listener=HttpListener(
            host=setting(environ, "MCP_HOST", TypeAdapter(IPvAnyAddress), "127.0.0.1"),
            port=setting(environ, "MCP_PORT", TypeAdapter(Port), "3055"),
            allowed_hosts=setting(environ, "MCP_ALLOWED_HOSTS", TypeAdapter(AllowedHosts), ""),
        ),
    )


def results_dir(environ: Mapping[str, str]) -> Path:
    """The root ``HOUDINI_MCP_RESULTS_DIR`` names for results too large to return
    inline, as an absolute path: by default, under the user's own cache.

    Raises:
        InvalidSetting: it names a home directory the user does not have.
    """
    named = environ.get("HOUDINI_MCP_RESULTS_DIR") or "~/.cache/houdini-mcp/results"
    try:
        return Path(named).expanduser().absolute()
    except RuntimeError as e:
        raise InvalidSetting(f"HOUDINI_MCP_RESULTS_DIR: {e}, got {named!r}") from None
