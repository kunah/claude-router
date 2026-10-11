from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from claude_router.cli import app
from claude_router.config import load_config
from helpers import IS_WINDOWS, posix_only, same, windows_only

runner = CliRunner()


def invoke(*args, **kwargs):
    return runner.invoke(app, list(args), **kwargs)


@pytest.fixture
def on_path(fake_claude, monkeypatch):
    monkeypatch.setenv("PATH", str(fake_claude.parent))
    monkeypatch.setattr("claude_router.detect.known_locations", lambda: [])
    return fake_claude


def test_version():
    result = invoke("--version")
    assert result.exit_code == 0 and "claude-router" in result.output


def test_init_registers_existing_dirs(home, on_path):
    personal = home / ".claude-personal"
    personal.mkdir()
    (personal / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "me@gmail.com"}}))
    (home / ".claude-empty").mkdir()
    (home / ".zshrc").write_text("alias claude='claude --effort max'\n")

    result = invoke("init", "--name", "work", "--yes")
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert set(cfg.accounts) == {"work", "personal"}
    assert cfg.default == "work"
    assert same(cfg.claude_path, on_path)
    assert "logged in as me@gmail.com" in result.output
    assert "alias claude='route-claude'" in result.output
    if IS_WINDOWS:
        assert "Set-Alias -Name claude -Value route-claude" in result.output
    assert "~/.zshrc:1" in result.output
    # idempotent
    result = invoke("init", "--yes")
    assert result.exit_code == 0 and set(load_config().accounts) == {"work", "personal"}


def test_init_interactive(home, on_path):
    personal = home / ".claude-personal"
    personal.mkdir()
    (personal / "settings.json").write_text("{}")
    result = invoke("init", input="work\ny\nhome\n")
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert set(cfg.accounts) == {"work", "home"} and cfg.default == "work"


def test_add_creates_dir_and_defaults(home):
    result = invoke("add", "work2")
    assert result.exit_code == 0, result.output
    directory = home / ".claude-work2"
    assert directory.is_dir()
    cfg = load_config()
    assert cfg.default == "work2" and cfg.accounts["work2"].config_dir == str(directory)
    assert "configure-route login work2" in result.output

    assert invoke("add", "work2").exit_code == 1
    assert invoke("add", "bad name").exit_code == 1
    assert invoke("add", "rel", "--config-dir", "relative/dir").exit_code == 1

    result = invoke("add", "other", "--arg", "--model", "--arg", "opus", "--env", "FOO=bar=baz")
    assert result.exit_code == 0, result.output
    other = load_config().accounts["other"]
    assert other.args == ["--model", "opus"] and other.env == {"FOO": "bar=baz"}
    assert load_config().default == "work2"
    assert invoke("add", "bad", "--env", "NOEQUALS").exit_code == 1


@posix_only
def test_add_dir_mode(home):
    invoke("add", "work2")
    assert stat.S_IMODE(os.stat(home / ".claude-work2").st_mode) == 0o700


def test_add_existing_and_seed(home):
    (home / ".claude" / "settings.json").write_text('{"model": "opus"}')
    (home / ".claude" / "projects").mkdir()
    result = invoke("add", "work", "--config-dir", "~/.claude")
    assert result.exit_code == 0, result.output
    assert "Registered existing ~/.claude" in result.output and "work@example.com" in result.output
    assert "login work" not in result.output

    result = invoke("add", "personal", "--seed-from", "work")
    assert result.exit_code == 0, result.output
    assert (home / ".claude-personal" / "settings.json").read_text() == '{"model": "opus"}'
    assert not (home / ".claude-personal" / "projects").exists()
    assert "settings.json" in result.output

    result = invoke("add", "dup", "--config-dir", "~/.claude/")
    assert result.exit_code == 1 and "already registered" in result.output


def test_remove_refuses_stock_dir(home):
    invoke("add", "work", "--config-dir", "~/.claude")
    result = invoke("remove", "work", "-y")
    assert result.exit_code == 0, result.output
    assert "not deleting" in result.output and (home / ".claude").is_dir()
    assert load_config().accounts == {} and load_config().default is None


def test_remove_deletes_dir_and_routes(home):
    invoke("add", "work", "--config-dir", "~/.claude")
    invoke("add", "personal")
    invoke("route", "add", "~/dev", "personal")
    invoke("route", "add", "~/other", "work")
    assert (home / ".claude-personal").is_dir()
    result = invoke("remove", "personal", "-y")
    assert result.exit_code == 0, result.output
    assert not (home / ".claude-personal").exists()
    cfg = load_config()
    assert "personal" not in cfg.accounts and [r.account for r in cfg.routes] == ["work"]
    assert cfg.routes[0].index == 0 and cfg.default == "work"


def test_remove_keep_dir_outside_home_and_abort(home, tmp_path):
    outside = tmp_path / "outside"
    invoke("add", "ext", "--config-dir", str(outside))
    assert outside.is_dir()
    result = invoke("remove", "ext", "-y")
    assert result.exit_code == 0 and outside.is_dir() and "outside your home" in result.output

    invoke("add", "keep")
    assert invoke("remove", "keep", "-y", "--keep-dir").exit_code == 0
    assert (home / ".claude-keep").is_dir()

    invoke("add", "x")
    result = invoke("remove", "x", input="n\n")
    assert result.exit_code == 1 and "DELETE the directory" in result.output
    assert "x" in load_config().accounts and (home / ".claude-x").is_dir()
    result = invoke("remove", "x", input="y\n")
    assert result.exit_code == 0 and not (home / ".claude-x").exists()
    assert invoke("remove", "ghost", "-y").exit_code == 1


def test_routes(home, monkeypatch):
    invoke("add", "work", "--config-dir", "~/.claude")
    invoke("add", "personal")
    (home / "dev" / "work").mkdir(parents=True)
    monkeypatch.chdir(home / "dev" / "work")
    monkeypatch.setenv("PWD", str(home / "dev" / "work"))

    result = invoke("route", "add", "~/dev/work/*", "work")
    assert result.exit_code == 0, result.output
    assert load_config().routes[0].match == "~/dev/work"
    assert invoke("route", "add", "~/dev/work", "work").output.startswith("Route ~/dev/work -> work already exists")
    result = invoke("route", "add", "~/dev/work/", "personal")
    assert "Updated route" in result.output and load_config().routes[0].account == "personal"
    assert invoke("route", "add", "~/dev", "ghost").exit_code == 1
    assert invoke("route", "add", "re:(bad", "work").exit_code == 1
    result = invoke("route", "add", "~/missing", "work")
    assert result.exit_code == 0 and "does not exist" in result.output

    result = invoke("route", "list")
    assert "path" in result.output and "~/missing" in result.output

    # relative paths resolve against the cwd and are stored relative to ~
    result = invoke("route", "add", ".", "work")  # same directory as ~/dev/work -> updates it
    assert result.exit_code == 0 and "Updated route ~/dev/work -> work" in result.output
    assert load_config().routes[0].account == "work"
    result = invoke("route", "add", "../other/*", "work")
    assert result.exit_code == 0, result.output
    assert load_config().routes[-1].match == "~/dev/other"
    assert invoke("route", "add", str(home / "abs"), "work").exit_code == 0
    assert load_config().routes[-1].match == "~/abs"
    assert invoke("route", "remove", "../other").exit_code == 0
    assert invoke("route", "remove", "~/abs").exit_code == 0
    assert [r.match for r in load_config().routes] == ["~/dev/work", "~/missing"]

    assert invoke("route", "remove", "2").exit_code == 0
    assert invoke("route", "remove", "~/dev/work/*").exit_code == 0
    assert load_config().routes == []
    assert invoke("route", "remove", "~/nothing").exit_code == 1


@windows_only
def test_routes_with_windows_paths(home, monkeypatch):
    invoke("add", "work", "--config-dir", "~/.claude")
    (home / "dev" / "work").mkdir(parents=True)
    monkeypatch.chdir(home / "dev" / "work")
    assert invoke("route", "add", "..\\other\\*", "work").exit_code == 0
    assert invoke("route", "add", str(home / "Abs") + "\\", "work").exit_code == 0
    assert [r.match for r in load_config().routes] == ["~/dev/other", "~/Abs"]
    result = invoke("route", "add", str(home / "ABS"), "work")
    assert "already exists" in result.output


def test_which(home, on_path):
    invoke("add", "work", "--config-dir", "~/.claude")
    invoke("add", "personal")
    invoke("route", "add", "~/dev", "personal")
    invoke("args", "--", "--effort", "max")

    result = invoke("which", str(home / "dev" / "x"))
    assert result.exit_code == 0, result.output
    assert "personal (~/.claude-personal)  [route ~/dev]" in result.output
    assert f"CLAUDE_CONFIG_DIR={home / '.claude-personal'}" in result.output
    assert "--effort max" in result.output

    result = invoke("which", str(home / "elsewhere"), "--json")
    data = json.loads(result.output)
    assert data["account"] == "work" and data["reason"] == "default" and data["matches"] == []
    assert data["env_changes"] == {"CLAUDE_ROUTER_ACCOUNT": "work"}


def test_set_default_and_args(home):
    invoke("add", "work", "--config-dir", "~/.claude")
    invoke("add", "personal")
    assert invoke("set-default", "personal").exit_code == 0 and load_config().default == "personal"
    assert invoke("set-default", "ghost").exit_code == 1

    assert invoke("args", "--", "--effort", "max").exit_code == 0
    assert load_config().args == ["--effort", "max"]
    assert invoke("args", "-a", "work", "--", "--model", "opus").exit_code == 0
    assert load_config().accounts["work"].args == ["--model", "opus"]
    result = invoke("args")
    assert "global: --effort max" in result.output and "work: --model opus" in result.output
    assert invoke("args", "--clear").exit_code == 0 and load_config().args == []
    assert invoke("args", "-a", "ghost", "--", "x").exit_code == 1


def test_list(home, on_path):
    assert invoke("list").exit_code == 1  # no config yet
    invoke("add", "work", "--config-dir", "~/.claude")
    invoke("add", "personal")
    result = invoke("list", "--fast")
    assert result.exit_code == 0, result.output
    assert "work *" in result.output and "work@example.com" in result.output and "not logged in" in result.output
    assert "CREDENTIALS" in result.output
    result = invoke("list")
    assert "fake@example.com (Fake Org, pro)" in result.output


def test_run_and_login_dry_run(home, on_path, monkeypatch):
    invoke("add", "work", "--config-dir", "~/.claude")
    invoke("add", "personal")
    invoke("args", "--", "--effort", "max")
    monkeypatch.setenv("CLAUDE_ROUTER_DRY_RUN", "1")

    result = invoke("run", "personal", "-p", "hi")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert same(data["argv"][0], on_path) and data["argv"][1:] == ["--effort", "max", "-p", "hi"]
    assert data["env_changes"]["CLAUDE_CONFIG_DIR"] == str(home / ".claude-personal")

    result = invoke("login", "personal", "--email", "x@y.z", "--sso")
    assert json.loads(result.output)["argv"][1:] == ["auth", "login", "--email", "x@y.z", "--sso"]

    result = invoke("logout", "work")
    assert json.loads(result.output)["argv"][1:] == ["auth", "logout"]
    assert invoke("run", "ghost").exit_code == 1


def test_doctor(home, on_path, write_config):
    write_config(
        """
        accounts:
          work: {config_dir: ~/.claude}
        routes:
          - {match: ~/dev, account: ghost}
        """
    )
    result = invoke("doctor")
    assert result.exit_code == 1, result.output
    assert "ghost" in result.output and "1 error(s)" in result.output
    assert same(load_config().claude_path, on_path)
    assert "9.9.9 (Claude Code)" in result.output

    write_config(
        """
        default: work
        accounts:
          work: {config_dir: ~/.claude}
        """
    )
    result = invoke("doctor", "--auth")
    assert result.exit_code == 0, result.output
    assert "logged in as fake@example.com" in result.output and "0 error(s)" in result.output


def test_doctor_warns_about_env(home, on_path, write_config, monkeypatch):
    write_config("default: work\naccounts:\n  work: {config_dir: ~/.claude}\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "CLAUDE_CONFIG_DIR=/elsewhere is exported" in result.output
    assert "ANTHROPIC_API_KEY exported" in result.output


def test_doctor_reads_shell_profiles(home, on_path, write_config):
    write_config("default: work\naccounts:\n  work: {config_dir: ~/.claude}\n")
    profile = home / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    profile.parent.mkdir(parents=True)
    profile.write_bytes("# profile\r\nSet-Alias -Name claude -Value route-claude\r\n".encode("utf-16"))
    (home / ".bashrc").write_text("claude() { command claude --effort max \"$@\"; }\n")
    result = invoke("doctor")
    assert "~/Documents/PowerShell/Microsoft.PowerShell_profile.ps1:2 sends claude through route-claude" in result.output
    assert "~/.bashrc:1 has `claude() {" in result.output


def test_doctor_without_config(home, on_path):
    result = invoke("doctor")
    assert result.exit_code == 0, result.output
    assert "run `configure-route init`" in result.output
    assert not (home / ".claude-router.yaml").exists()


def test_edit_validates(home, monkeypatch):
    invoke("add", "work", "--config-dir", "~/.claude")
    noop = [sys.executable, "-c", "pass"]
    monkeypatch.setenv("EDITOR", subprocess.list2cmdline(noop) if IS_WINDOWS else shlex.join(noop))
    monkeypatch.delenv("VISUAL", raising=False)
    result = invoke("edit")
    assert result.exit_code == 0, result.output
    assert "is valid" in result.output
