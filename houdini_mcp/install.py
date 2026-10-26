"""``houdini-mcp install``: the plugin into Houdini, and this server into each MCP client here.

[LAW:effects-at-boundaries] Every place it reads or writes is reached through the
:class:`Machine` handed in, never the process's own environment or home, so a test
installs into a directory of its own.
"""

import json
import os
import shlex
import shutil
import subprocess  # nosec B404 - registering with a client is running its own CLI
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import houdini_plugin

# The Houdini the plugin is built for: its preferences directory is named for it.
HOUDINI_VERSION = "22.0"
# The name each MCP client lists this server under.
SERVER_NAME = "houdini"
# A client's command line that has not answered in this long is not going to.
_CLI_SECONDS = 60


@dataclass(frozen=True)
class Machine:
    """The machine being installed on: its environment, the user's home and its platform."""

    environ: Mapping[str, str]
    home: Path
    platform: str  # as sys.platform spells it


@dataclass(frozen=True)
class ServerEntry:
    """How a client starts this server: a command line and the environment it runs in."""

    argv: tuple[str, ...]
    env: Mapping[str, str]

    def keeping(self, env: Mapping[str, str]) -> "ServerEntry":
        """This entry, with what an earlier entry's environment set beside its own.

        A user's HOUDINI_HOST, HOUDINI_PORT or HOUDINI_RPC_TOKEN survives an install
        run again; the variables this entry sets are this entry's.
        """
        return ServerEntry(self.argv, {**env, **self.env})

    def as_json(self) -> dict[str, object]:
        """The entry as Claude Desktop's and Cursor's ``mcpServers`` hold one."""
        return {"command": self.argv[0], "args": list(self.argv[1:]), "env": dict(self.env)}


# The keys of an ``mcpServers`` entry that say how a client starts or reaches a server,
# stdio or http: an install replaces them, and keeps the rest (disabled, autoApprove, ...).
_HOW_REACHED = frozenset({"command", "args", "env", "type", "url", "headers"})


def this_server() -> ServerEntry:
    """This interpreter running this server over stdio.

    The interpreter's absolute path runs under a GUI client's bare PATH, and Houdini loads
    the plugin from this interpreter's distribution, so server and plugin are one release.
    ``-P`` keeps the folder a client starts it in off ``sys.path``: a project's own
    ``houdini_mcp`` or ``rpyc`` does not load in place of this distribution's.
    """
    return ServerEntry((sys.executable, "-P", "-m", "houdini_mcp"), {"MCP_TRANSPORT": "stdio"})


@dataclass(frozen=True)
class Installed:
    where: str


@dataclass(frozen=True)
class NotFound:
    looked: str


@dataclass(frozen=True)
class Failed:
    why: str


Outcome = Installed | NotFound | Failed


def houdini_pref_dir(machine: Machine) -> Path:
    """Houdini's user preferences directory, as Houdini finds it.

    Houdini takes HOUDINI_USER_PREF_DIR only when it holds ``__HVER__`` (one without
    is "missing __HVER__, ignored", Houdini 22.0.466), and its own default otherwise.
    """
    named = machine.environ.get("HOUDINI_USER_PREF_DIR", "")
    if "__HVER__" in named:
        return Path(named.replace("__HVER__", HOUDINI_VERSION))
    defaults = {
        "darwin": machine.home / "Library" / "Preferences" / "houdini" / HOUDINI_VERSION,
        "win32": machine.home / "Documents" / f"houdini{HOUDINI_VERSION}",
    }
    return defaults.get(machine.platform, machine.home / f"houdini{HOUDINI_VERSION}")


def plugin_tree() -> Path:
    """The plugin's packages directory this distribution ships: the source tree, when editable."""
    return Path(houdini_plugin.__file__).resolve().parent


def install_plugin(machine: Machine, _server: ServerEntry) -> Outcome:
    """Point Houdini's packages directory at the plugin this distribution ships.

    [LAW:one-source-of-truth] Houdini loads the plugin where it is installed, through
    ``package_path``, never a copy: an upgrade of this distribution upgrades the plugin
    Houdini loads with it. The pointer takes the name of the package file an earlier
    copy was installed under, so that copy no longer loads.
    """
    packages = houdini_pref_dir(machine) / "packages"
    pointer = packages / "houdini_mcp.json"
    try:
        packages.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({"package_path": plugin_tree().as_posix()}, indent=4) + "\n")
    except OSError as e:
        return Failed(str(e))
    return Installed(str(pointer))


def _run(argv: list[str], machine: Machine) -> subprocess.CompletedProcess[str] | Failed:
    try:
        # An argv list, no shell: the client's CLI as PATH finds it, and this server's command.
        return subprocess.run(  # nosec B603
            argv,
            env=dict(machine.environ),
            capture_output=True,
            text=True,
            timeout=_CLI_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return Failed(str(e))


def _said(done: subprocess.CompletedProcess[str]) -> str:
    return (done.stdout + done.stderr).strip()


def _cli(machine: Machine, name: str) -> str | None:
    return shutil.which(name, path=machine.environ.get("PATH"))


@dataclass(frozen=True)
class Entry:
    """This server's entry in a table of servers, as the client wrote it; empty when absent."""

    fields: dict[str, object]
    env: dict[str, str]


def _entry(servers: object) -> Entry | None:
    """[LAW:single-enforcer] The one reading of this server's entry in a table of servers;
    None for a shape no client writes."""
    entry = servers.get(SERVER_NAME, {}) if isinstance(servers, dict) else None
    env = entry.get("env", {}) if isinstance(entry, dict) else None
    if not (isinstance(env, dict) and all(isinstance(value, str) for value in env.values())):
        return None
    return Entry(entry, env)


@dataclass(frozen=True)
class Registered:
    """A client's config, parsed: its table of servers, and this server's entry in it."""

    config: dict[str, object]
    servers: dict[str, object]
    entry: Entry


def registered(path: Path, load: Callable[[str], object], table: str) -> Registered | Failed:
    """The client config at *path*, parsed; empty when there is none yet.

    [LAW:parse-dont-validate] A config whose shape is not the one the client writes is
    refused here, once, and left as it is: nothing past this reads or writes it.
    """
    try:
        config = load(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as e:  # JSON's and TOML's decode errors are ValueErrors
        return Failed(f"{path} could not be read, and was left as it is: {e}")
    servers = config.get(table, {}) if isinstance(config, dict) else None
    entry = _entry(servers)
    if not (isinstance(config, dict) and isinstance(servers, dict) and entry is not None):
        return Failed(
            f"{path} does not hold {table} as the client writes it, and was left as it is"
        )
    return Registered(config, servers, entry)


def _claude_config(machine: Machine) -> Path:
    named = machine.environ.get("CLAUDE_CONFIG_DIR")
    return Path(named) / ".claude.json" if named else machine.home / ".claude.json"


def _codex_config(machine: Machine) -> Path:
    named = machine.environ.get("CODEX_HOME")
    return (Path(named) if named else machine.home / ".codex") / "config.toml"


def claude_code(machine: Machine, server: ServerEntry) -> Outcome:
    """Register the server in Claude Code's user scope, replacing any earlier registration."""
    claude = _cli(machine, "claude")
    if claude is None:
        return NotFound("claude on PATH")
    path = _claude_config(machine)
    earlier = registered(path, json.loads, "mcpServers")
    if isinstance(earlier, Failed):
        return earlier
    projects = _projects(earlier.config, path)
    if isinstance(projects, Failed):
        return projects
    local, gone = projects
    # A project's local-scope entry runs over the user's (`claude mcp add`'s default scope,
    # which a hand-made registration used), so what it set is carried here too: removing
    # it afterwards, as the report asks, loses nothing.
    user = {"user scope": earlier.entry.env} if SERVER_NAME in earlier.servers else {}
    carried = _agreed(user | local, server.env)
    if isinstance(carried, Failed):
        return carried
    server = server.keeping(carried)
    # [LAW:one-source-of-truth] the claude command owns its config file, which running
    # sessions write too; `add` refuses a name that exists, so the old one goes first.
    removed = _run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], machine)
    if isinstance(removed, Failed):
        return removed
    if removed.returncode != 0 and f'No MCP server named "{SERVER_NAME}"' not in _said(removed):
        return Failed(f"claude mcp remove: {_said(removed)}")
    env = [flag for name, value in server.env.items() for flag in ("-e", f"{name}={value}")]
    add = [claude, "mcp", "add", "--scope", "user", SERVER_NAME, *env, "--", *server.argv]
    added = _run(add, machine)
    # The earlier entry is gone by now, so a failure carries the command that registers
    # what it held: nothing the user set is lost with it.
    if isinstance(added, Failed):
        return Failed(f"{added.why}; to register it, run: {shlex.join(add)}")
    if added.returncode != 0:
        return Failed(f"claude mcp add: {_said(added)}; to register it, run: {shlex.join(add)}")
    if local:
        return Failed(
            "registered in its user config, with the environment these projects' own "
            f"{SERVER_NAME!r} set, but Claude Code runs theirs there instead: "
            f"{', '.join(local)}. In each, run: claude mcp remove --scope local {SERVER_NAME}"
        )
    passed = f" (passed over the entries of projects that no longer exist: {', '.join(gone)})"
    return Installed("its user config" + (passed if gone else ""))


def _projects(
    config: dict[str, object], path: Path
) -> tuple[dict[str, dict[str, str]], list[str]] | Failed:
    """The environment each project's own local-scope entry for this server sets, by project,
    and the projects holding an entry whose folder is gone.

    Claude Code keeps a project's settings after its folder is deleted (a removed worktree).
    It cannot run a server there, and ``claude mcp remove --scope local`` cannot be run from
    there, so such an entry neither runs over the user's nor carries into it.
    """
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return Failed(f"{path} does not hold projects as Claude Code writes it; left as it is")
    found: dict[str, dict[str, str]] = {}
    gone: list[str] = []
    for project, settings in projects.items():
        servers = settings.get("mcpServers", {}) if isinstance(settings, dict) else None
        if isinstance(servers, dict) and SERVER_NAME not in servers:
            continue
        entry = _entry(servers)
        if entry is None:
            return Failed(
                f"{path} does not hold {project}'s servers as Claude Code writes them; "
                "left as it is"
            )
        if Path(project).is_dir():
            found[project] = entry.env
        else:
            gone.append(project)
    return found, gone


def _agreed(
    envs: Mapping[str, Mapping[str, str]], own: Mapping[str, str]
) -> dict[str, str] | Failed:
    """The one environment every earlier entry sets, past the variables this entry sets itself.

    An entry that leaves a variable unset runs with its default, so unset is a value of its
    own: one project pointed at another Houdini does not repoint the rest.
    """
    names = {name for env in envs.values() for name in env} - set(own)
    seen = {name: {env.get(name) for env in envs.values()} for name in names}
    differing = sorted(name for name, values in seen.items() if len(values) > 1)
    if differing:
        return Failed(
            f"the earlier entries for {SERVER_NAME!r} ({', '.join(envs)}) set "
            f"{', '.join(differing)} differently; remove the ones you do not want and run again"
        )
    return {name: value for name, (value,) in seen.items() if value is not None}


def codex(machine: Machine, server: ServerEntry) -> Outcome:
    """Register the server in Codex's config; ``codex mcp add`` replaces an earlier one."""
    cli = _cli(machine, "codex")
    if cli is None:
        return NotFound("codex on PATH")
    earlier = registered(_codex_config(machine), tomllib.loads, "mcp_servers")
    if isinstance(earlier, Failed):
        return earlier
    server = server.keeping(earlier.entry.env)
    env = [flag for name, value in server.env.items() for flag in ("--env", f"{name}={value}")]
    added = _run([cli, "mcp", "add", SERVER_NAME, *env, "--", *server.argv], machine)
    if isinstance(added, Failed):
        return added
    if added.returncode != 0:
        return Failed(f"codex mcp add: {_said(added)}")
    # `codex mcp add` writes the whole entry anew and takes no flag for the rest of it.
    dropped = sorted(set(earlier.entry.fields) - _HOW_REACHED)
    lost = f" (codex mcp add did not keep the earlier entry's {', '.join(dropped)})"
    return Installed("its config" + (lost if dropped else ""))


@dataclass(frozen=True)
class JsonConfig:
    """A client that reads its servers from ``mcpServers`` in a JSON file in its own directory."""

    app_dir: Callable[[Machine], Path | NotFound]
    file: str

    def __call__(self, machine: Machine, server: ServerEntry) -> Outcome:
        app = self.app_dir(machine)
        if isinstance(app, NotFound):
            return app
        if not app.is_dir():
            return NotFound(str(app))
        path = app / self.file
        earlier = registered(path, json.loads, "mcpServers")
        if isinstance(earlier, Failed):
            return earlier
        kept = {k: v for k, v in earlier.entry.fields.items() if k not in _HOW_REACHED}
        earlier.servers[SERVER_NAME] = kept | server.keeping(earlier.entry.env).as_json()
        earlier.config["mcpServers"] = earlier.servers
        try:
            _replace(path, json.dumps(earlier.config, indent=2) + "\n")
        except OSError as e:
            return Failed(str(e))
        return Installed(str(path))


def _replace(path: Path, text: str) -> None:
    """Put *text* in the file *path* names, whole, or leave that file as it was.

    The client reads its config while this writes it, and an interrupted write would leave
    half of it; a file renamed over another is the old one or the new one. A link (a config
    kept in a dotfiles repository) stays a link: the file it names is the one replaced.
    """
    target = path.resolve()
    staged = target.with_name(f".{target.name}.houdini-mcp")
    staged.write_text(text)
    os.replace(staged, target)


def _claude_desktop_dir(machine: Machine) -> Path | NotFound:
    if machine.platform == "win32":
        appdata = machine.environ.get("APPDATA")
        return Path(appdata) / "Claude" if appdata else NotFound("APPDATA, unset")
    if machine.platform == "darwin":
        return machine.home / "Library" / "Application Support" / "Claude"
    return machine.home / ".config" / "Claude"


@dataclass(frozen=True)
class Step:
    name: str
    install: Callable[[Machine, ServerEntry], Outcome]


# [LAW:one-type-per-behavior] every step is attempted, and says what it did; a client
# that is not on this machine is one outcome among the others.
STEPS = (
    Step("Houdini plugin", install_plugin),
    Step("Claude Code", claude_code),
    Step("Claude Desktop", JsonConfig(_claude_desktop_dir, "claude_desktop_config.json")),
    Step("Cursor", JsonConfig(lambda machine: machine.home / ".cursor", "mcp.json")),
    Step("Codex", codex),
)


def install(machine: Machine, server: ServerEntry) -> list[tuple[str, Outcome]]:
    """Run every step, in order, and say what each did."""
    return [(step.name, step.install(machine, server)) for step in STEPS]


def _described(outcome: Outcome) -> str:
    match outcome:
        case Installed(where):
            return f"installed in {where}"
        case NotFound(looked):
            return f"not found ({looked})"
        case Failed(why):
            return f"FAILED: {why}"


def report(results: list[tuple[str, Outcome]], server: ServerEntry) -> tuple[str, int]:
    """What the install did, for a person to read, and the exit status that says it."""
    env = [f"{name}={value}" for name, value in server.env.items()]
    lines = [
        *(f"{name}: {_described(outcome)}" for name, outcome in results),
        "",
        "Restart Houdini: the plugin starts its listener once the UI is up.",
        "Restart each MCP client above to load the server.",
        f"Any other MCP client runs it as: {shlex.join([*env, *server.argv])}",
    ]
    failed = any(isinstance(outcome, Failed) for _, outcome in results)
    return "\n".join(lines), int(failed)


def main(machine: Machine) -> int:
    server = this_server()
    text, status = report(install(machine, server), server)
    print(text)
    return status
