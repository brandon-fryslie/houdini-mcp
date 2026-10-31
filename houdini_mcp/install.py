"""``houdini-mcp install``: the plugin into Houdini, and this server into each MCP client
here, or into one project's ``.mcp.json``.

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

import houdini_mcp_handshake

import houdini_plugin

from .connection import BUILD

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


# The keys of a stdio entry that say how a client starts the server: an install replaces
# them, and keeps the rest (Cursor's disabled and autoApprove, Codex's cwd, ...).
_HOW_STARTED = frozenset({"command", "args", "env", "type"})


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
class Kept:
    """The client reaches this server at a URL the user gave it (a server run in Docker),
    and keeps that entry: an install does not repoint it at a server of its own."""

    url: str


@dataclass(frozen=True)
class Failed:
    why: str


Outcome = Installed | NotFound | Kept | Failed


def houdini_pref_dir(machine: Machine) -> Path:
    """Houdini's user preferences directory, as Houdini finds it.

    Houdini takes HOUDINI_USER_PREF_DIR only when it holds ``__HVER__`` (one without
    is "missing __HVER__, ignored", Houdini 22.0.466), and its own default otherwise.
    """
    named = machine.environ.get("HOUDINI_USER_PREF_DIR", "")
    if "__HVER__" in named:
        return Path(named.replace("__HVER__", HOUDINI_VERSION))
    if machine.platform == "darwin":
        return machine.home / "Library" / "Preferences" / "houdini" / HOUDINI_VERSION
    return machine.home / f"houdini{HOUDINI_VERSION}"


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
        pointer.write_text(
            json.dumps({"package_path": houdini_plugin.PACKAGES.as_posix()}, indent=4) + "\n"
        )
    except OSError as e:
        return Failed(str(e))
    return Installed(str(pointer))


def _run(
    argv: list[str], machine: Machine, cwd: Path | None = None
) -> subprocess.CompletedProcess[str] | Failed:
    try:
        # An argv list, no shell: the client's CLI as PATH finds it, and this server's command.
        return subprocess.run(  # nosec B603
            argv,
            cwd=cwd,
            env=dict(machine.environ),
            capture_output=True,
            text=True,
            timeout=_CLI_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return Failed(str(e))


def _said(done: subprocess.CompletedProcess[str]) -> str:
    return (done.stdout + done.stderr).strip()


def _cli(machine: Machine, name: str, *installed: Path) -> str | None:
    """The client's command on PATH, else where the client's own installer puts it."""
    on_path = shutil.which(name, path=machine.environ.get("PATH"))
    return on_path or next((str(cli) for cli in installed if shutil.which(cli)), None)


@dataclass(frozen=True)
class Entry:
    """This server's entry in a table of servers, as the client wrote it; empty when absent."""

    fields: dict[str, object]
    env: dict[str, str]
    url: str | None  # the URL the client reaches the server at, for a server not over stdio


def _entry(servers: object) -> Entry | None:
    """[LAW:single-enforcer] The one reading of this server's entry in a table of servers;
    None for a shape no client writes."""
    entry = servers.get(SERVER_NAME, {}) if isinstance(servers, dict) else None
    if not isinstance(entry, dict):
        return None
    env = entry.get("env", {})
    url = entry.get("url")
    if not (
        isinstance(env, dict)
        and all(isinstance(value, str) for value in env.values())
        and (url is None or isinstance(url, str))
    ):
        return None
    return Entry(entry, env, url)


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
        config = load(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as e:  # JSON's and TOML's decode errors are ValueErrors
        return Failed(f"{path} could not be read, and was left as it is: {e}")
    servers = config.get(table, {}) if isinstance(config, dict) else None
    entry = _entry(servers)
    if not (isinstance(config, dict) and isinstance(servers, dict) and entry is not None):
        return Failed(
            f"{path} does not hold {table} as the client writes it, and was left as it is"
        )
    return Registered(config, servers, entry)


def _claude_cli(machine: Machine) -> str | None:
    return _cli(
        machine,
        "claude",
        machine.home / ".claude" / "local" / "claude",
        machine.home / ".local" / "bin" / "claude",
    )


def _claude_config(machine: Machine) -> Path:
    named = machine.environ.get("CLAUDE_CONFIG_DIR")
    return Path(named) / ".claude.json" if named else machine.home / ".claude.json"


def _codex_config(machine: Machine) -> Path:
    named = machine.environ.get("CODEX_HOME")
    return (Path(named) if named else machine.home / ".codex") / "config.toml"


def claude_code(machine: Machine, server: ServerEntry) -> Outcome:
    """Register the server in Claude Code's user scope, replacing any earlier registration."""
    claude = _claude_cli(machine)
    if claude is None:
        return NotFound("claude on PATH, in ~/.claude/local or in ~/.local/bin")
    path = _claude_config(machine)
    earlier = registered(path, json.loads, "mcpServers")
    if isinstance(earlier, Failed):
        return earlier
    if earlier.entry.url is not None:
        return Kept(earlier.entry.url)
    projects = _projects(earlier.config, path)
    if isinstance(projects, Failed):
        return projects
    local = projects.local
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
    if user:
        removed = _run([claude, "mcp", "remove", "--scope", "user", SERVER_NAME], machine)
        if isinstance(removed, Failed):
            return removed
        if removed.returncode != 0:
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
    return Installed(
        "its user config"
        + _note("kept the entries of projects that reach the server at a URL:", projects.at_url)
        + _note("passed over the entries of projects that no longer exist:", projects.gone)
        + _note("claude mcp add did not keep the earlier entry's", _dropped(earlier.entry))
    )


def _dropped(entry: Entry) -> list[str]:
    """What an earlier stdio entry held beyond how it started the server."""
    return sorted(set(entry.fields) - _HOW_STARTED)


def _note(what: str, names: list[str]) -> str:
    """A note on how a step went, naming *names*; nothing when there are none."""
    return f" ({what} {', '.join(names)})" if names else ""


@dataclass(frozen=True)
class Projects:
    """The projects whose own local-scope entry for this server Claude Code keeps.

    ``local``: the environment each stdio entry sets, by project; these run over the user's.
    ``at_url``: those reaching a server at a URL the user gave them, which stay as they are.
    ``gone``: those whose folder is deleted (a removed worktree), which Claude Code keeps. It
    cannot run a server there, and ``claude mcp remove --scope local`` cannot be run from
    there, so such an entry neither runs over the user's nor carries into it.
    """

    local: dict[str, dict[str, str]]
    at_url: list[str]
    gone: list[str]


def _projects(config: dict[str, object], path: Path) -> Projects | Failed:
    """The projects in Claude Code's config with an entry of their own for this server."""
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        return Failed(f"{path} does not hold projects as Claude Code writes it; left as it is")
    found = Projects({}, [], [])
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
        if not Path(project).is_dir():
            found.gone.append(project)
        elif entry.url is not None:
            found.at_url.append(project)
        else:
            found.local[project] = entry.env
    return found


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
    if earlier.entry.url is not None:
        return Kept(earlier.entry.url)
    server = server.keeping(earlier.entry.env)
    env = [flag for name, value in server.env.items() for flag in ("--env", f"{name}={value}")]
    added = _run([cli, "mcp", "add", SERVER_NAME, *env, "--", *server.argv], machine)
    if isinstance(added, Failed):
        return added
    if added.returncode != 0:
        return Failed(f"codex mcp add: {_said(added)}")
    # `codex mcp add` writes the whole entry anew and takes no flag for the rest of it.
    return Installed(
        "its config"
        + _note("codex mcp add did not keep the earlier entry's", _dropped(earlier.entry))
    )


@dataclass(frozen=True)
class JsonConfig:
    """A client that reads its servers from ``mcpServers`` in a JSON file in its own directory."""

    app_dir: Callable[[Machine], Path]
    file: str

    def __call__(self, machine: Machine, server: ServerEntry) -> Outcome:
        app = self.app_dir(machine)
        if not app.is_dir():
            return NotFound(str(app))
        path = app / self.file
        earlier = registered(path, json.loads, "mcpServers")
        if isinstance(earlier, Failed):
            return earlier
        if earlier.entry.url is not None:
            return Kept(earlier.entry.url)
        kept = {k: v for k, v in earlier.entry.fields.items() if k not in _HOW_STARTED}
        earlier.servers[SERVER_NAME] = kept | server.keeping(earlier.entry.env).as_json()
        earlier.config["mcpServers"] = earlier.servers
        try:
            _replace(path, json.dumps(earlier.config, indent=2, ensure_ascii=False) + "\n")
        except OSError as e:
            return Failed(str(e))
        return Installed(str(path))


def _replace(path: Path, text: str) -> None:
    """Put *text* in the file *path* names, whole, or leave that file as it was.

    The client reads its config while this writes it, and an interrupted write would leave
    half of it; a file renamed over another is the old one or the new one. A link (a config
    kept in a dotfiles repository) stays a link: the file it names is the one replaced, and
    keeps its permissions (a config holding a token may be readable by its owner alone).
    """
    target = path.resolve()
    staged = target.with_name(f".{target.name}.houdini-mcp")
    # A config this creates is its owner's alone: client configs hold tokens.
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    try:
        with os.fdopen(
            os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "w", encoding="utf-8"
        ) as file:
            file.write(text)
        os.chmod(staged, mode)  # as the target is, whatever the umask took from it
        os.replace(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def _claude_desktop_dir(machine: Machine) -> Path:
    if machine.platform == "darwin":
        return machine.home / "Library" / "Application Support" / "Claude"
    return machine.home / ".config" / "Claude"


@dataclass(frozen=True)
class Step:
    name: str
    install: Callable[[Machine, ServerEntry], Outcome]


# [LAW:one-type-per-behavior] every step is attempted, and says what it did; a client
# that is not on this machine is one outcome among the others.
PLUGIN = Step("Houdini plugin", install_plugin)
STEPS = (
    PLUGIN,
    Step("Claude Code", claude_code),
    Step("Claude Desktop", JsonConfig(_claude_desktop_dir, "claude_desktop_config.json")),
    Step("Cursor", JsonConfig(lambda machine: machine.home / ".cursor", "mcp.json")),
    Step("Codex", codex),
)


# What `claude mcp get` prints when a project's .mcp.json entry will not run, and what
# the user does about it (Claude Code 2.1.299).
_NOT_RUN = (
    (
        "Scope: Local config",
        f"Claude Code runs a local-scope {SERVER_NAME!r} instead; "
        f"run: claude mcp remove --scope local {SERVER_NAME}",
    ),
    ("Status: ✘ Rejected", "it was declined; run: claude mcp reset-project-choices"),
    ("Status: ⊘ Disabled", "it is disabled here; enable it with /mcp in Claude Code"),
)
# An entry pending approval and a declined one print alike beside a user-scope entry.
_USER_SCOPE = "Scope: User config"


@dataclass(frozen=True)
class ProjectConfig:
    """A project's ``.mcp.json``, the file Claude Code reads a project's servers from."""

    project: Path

    def __call__(self, machine: Machine, server: ServerEntry) -> Outcome:
        outcome = JsonConfig(lambda _machine: self.project, ".mcp.json")(machine, server)
        claude = _claude_cli(machine)
        if claude is None or isinstance(outcome, Failed | NotFound):
            return outcome
        # [LAW:one-source-of-truth] A local-scope entry runs over .mcp.json, and Claude Code
        # keys it by git root, not folder: only claude can say which entry runs here.
        running = _run([claude, "mcp", "get", SERVER_NAME], machine, self.project)
        if isinstance(running, Failed):
            return Failed(f"{_described(outcome)}, but claude mcp get: {running.why}")
        if running.returncode != 0:
            return Failed(f"{_described(outcome)}, but claude mcp get: {_said(running)}")
        for line, remedy in _NOT_RUN:
            if line in running.stdout:
                return Failed(f"{_described(outcome)}, but {remedy}, in {self.project}")
        if isinstance(outcome, Installed) and _USER_SCOPE in running.stdout:
            return Installed(
                f"{outcome.where} (Claude Code runs your user-scope entry until you approve "
                "this one; if you declined it, run: claude mcp reset-project-choices)"
            )
        return outcome


def in_project(project: Path) -> tuple[Step, ...]:
    """The steps that serve one project and leave every client's own config alone.

    The plugin is still Houdini's, for the whole machine: Houdini reads its packages
    directory with no project in view, so the last install's build is the one it loads.
    """
    return (PLUGIN, Step("Project", ProjectConfig(project)))


def install(
    machine: Machine, server: ServerEntry, steps: tuple[Step, ...]
) -> list[tuple[str, Outcome]]:
    """Run every step, in order, and say what each did."""
    return [(step.name, step.install(machine, server)) for step in steps]


def _described(outcome: Outcome) -> str:
    match outcome:
        case Installed(where):
            return f"installed in {where}"
        case NotFound(looked):
            return f"not found ({looked})"
        case Kept(url):
            return f"kept its entry, which reaches the server at {url}"
        case Failed(why):
            return f"FAILED: {why}"


def report(results: list[tuple[str, Outcome]], server: ServerEntry) -> tuple[str, int]:
    """What the install did, for a person to read, and the exit status that says it."""
    lines = [
        *(f"{name}: {_described(outcome)}" for name, outcome in results),
        f"Version: {houdini_mcp_handshake.RELEASE}, plugin build {BUILD}",
        "",
        "Restart Houdini: the plugin starts its listener once the UI is up.",
        "Restart each MCP client to load the server.",
        f"Any other MCP client's {SERVER_NAME!r} entry: {json.dumps(server.as_json())}",
    ]
    failed = any(isinstance(outcome, Failed) for _, outcome in results)
    return "\n".join(lines), int(failed)


def main(machine: Machine, project: Path | None) -> int:
    server = this_server()
    steps = STEPS if project is None else in_project(project)
    text, status = report(install(machine, server, steps), server)
    print(text)
    return status
