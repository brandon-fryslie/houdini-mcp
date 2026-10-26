"""``houdini-mcp install``, into a home, a Houdini and MCP clients of the test's own."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from houdini_mcp.install import (
    Failed,
    Installed,
    JsonConfig,
    Kept,
    Machine,
    NotFound,
    ServerEntry,
    claude_code,
    codex,
    houdini_pref_dir,
    install,
    install_plugin,
    plugin_tree,
    report,
)

SERVER = ServerEntry(("/venv/bin/python", "-P", "-m", "houdini_mcp"), {"MCP_TRANSPORT": "stdio"})

# A client's command line: records each call's argv, one JSON list per line, and
# answers as the real one does for a name it holds (`known`) or does not.
_FAKE_CLI = """\
#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls"), "a") as calls:
    calls.write(json.dumps(sys.argv[1:]) + "\\n")
known = os.path.join(here, "known")
if sys.argv[1:3] == ["mcp", "remove"]:
    if not os.path.exists(known):
        print('No MCP server named "houdini" in user scope', file=sys.stderr)
        sys.exit(1)
    if os.path.exists(os.path.join(here, "broken")):
        print("config is locked", file=sys.stderr)
        sys.exit(1)
    os.remove(known)
elif sys.argv[1:3] == ["mcp", "add"]:
    open(known, "w").close()
"""


def _cli(bin_dir: Path, name: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = bin_dir / name
    cli.write_text(_FAKE_CLI.format(python=sys.executable))
    cli.chmod(0o755)
    return cli


def _calls(bin_dir: Path) -> list[list[str]]:
    return [json.loads(line) for line in (bin_dir / "calls").read_text().splitlines()]


def _machine(tmp_path: Path, platform: str = "darwin", **environ: str) -> Machine:
    return Machine({"PATH": str(tmp_path / "bin"), **environ}, tmp_path / "home", platform)


def _claude_json(tmp_path: Path, config: dict[str, object]) -> Machine:
    """A machine whose Claude Code config is *config*, its projects' folders made, a
    ``/gone/`` project's left unmade."""
    for project in config.get("projects", {}):  # type: ignore[union-attr]
        if not project.startswith("/gone/"):
            (tmp_path / project.lstrip("/")).mkdir(parents=True)
    projects = {
        (p if p.startswith("/gone/") else str(tmp_path / p.lstrip("/"))): s
        for p, s in config.get("projects", {}).items()  # type: ignore[union-attr]
    }
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / ".claude.json").write_text(
        json.dumps({**config, **({"projects": projects} if projects else {})})
    )
    return _machine(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "claude"))


def test_houdini_is_pointed_at_the_plugin_this_distribution_ships(tmp_path: Path) -> None:
    machine = _machine(tmp_path, HOUDINI_USER_PREF_DIR=str(tmp_path / "pref" / "houdini__HVER__"))

    outcome = install_plugin(machine, SERVER)

    pointer = tmp_path / "pref" / "houdini22.0" / "packages" / "houdini_mcp.json"
    assert outcome == Installed(str(pointer))
    assert json.loads(pointer.read_text()) == {"package_path": plugin_tree().as_posix()}
    # What Houdini finds there: the package file, and the startup script that starts the
    # listener in a graphical session.
    assert (plugin_tree() / "houdini_mcp.json").is_file()
    assert (plugin_tree() / "houdini_mcp" / "python3.13libs" / "uiready.py").is_file()


def test_a_second_install_points_at_the_plugin_again_in_place_of_an_earlier_one(
    tmp_path: Path,
) -> None:
    machine = _machine(tmp_path)
    pointer = houdini_pref_dir(machine) / "packages" / "houdini_mcp.json"
    pointer.parent.mkdir(parents=True)
    # The package file a copy of the plugin was installed under, by hand.
    pointer.write_text('{"hpath": "$HOUDINI_PACKAGE_PATH/houdini_mcp"}')

    install_plugin(machine, SERVER)

    assert json.loads(pointer.read_text()) == {"package_path": plugin_tree().as_posix()}


@pytest.mark.parametrize(
    ("platform", "under_home"),
    [
        ("darwin", "Library/Preferences/houdini/22.0"),
        ("win32", "Documents/houdini22.0"),
        ("linux", "houdini22.0"),
    ],
)
def test_houdinis_preferences_are_where_houdini_keeps_them(
    tmp_path: Path, platform: str, under_home: str
) -> None:
    machine = _machine(tmp_path, platform)
    assert houdini_pref_dir(machine) == machine.home / under_home


def test_a_preferences_directory_that_cannot_be_made_fails_the_plugin_step(tmp_path: Path) -> None:
    (tmp_path / "a-file").write_text("")
    machine = _machine(tmp_path, HOUDINI_USER_PREF_DIR=str(tmp_path / "a-file" / "houdini__HVER__"))

    assert isinstance(install_plugin(machine, SERVER), Failed)


def test_claude_code_registers_the_server_in_its_user_scope(tmp_path: Path) -> None:
    claude = _cli(tmp_path / "bin", "claude")

    assert claude_code(_machine(tmp_path), SERVER) == Installed("its user config")

    assert _calls(tmp_path / "bin")[-1] == [
        "mcp", "add", "--scope", "user", "houdini", "-e", "MCP_TRANSPORT=stdio",
        "--", "/venv/bin/python", "-P", "-m", "houdini_mcp",
    ]  # fmt: skip
    assert claude.with_name("known").exists()


_EARLIER_CLAUDE = {"mcpServers": {"houdini": {"command": "old"}}}


def test_claude_code_replaces_an_earlier_registration(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "claude")
    (tmp_path / "bin" / "known").touch()
    machine = _claude_json(tmp_path, _EARLIER_CLAUDE)

    assert claude_code(machine, SERVER) == Installed("its user config")

    assert [call[:2] for call in _calls(tmp_path / "bin")] == [["mcp", "remove"], ["mcp", "add"]]


def test_claude_code_with_no_earlier_registration_removes_nothing(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "claude")

    assert claude_code(_machine(tmp_path), SERVER) == Installed("its user config")

    assert [call[:2] for call in _calls(tmp_path / "bin")] == [["mcp", "add"]]


def test_a_registration_claude_code_will_not_remove_fails_and_adds_nothing(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "claude")
    (tmp_path / "bin" / "known").touch()
    (tmp_path / "bin" / "broken").touch()
    machine = _claude_json(tmp_path, _EARLIER_CLAUDE)

    assert claude_code(machine, SERVER) == Failed("claude mcp remove: config is locked")
    assert [call[:2] for call in _calls(tmp_path / "bin")] == [["mcp", "remove"]]


def test_claude_code_installed_by_its_local_installer_is_found_off_path(tmp_path: Path) -> None:
    """The local installer puts claude in ~/.claude/local and an alias in the shell's rc."""
    machine = _machine(tmp_path)
    _cli(machine.home / ".claude" / "local", "claude")

    assert claude_code(machine, SERVER) == Installed("its user config")
    assert _calls(machine.home / ".claude" / "local")[-1][:2] == ["mcp", "add"]


def test_codex_registers_the_server(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "codex")

    assert codex(_machine(tmp_path), SERVER) == Installed("its config")

    assert _calls(tmp_path / "bin") == [
        ["mcp", "add", "houdini", "--env", "MCP_TRANSPORT=stdio",
         "--", "/venv/bin/python", "-P", "-m", "houdini_mcp"],
    ]  # fmt: skip


def test_a_client_command_not_on_path_is_not_found(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    assert claude_code(machine, SERVER) == NotFound(
        "claude on PATH, in ~/.claude/local or in ~/.local/bin"
    )
    assert codex(machine, SERVER) == NotFound("codex on PATH")


_CURSOR = JsonConfig(lambda machine: machine.home / ".cursor", "mcp.json")


def test_a_json_client_keeps_its_other_servers_and_settings(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    config = machine.home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"other": {"url": "http://x"}}, "theme": "dark"}))

    assert _CURSOR(machine, SERVER) == Installed(str(config))

    assert json.loads(config.read_text()) == {
        "mcpServers": {
            "other": {"url": "http://x"},
            "houdini": {
                "command": "/venv/bin/python",
                "args": ["-P", "-m", "houdini_mcp"],
                "env": {"MCP_TRANSPORT": "stdio"},
            },
        },
        "theme": "dark",
    }


def test_a_json_client_with_no_config_file_yet_gets_one(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    (machine.home / ".cursor").mkdir(parents=True)

    _CURSOR(machine, SERVER)

    assert list(json.loads((machine.home / ".cursor" / "mcp.json").read_text())["mcpServers"]) == [
        "houdini"
    ]


def test_a_json_client_that_is_not_installed_is_not_found(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    assert _CURSOR(machine, SERVER) == NotFound(str(machine.home / ".cursor"))


@pytest.mark.parametrize(
    "text",
    ["{not json", "[]", '{"mcpServers": []}', '{"mcpServers": {"houdini": {"env": {"A": 1}}}}'],
)
def test_a_json_config_it_cannot_read_fails_and_is_left_as_it_is(tmp_path: Path, text: str) -> None:
    machine = _machine(tmp_path)
    config = machine.home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(text)

    assert isinstance(_CURSOR(machine, SERVER), Failed)
    assert config.read_text() == text


def test_the_report_says_what_each_step_did_and_fails_when_one_failed(tmp_path: Path) -> None:
    text, status = report(
        [("A", Installed("here")), ("B", NotFound("b on PATH")), ("C", Failed("broke"))], SERVER
    )
    assert text.splitlines()[:3] == [
        "A: installed in here",
        "B: not found (b on PATH)",
        "C: FAILED: broke",
    ]
    assert (
        "Any other MCP client's 'houdini' entry: "
        '{"command": "/venv/bin/python", "args": ["-P", "-m", "houdini_mcp"], '
        '"env": {"MCP_TRANSPORT": "stdio"}}'
    ) in text
    assert status == 1
    assert report([("A", Installed("here")), ("B", NotFound("x"))], SERVER)[1] == 0


def test_every_step_runs_on_a_machine_with_no_clients(tmp_path: Path) -> None:
    results = install(_machine(tmp_path), SERVER)
    assert [name for name, _ in results] == [
        "Houdini plugin",
        "Claude Code",
        "Claude Desktop",
        "Cursor",
        "Codex",
    ]
    assert isinstance(results[0][1], Installed)
    assert all(isinstance(outcome, NotFound) for _, outcome in results[1:])


def test_the_command_installs_this_interpreters_server(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "claude")
    home = tmp_path / "home"
    done = subprocess.run(
        [sys.executable, "-m", "houdini_mcp", "install"],
        env={
            "HOME": str(home),
            "PATH": str(tmp_path / "bin"),
            "HOUDINI_USER_PREF_DIR": str(tmp_path / "pref" / "houdini__HVER__"),
        },
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Claude Code: installed in its user config" in done.stdout
    assert _calls(tmp_path / "bin")[-1][-4:] == [sys.executable, "-P", "-m", "houdini_mcp"]
    assert (tmp_path / "pref" / "houdini22.0" / "packages" / "houdini_mcp.json").is_file()


def test_a_preferences_directory_named_without_hver_is_ignored_as_houdini_ignores_it(
    tmp_path: Path,
) -> None:
    machine = _machine(tmp_path, HOUDINI_USER_PREF_DIR=str(tmp_path / "pref"))
    assert houdini_pref_dir(machine) == machine.home / "Library/Preferences/houdini/22.0"


# An entry a user wrote by hand, pointed at another Houdini, and the same entry installed again.
_EARLIER = {"HOUDINI_HOST": "studio-box", "MCP_TRANSPORT": "http"}
_KEPT = {"HOUDINI_HOST": "studio-box", "MCP_TRANSPORT": "stdio"}


def test_a_json_client_keeps_what_its_earlier_entry_set_in_the_environment(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    config = machine.home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"houdini": {"command": "old", "env": _EARLIER}}}))

    _CURSOR(machine, SERVER)

    assert json.loads(config.read_text())["mcpServers"]["houdini"]["env"] == _KEPT


def test_claude_code_keeps_what_its_earlier_entry_set_in_the_environment(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "claude")
    (tmp_path / "bin" / "known").touch()
    machine = _machine(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "claude"))
    (tmp_path / "claude").mkdir()
    (tmp_path / "claude" / ".claude.json").write_text(
        json.dumps({"mcpServers": {"houdini": {"command": "old", "env": _EARLIER}}})
    )

    claude_code(machine, SERVER)

    added = _calls(tmp_path / "bin")[-1]
    assert added[added.index("houdini") + 1 : added.index("--")] == [
        "-e", "HOUDINI_HOST=studio-box", "-e", "MCP_TRANSPORT=stdio",
    ]  # fmt: skip


def test_a_project_that_registers_its_own_server_in_claude_code_fails_the_step(
    tmp_path: Path,
) -> None:
    """Claude Code runs a project's local-scope entry over the user's (claude mcp add's
    default scope, which an earlier hand-made registration used)."""
    _cli(tmp_path / "bin", "claude")
    machine = _claude_json(
        tmp_path,
        {
            "projects": {
                "/work/shot": {
                    "mcpServers": {"houdini": {"command": "uv", "env": {"HOUDINI_RPC_TOKEN": "s3"}}}
                },
                "/work/other": {"mcpServers": {}},
                "/work/bare": {},
            }
        },
    )

    outcome = claude_code(machine, SERVER)

    assert isinstance(outcome, Failed)
    assert "/work/shot" in outcome.why and "/work/other" not in outcome.why
    assert "claude mcp remove --scope local houdini" in outcome.why
    # What the project's entry set is in the user's, so the remove the report asks for
    # loses nothing.
    added = _calls(tmp_path / "bin")[-1]
    assert added[:2] == ["mcp", "add"]
    assert added[added.index("houdini") + 1 : added.index("--")] == [
        "-e", "HOUDINI_RPC_TOKEN=s3", "-e", "MCP_TRANSPORT=stdio",
    ]  # fmt: skip


@pytest.mark.parametrize(
    "config",
    [
        # The user's entry runs on the local Houdini; one project's points at another.
        {
            "mcpServers": {"houdini": {"env": {"MCP_TRANSPORT": "stdio"}}},
            "projects": {
                "/work/shot": {"mcpServers": {"houdini": {"env": {"HOUDINI_HOST": "render"}}}}
            },
        },
        # Two projects, one pointed at another Houdini and one at the local one.
        {
            "projects": {
                "/work/shot": {"mcpServers": {"houdini": {"env": {"HOUDINI_HOST": "render"}}}},
                "/work/look": {"mcpServers": {"houdini": {"env": {}}}},
            },
        },
    ],
)
def test_an_entry_that_leaves_a_variable_unset_disagrees_with_one_that_sets_it(
    tmp_path: Path, config: dict[str, object]
) -> None:
    """Unset is the default, the local Houdini: carrying one project's host into the user's
    entry would repoint every other project."""
    _cli(tmp_path / "bin", "claude")
    machine = _claude_json(tmp_path, config)

    outcome = claude_code(machine, SERVER)

    assert isinstance(outcome, Failed) and "HOUDINI_HOST" in outcome.why
    assert not (tmp_path / "bin" / "calls").exists()


def test_earlier_claude_code_entries_that_disagree_register_nothing(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "claude")
    entry = {"command": "uv", "env": {"HOUDINI_HOST": "a", "MCP_TRANSPORT": "stdio"}}
    machine = _claude_json(
        tmp_path,
        {
            "mcpServers": {"houdini": entry},
            "projects": {
                "/work/shot": {
                    "mcpServers": {
                        "houdini": {"env": {"HOUDINI_HOST": "b", "MCP_TRANSPORT": "http"}}
                    }
                }
            },
        },
    )

    outcome = claude_code(machine, SERVER)

    assert isinstance(outcome, Failed)
    assert "HOUDINI_HOST" in outcome.why and "MCP_TRANSPORT" not in outcome.why
    assert not (tmp_path / "bin" / "calls").exists()


def test_codex_keeps_what_its_earlier_entry_set_in_the_environment(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "codex")
    machine = _machine(tmp_path, CODEX_HOME=str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text(
        '[mcp_servers.houdini]\ncommand = "old"\n\n'
        '[mcp_servers.houdini.env]\nHOUDINI_HOST = "studio-box"\nMCP_TRANSPORT = "http"\n'
    )

    codex(machine, SERVER)

    added = _calls(tmp_path / "bin")[-1]
    assert added[added.index("houdini") + 1 : added.index("--")] == [
        "--env", "HOUDINI_HOST=studio-box", "--env", "MCP_TRANSPORT=stdio",
    ]  # fmt: skip


def test_a_client_config_it_cannot_read_registers_nothing(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "codex")
    machine = _machine(tmp_path, CODEX_HOME=str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text("[mcp_servers.houdini\n")

    assert isinstance(codex(machine, SERVER), Failed)
    assert not (tmp_path / "bin" / "calls").exists()


def test_a_project_whose_folder_is_gone_neither_fails_the_step_nor_carries(
    tmp_path: Path,
) -> None:
    """Claude Code keeps a deleted worktree's settings; no server runs there, and its
    entry cannot be removed from there."""
    _cli(tmp_path / "bin", "claude")
    gone = {"mcpServers": {"houdini": {"env": {"HOUDINI_HOST": "render"}}}}
    machine = _claude_json(tmp_path, {"projects": {"/gone/worktree": gone}})

    outcome = claude_code(machine, SERVER)

    assert outcome == Installed(
        "its user config (passed over the entries of projects that no longer exist: /gone/worktree)"
    )
    added = _calls(tmp_path / "bin")[-1]
    assert added[added.index("houdini") + 1 : added.index("--")] == ["-e", "MCP_TRANSPORT=stdio"]


def test_a_json_client_keeps_the_earlier_entrys_own_settings(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    config = machine.home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    earlier = {"command": "uv", "type": "stdio", "disabled": False, "autoApprove": ["get_scene"]}
    config.write_text(json.dumps({"mcpServers": {"houdini": earlier}}))

    _CURSOR(machine, SERVER)

    assert json.loads(config.read_text())["mcpServers"]["houdini"] == {
        "disabled": False,
        "autoApprove": ["get_scene"],
        "command": "/venv/bin/python",
        "args": ["-P", "-m", "houdini_mcp"],
        "env": {"MCP_TRANSPORT": "stdio"},
    }


def test_a_json_config_kept_behind_a_link_stays_a_link(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    (machine.home / ".cursor").mkdir(parents=True)
    kept = tmp_path / "dotfiles" / "cursor-mcp.json"
    kept.parent.mkdir()
    kept.write_text('{"mcpServers": {}}')
    (machine.home / ".cursor" / "mcp.json").symlink_to(kept)

    _CURSOR(machine, SERVER)

    assert (machine.home / ".cursor" / "mcp.json").is_symlink()
    assert list(json.loads(kept.read_text())["mcpServers"]) == ["houdini"]
    assert sorted(path.name for path in kept.parent.iterdir()) == ["cursor-mcp.json"]


def test_codex_says_what_of_its_earlier_entry_it_did_not_keep(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "codex")
    machine = _machine(tmp_path, CODEX_HOME=str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text(
        '[mcp_servers.houdini]\ncommand = "old"\nstartup_timeout_sec = 30\n'
    )

    assert codex(machine, SERVER) == Installed(
        "its config (codex mcp add did not keep the earlier entry's startup_timeout_sec)"
    )


# The README's Docker client entry: the client reaches a server run in a container.
_DOCKER = "http://localhost:3055/mcp"


def test_a_json_client_pointed_at_a_server_by_url_keeps_its_entry(tmp_path: Path) -> None:
    machine = _machine(tmp_path)
    config = machine.home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    text = json.dumps({"mcpServers": {"houdini": {"url": _DOCKER}}})
    config.write_text(text)

    assert _CURSOR(machine, SERVER) == Kept(_DOCKER)
    assert config.read_text() == text


def test_claude_code_pointed_at_a_server_by_url_keeps_its_entry(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "claude")
    machine = _claude_json(tmp_path, {"mcpServers": {"houdini": {"type": "http", "url": _DOCKER}}})

    assert claude_code(machine, SERVER) == Kept(_DOCKER)
    assert not (tmp_path / "bin" / "calls").exists()


def test_codex_pointed_at_a_server_by_url_keeps_its_entry(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "codex")
    machine = _machine(tmp_path, CODEX_HOME=str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text(f'[mcp_servers.houdini]\nurl = "{_DOCKER}"\n')

    assert codex(machine, SERVER) == Kept(_DOCKER)
    assert not (tmp_path / "bin" / "calls").exists()


def test_a_kept_entry_is_reported_and_is_not_a_failure() -> None:
    text, status = report([("Cursor", Kept(_DOCKER))], SERVER)
    assert text.splitlines()[0] == f"Cursor: kept its entry, which reaches the server at {_DOCKER}"
    assert status == 0


def test_a_json_config_keeps_its_permissions_and_its_text(tmp_path: Path) -> None:
    """A config holding a token may be readable by its owner alone."""
    machine = _machine(tmp_path)
    config = machine.home / ".cursor" / "mcp.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"other": {"description": "José's"}}}))
    config.chmod(0o600)

    _CURSOR(machine, SERVER)

    assert config.stat().st_mode & 0o777 == 0o600
    assert "José's" in config.read_text()
    assert sorted(path.name for path in config.parent.iterdir()) == ["mcp.json"]


def test_houdinis_preferences_on_windows_are_not_moved_by_a_shells_home(tmp_path: Path) -> None:
    """Git Bash sets HOME in its own shell; Houdini started from the Start menu has none."""
    machine = _machine(tmp_path, "win32", HOME=str(tmp_path / "h"))
    assert houdini_pref_dir(machine) == machine.home / "Documents" / "houdini22.0"


def test_claude_desktop_on_windows_with_no_appdata_is_not_found(tmp_path: Path) -> None:
    results = dict(install(_machine(tmp_path, "win32"), SERVER))
    assert results["Claude Desktop"] == NotFound("APPDATA, unset")


def test_the_entry_another_client_is_shown_is_one_its_config_takes() -> None:
    server = ServerEntry((r"C:\Users\Jane Doe\python.exe", "-P", "-m", "houdini_mcp"), {})
    text, _ = report([], server)
    shown = text.splitlines()[-1].split(" entry: ", 1)[1]
    assert json.loads(shown) == server.as_json()


def test_claude_code_keeps_a_projects_entry_that_reaches_the_server_at_a_url(
    tmp_path: Path,
) -> None:
    _cli(tmp_path / "bin", "claude")
    docker = {"mcpServers": {"houdini": {"type": "http", "url": _DOCKER}}}
    machine = _claude_json(
        tmp_path,
        {
            "mcpServers": {"houdini": {"env": {"HOUDINI_PORT": "19000"}}},
            "projects": {"/work/docker": docker, "/work/shot": {"mcpServers": {}}},
        },
    )
    (tmp_path / "bin" / "known").touch()

    outcome = claude_code(machine, SERVER)

    project = str(tmp_path / "work" / "docker")
    assert outcome == Installed(
        f"its user config (kept the entries of projects that reach the server at a URL: {project})"
    )
    # The project's URL entry is neither compared with the user's nor carried into it.
    added = _calls(tmp_path / "bin")[-1]
    assert added[added.index("houdini") + 1 : added.index("--")] == [
        "-e", "HOUDINI_PORT=19000", "-e", "MCP_TRANSPORT=stdio",
    ]  # fmt: skip


def test_claude_code_says_what_of_its_earlier_entry_it_did_not_keep(tmp_path: Path) -> None:
    _cli(tmp_path / "bin", "claude")
    (tmp_path / "bin" / "known").touch()
    machine = _claude_json(
        tmp_path, {"mcpServers": {"houdini": {"type": "stdio", "command": "uv", "timeout": 30}}}
    )

    assert claude_code(machine, SERVER) == Installed(
        "its user config (claude mcp add did not keep the earlier entry's timeout)"
    )
