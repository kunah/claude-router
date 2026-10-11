from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

STATE_WORK = {
    "oauthAccount": {"emailAddress": "work@example.com", "organizationName": "Work Inc", "displayName": "Worker"}
}

# Stand-in for the claude binary: answers --version and `auth status`, otherwise prints what it
# received and exits 3. It runs through a tiny sh launcher on POSIX and a claude.cmd on Windows.
FAKE_CLAUDE_PY = """\
import json
import os
import sys

args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    print(json.dumps({"loggedIn": True, "email": "fake@example.com", "orgName": "Fake Org",
                      "subscriptionType": "pro", "configDirectory": config_dir}))
    sys.exit(0)
for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_ROUTER_ACCOUNT", "CLAUDE_ROUTER_DEPTH"):
    print(name + "=" + os.environ.get(name, "<unset>"))
for arg in args:
    print("ARG=" + arg)
sys.exit(3)
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated home directory with a stock ~/.claude that is 'logged in' as work@example.com."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))  # what os.path.expanduser reads on Windows
    for var in (
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_ROUTER_ACCOUNT",
        "CLAUDE_ROUTER_DRY_RUN",
        "CLAUDE_ROUTER_QUIET",
        "CLAUDE_ROUTER_DEPTH",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_ROUTER_CONFIG", str(h / ".claude-router.yaml"))
    (h / ".claude").mkdir()
    (h / ".claude.json").write_text(json.dumps(STATE_WORK))
    return h


@pytest.fixture
def fake_claude(tmp_path):
    impl = tmp_path / "fake-impl" / "fake_claude.py"
    impl.parent.mkdir(exist_ok=True)
    impl.write_text(FAKE_CLAUDE_PY)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    if os.name == "nt":
        launcher = bin_dir / "claude.cmd"
        launcher.write_text(f'@"{sys.executable}" "{impl}" %*\r\n@exit /b %errorlevel%\r\n', newline="")
    else:
        launcher = bin_dir / "claude"
        launcher.write_text(f"#!/bin/sh\nexec '{sys.executable}' '{impl}' \"$@\"\n")
        launcher.chmod(0o755)
    return launcher


@pytest.fixture
def write_config(home):
    def _write(text: str):
        path = home / ".claude-router.yaml"
        path.write_text(textwrap.dedent(text))
        return path

    return _write


@pytest.fixture(autouse=True)
def _no_system_calls(monkeypatch):
    """Never touch the real Keychain or npm during tests."""
    monkeypatch.setattr("claude_router.accounts.keychain_item_exists", lambda service: None)
    monkeypatch.setattr("claude_router.accounts.keychain_delete", lambda service: False)
    monkeypatch.setattr("claude_router.detect._npm_prefix", lambda: None)
