from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import sysconfig

import pytest

from claude_router import route
from claude_router.config import load_config
from claude_router.resolve import Decision, candidate_cwds, resolve_account
from helpers import same

CONFIG = """
claude_path: '{claude}'
default: work
args: ["--effort", "max"]
accounts:
  work: {{config_dir: ~/.claude}}
  personal: {{config_dir: ~/.claude-personal, args: ["--model", "opus"]}}
routes:
  - {{match: ~/dev/oss, account: personal}}
"""

EXPECTED_ARGS = "ARG=--effort\nARG=max\nARG=--model\nARG=opus\nARG=--help\nARG=two words\n"


def setup(home, fake_claude, write_config, monkeypatch):
    personal = home / ".claude-personal"
    personal.mkdir()
    proj = home / "dev" / "oss" / "x"
    proj.mkdir(parents=True)
    write_config(CONFIG.format(claude=fake_claude))
    monkeypatch.setenv("CLAUDE_ROUTER_DRY_RUN", "1")
    return personal, proj


def chdir(monkeypatch, path):
    monkeypatch.chdir(path)
    monkeypatch.setenv("PWD", str(path))


def test_dry_run_routes_by_cwd(home, fake_claude, write_config, monkeypatch, capsys):
    personal, proj = setup(home, fake_claude, write_config, monkeypatch)
    chdir(monkeypatch, proj)
    assert route.main(["-p", "hi"]) == 0
    out = capsys.readouterr()
    data = json.loads(out.out)
    assert data["account"] == "personal" and data["reason"] == "route" and data["route"] == "~/dev/oss"
    assert data["argv"] == [str(fake_claude), "--effort", "max", "--model", "opus", "-p", "hi"]
    assert data["env_changes"]["CLAUDE_CONFIG_DIR"] == str(personal)
    assert data["env_changes"]["CLAUDE_ROUTER_ACCOUNT"] == "personal"
    assert out.err == ""


def test_dry_run_default_unsets_config_dir(home, fake_claude, write_config, monkeypatch, capsys):
    setup(home, fake_claude, write_config, monkeypatch)
    chdir(monkeypatch, home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
    assert route.main([]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["account"] == "work" and data["reason"] == "default"
    assert data["env_changes"]["CLAUDE_CONFIG_DIR"] is None
    assert data["argv"] == [str(fake_claude), "--effort", "max"]


def test_override_env(home, fake_claude, write_config, monkeypatch, capsys):
    personal, proj = setup(home, fake_claude, write_config, monkeypatch)
    chdir(monkeypatch, proj)
    monkeypatch.setenv("CLAUDE_ROUTER_ACCOUNT", "work")
    assert route.main([]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "override"
    monkeypatch.setenv("CLAUDE_ROUTER_ACCOUNT", "ghost")
    assert route.main([]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "ghost" in out.err


def test_missing_config_passes_through(home, fake_claude, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(fake_claude.parent))
    monkeypatch.setenv("CLAUDE_ROUTER_DRY_RUN", "1")
    chdir(monkeypatch, home)
    assert route.main(["--version"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["account"] is None and data["env_changes"] == {}
    assert same(data["argv"][0], fake_claude) and data["argv"][1:] == ["--version"]


def test_broken_config_does_not_launch(home, fake_claude, write_config, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_ROUTER_DRY_RUN", "1")
    chdir(monkeypatch, home)
    write_config("accounts: {work: {config_dir: ~/.claude}}\nroutes:\n  - {match: ~/dev, account: ghost}\n")
    assert route.main([]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "ghost" in out.err and "nothing was launched" in out.err
    write_config("accounts: [\n")
    assert route.main([]) == 2
    assert "invalid YAML" in capsys.readouterr().err


def test_no_binary(home, write_config, monkeypatch, capsys, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr("claude_router.detect.known_locations", lambda: [])
    monkeypatch.setenv("CLAUDE_ROUTER_DRY_RUN", "1")
    chdir(monkeypatch, home)
    write_config("claude_path: /nonexistent/claude\naccounts: {work: {config_dir: ~/.claude}}\n")
    assert route.main([]) == 127
    assert "could not find" in capsys.readouterr().err


def test_loop_guard(home, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_ROUTER_DEPTH", str(route.MAX_DEPTH))
    assert route.main([]) == 2
    assert "points back at route-claude" in capsys.readouterr().err


def test_announce(home, fake_claude, write_config, monkeypatch, capsys):
    setup(home, fake_claude, write_config, monkeypatch)
    cfg = load_config()
    decision = resolve_account(cfg, candidate_cwds(str(home / "dev" / "oss")))
    route._announce(cfg, decision)
    assert capsys.readouterr().err == ""  # auto + no TTY under pytest
    cfg.announce = "always"
    route._announce(cfg, decision)
    assert "personal (~/.claude-personal) via route ~/dev/oss" in capsys.readouterr().err
    monkeypatch.setenv("CLAUDE_ROUTER_QUIET", "1")
    route._announce(cfg, decision)
    assert capsys.readouterr().err == ""
    monkeypatch.delenv("CLAUDE_ROUTER_QUIET")
    route._announce(cfg, Decision(account=None, reason="none"))
    assert "no route matched" in capsys.readouterr().err


def test_exit_status():
    assert route.exit_status(3) == 3
    assert route.exit_status(0xC000013A) == 0xC000013A - (1 << 32)  # STATUS_CONTROL_C_EXIT


def test_spawn_and_wait(home, fake_claude, capfd):
    """The Windows launch path, exercised on every platform."""
    before = signal.getsignal(signal.SIGINT)
    assert route.spawn_and_wait([str(fake_claude), "x y"], dict(os.environ)) == 3
    assert "ARG=x y" in capfd.readouterr().out
    assert signal.getsignal(signal.SIGINT) is before  # handlers restored
    assert route.spawn_and_wait([os.path.join(str(home), "missing-claude")], dict(os.environ)) == 127


def _run_router(command, home, cwd):
    env = dict(os.environ)
    env["PWD"] = str(cwd)
    return subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=60)


def _prepare_exec(home, fake_claude, write_config):
    personal = home / ".claude-personal"
    personal.mkdir()
    proj = home / "dev" / "oss" / "p"
    proj.mkdir(parents=True)
    write_config(CONFIG.format(claude=fake_claude))
    return personal, proj


def test_exec_forwards_args_and_exit_code(home, fake_claude, write_config):
    personal, proj = _prepare_exec(home, fake_claude, write_config)
    result = _run_router([sys.executable, "-m", "claude_router.route", "--help", "two words"], home, proj)
    assert result.returncode == 3, result.stderr
    assert EXPECTED_ARGS in result.stdout
    assert f"CLAUDE_CONFIG_DIR={personal}\n" in result.stdout
    assert "CLAUDE_ROUTER_ACCOUNT=personal\n" in result.stdout
    assert "CLAUDE_ROUTER_DEPTH=1\n" in result.stdout
    assert result.stderr == ""  # announce is auto and stdout is a pipe


def test_console_script_end_to_end(home, fake_claude, write_config):
    """The installed `route-claude` launcher (route-claude.exe on Windows) behaves the same."""
    script = shutil.which("route-claude", path=sysconfig.get_path("scripts"))
    if script is None:
        pytest.skip("route-claude is not installed in this environment")
    personal, proj = _prepare_exec(home, fake_claude, write_config)
    result = _run_router([script, "--help", "two words"], home, proj)
    assert result.returncode == 3, result.stderr
    assert EXPECTED_ARGS in result.stdout
    assert f"CLAUDE_CONFIG_DIR={personal}\n" in result.stdout
