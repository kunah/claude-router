from __future__ import annotations

import json
import os
import stat

from claude_router import accounts
from claude_router.config import Account, Config


def test_state_file_and_identity(home):
    stock = str(home / ".claude")
    other = str(home / ".claude-personal")
    assert accounts.state_file(stock) == str(home / ".claude.json")
    assert accounts.state_file(other) == f"{other}/.claude.json"

    identity = accounts.login_identity(stock)
    assert identity is not None and identity.email == "work@example.com" and identity.organization == "Work Inc"
    assert accounts.login_identity(other) is None
    os.makedirs(other)
    (home / ".claude-personal" / ".claude.json").write_text("{not json")
    assert accounts.login_identity(other) is None
    (home / ".claude-personal" / ".claude.json").write_text(json.dumps({"oauthAccount": None}))
    assert accounts.login_identity(other) is None


def test_keychain_service(home):
    assert accounts.keychain_service(None) == "Claude Code-credentials"
    assert accounts.keychain_service(str(home / ".claude")) == "Claude Code-credentials"
    # known vector from a real machine
    assert accounts.keychain_service("/Users/hugo/.claude-personal") == "Claude Code-credentials-003b06eb"
    assert accounts.keychain_service("/Users/hugo/.claude-personal/") != "Claude Code-credentials-003b06eb"


def test_seed_dir(tmp_path):
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    dst.mkdir()
    for name in ("settings.json", "keybindings.json", "CLAUDE.md", ".claude.json", "history.jsonl"):
        (src / name).parent.mkdir(parents=True, exist_ok=True)
        (src / name).write_text("x")
    (src / "skills" / "a").mkdir(parents=True)
    (src / "skills" / "a" / "SKILL.md").write_text("skill")
    (src / "projects" / "p").mkdir(parents=True)
    (src / "sessions").mkdir()
    copied = accounts.seed_dir(str(src), str(dst))
    assert copied == ["settings.json", "keybindings.json", "CLAUDE.md", "skills/"]
    assert (dst / "skills" / "a" / "SKILL.md").read_text() == "skill"
    assert not (dst / ".claude.json").exists()
    assert not (dst / "projects").exists()
    assert not (dst / "history.jsonl").exists()


def test_discover_existing_dirs(home):
    personal = home / ".claude-personal"
    personal.mkdir()
    (personal / ".claude.json").write_text("{}")
    settings_only = home / ".claude-work2"
    settings_only.mkdir()
    (settings_only / "settings.json").write_text("{}")
    (home / ".claude-empty").mkdir()
    (home / ".claude-router.yaml").write_text("")
    assert accounts.discover_existing_dirs() == [str(home / ".claude"), str(personal), str(settings_only)]


def test_deletion_refusal(home, tmp_path):
    assert accounts.deletion_refusal(str(home / ".claude"))
    assert accounts.deletion_refusal(str(home))
    assert accounts.deletion_refusal("/")
    assert accounts.deletion_refusal(str(tmp_path / "elsewhere"))
    assert accounts.deletion_refusal(str(home / ".claude-personal")) is None


def test_create_config_dir(home):
    path = str(home / ".claude-new")
    assert accounts.create_config_dir(path) is True
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o700
    assert accounts.create_config_dir(path) is False


def test_auth_status_uses_account_env(home, fake_claude):
    personal = home / ".claude-personal"
    personal.mkdir()
    cfg = Config(path=home / "c.yaml", exists=True)
    cfg.accounts["personal"] = Account(name="personal", config_dir=str(personal))
    cfg.accounts["work"] = Account(name="work", config_dir=str(home / ".claude"))
    status = accounts.auth_status(str(fake_claude), cfg, cfg.accounts["personal"])
    assert status and status["loggedIn"] and status["configDirectory"] == str(personal)
    status = accounts.auth_status(str(fake_claude), cfg, cfg.accounts["work"])
    assert status and status["configDirectory"] == str(home / ".claude")
    assert accounts.auth_status("/nonexistent/claude", cfg, cfg.accounts["work"]) is None
