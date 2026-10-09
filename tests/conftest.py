from __future__ import annotations

import json
import textwrap

import pytest

STATE_WORK = {
    "oauthAccount": {"emailAddress": "work@example.com", "organizationName": "Work Inc", "displayName": "Worker"}
}

FAKE_CLAUDE = """\
#!/bin/sh
if [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi
if [ "$1" = "auth" ] && [ "$2" = "status" ]; then
  printf '{"loggedIn": true, "email": "fake@example.com", "orgName": "Fake Org", "subscriptionType": "pro", "configDirectory": "%s"}\\n' "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  exit 0
fi
printf 'CLAUDE_CONFIG_DIR=%s\\n' "${CLAUDE_CONFIG_DIR-<unset>}"
printf 'CLAUDE_ROUTER_ACCOUNT=%s\\n' "${CLAUDE_ROUTER_ACCOUNT-<unset>}"
for a in "$@"; do printf 'ARG=%s\\n' "$a"; done
exit 3
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME with a stock ~/.claude that is 'logged in' as work@example.com."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for var in (
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_ROUTER_ACCOUNT",
        "CLAUDE_ROUTER_DRY_RUN",
        "CLAUDE_ROUTER_QUIET",
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
    """A shell script standing in for the claude binary (prints env + args, exits 3)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(0o755)
    return script


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
