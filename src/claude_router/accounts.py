"""Helpers for individual Claude config directories: identity, Keychain, seeding, discovery."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .config import Account, Config, home_dir, is_stock_dir, stock_config_dir
from .resolve import build_env

KEYCHAIN_SERVICE = "Claude Code-credentials"
STATE_FILE = ".claude.json"
SEED_FILES = ("settings.json", "keybindings.json", "CLAUDE.md")
SEED_DIRS = ("skills", "agents", "commands", "output-styles")
DISCOVERY_MARKERS = (".claude.json", "settings.json")


@dataclass
class Identity:
    email: str | None
    organization: str | None
    display_name: str | None = None


def state_file(config_dir: str) -> str:
    """Claude's global state file: ``~/.claude.json`` for the stock dir, ``<dir>/.claude.json`` otherwise."""
    if is_stock_dir(config_dir):
        return os.path.join(home_dir(), STATE_FILE)
    return os.path.join(config_dir, STATE_FILE)


def login_identity(config_dir: str) -> Identity | None:
    """Best-effort identity from the state file (no subprocess, never creates files)."""
    try:
        with open(state_file(config_dir), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    account = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(account, dict):
        return None
    return Identity(
        email=account.get("emailAddress"),
        organization=account.get("organizationName"),
        display_name=account.get("displayName"),
    )


def keychain_service(config_dir: str | None) -> str:
    """Mirror Claude Code's macOS Keychain naming.

    The suffix is ``sha256(CLAUDE_CONFIG_DIR)[:8]`` and is only applied when the variable is
    set, which the router does for every account except the stock ``~/.claude``.
    """
    if config_dir is None or is_stock_dir(config_dir):
        return KEYCHAIN_SERVICE
    digest = hashlib.sha256(unicodedata.normalize("NFC", config_dir).encode("utf-8")).hexdigest()[:8]
    return f"{KEYCHAIN_SERVICE}-{digest}"


def keychain_item_exists(service: str) -> bool | None:
    """True/False on macOS (metadata lookup only, no secret is read); None elsewhere."""
    if sys.platform != "darwin" or shutil.which("security") is None:
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def keychain_delete(service: str) -> bool:
    if sys.platform != "darwin" or shutil.which("security") is None:
        return False
    try:
        result = subprocess.run(
            ["security", "delete-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def auth_status(claude_path: str, cfg: Config, account: Account, timeout: float = 30) -> dict | None:
    """Run ``claude auth status --json`` under the account's environment.

    Only call this for directories that exist: Claude creates ``.claude.json`` in an empty dir.
    """
    env = build_env(cfg, account, os.environ)
    try:
        result = subprocess.run(
            [claude_path, "auth", "status", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def create_config_dir(path: str) -> bool:
    """Create the directory with mode 0700. Returns True when it was created."""
    if os.path.isdir(path):
        return False
    os.makedirs(path, mode=0o700)
    os.chmod(path, 0o700)
    return True


def seed_dir(src: str, dst: str) -> list[str]:
    """Copy the whitelisted settings files/dirs from one config dir into another."""
    copied: list[str] = []
    for name in SEED_FILES:
        source = os.path.join(src, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(dst, name))
            copied.append(name)
    for name in SEED_DIRS:
        source = os.path.join(src, name)
        if os.path.isdir(source):
            shutil.copytree(source, os.path.join(dst, name), dirs_exist_ok=True, symlinks=True)
            copied.append(name + "/")
    return copied


def discover_existing_dirs() -> list[str]:
    """``~/.claude`` plus every ``~/.claude-*`` directory that looks like a Claude config dir."""
    home = Path(home_dir())
    found: list[str] = []
    stock = Path(stock_config_dir())
    if stock.is_dir():
        found.append(str(stock))
    for candidate in sorted(home.glob(".claude-*")):
        if candidate.is_dir() and any((candidate / marker).exists() for marker in DISCOVERY_MARKERS):
            found.append(str(candidate))
    return found


def deletion_refusal(config_dir: str) -> str | None:
    """Why this directory must never be deleted by ``configure-route remove`` (None = allowed)."""
    real = os.path.realpath(config_dir)
    home = os.path.realpath(home_dir())
    if real == os.path.realpath(stock_config_dir()):
        return "it is the stock ~/.claude directory"
    if real == home:
        return "it is your home directory"
    if real == os.path.abspath(os.sep):
        return "it is the filesystem root"
    if not real.startswith(home + os.sep):
        return "it is outside your home directory"
    return None
