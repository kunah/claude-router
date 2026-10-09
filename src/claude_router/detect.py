"""Find the Claude Code binary."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

SELF_MARKERS = (b"claude_router", b"route-claude")


@dataclass
class Candidate:
    path: str
    source: str
    exists: bool
    executable: bool
    is_symlink: bool
    target: str | None
    broken: bool
    is_self: bool
    version: str | None = None


def is_self(path: str) -> bool:
    """True when ``path`` is one of our own launchers (prevents a ``claude -> route-claude`` loop)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(1024)
    except OSError:
        return False
    return any(marker in head for marker in SELF_MARKERS)


def usable(path: str | None) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK) and not is_self(path)


def known_locations() -> list[tuple[str, str]]:
    home = os.path.expanduser("~")
    exe = "claude.exe" if sys.platform == "win32" else "claude"
    return [
        (os.path.join(home, ".local", "bin", exe), "native installer"),
        (os.path.join(home, ".claude", "local", "claude"), "legacy local install"),
        ("/opt/homebrew/bin/claude", "homebrew"),
        ("/usr/local/bin/claude", "/usr/local/bin"),
    ]


def find_claude(hint: str | None = None) -> str | None:
    """Fast lookup for the hot path (no subprocesses): hint, then PATH, then the known locations."""
    if hint:
        candidate = hint if os.path.isabs(hint) else shutil.which(hint)
        if usable(candidate):
            return candidate
    found = shutil.which("claude")
    if usable(found):
        return found
    for path, _source in known_locations():
        if usable(path):
            return path
    return None


def inspect(path: str, source: str) -> Candidate:
    exists = os.path.lexists(path)
    is_symlink = os.path.islink(path)
    target = os.path.realpath(path) if is_symlink else None
    broken = is_symlink and not os.path.exists(path)
    executable = os.path.isfile(path) and os.access(path, os.X_OK)
    return Candidate(
        path=path,
        source=source,
        exists=exists,
        executable=executable,
        is_symlink=is_symlink,
        target=target,
        broken=broken,
        is_self=is_self(path) if executable else False,
    )


def claude_version(path: str, timeout: float = 10) -> str | None:
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_CONFIG_DIR"}
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0].strip() if output else None


def _npm_prefix() -> str | None:
    if shutil.which("npm") is None:
        return None
    try:
        result = subprocess.run(
            ["npm", "prefix", "-g"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    prefix = result.stdout.strip()
    return prefix or None


def all_candidates(hint: str | None = None, *, with_version: bool = True) -> list[Candidate]:
    """Every location worth reporting in ``doctor`` (may run ``npm`` and ``claude --version``)."""
    paths: list[tuple[str, str]] = []
    if hint:
        paths.append((hint, "config claude_path"))
    found = shutil.which("claude")
    if found:
        paths.append((found, "PATH"))
    paths.extend(known_locations())
    nvm_bin = os.environ.get("NVM_BIN")
    if nvm_bin:
        paths.append((os.path.join(nvm_bin, "claude"), "nvm"))
    npm_prefix = _npm_prefix()
    if npm_prefix:
        paths.append((os.path.join(npm_prefix, "bin", "claude"), "npm global"))

    seen: set[str] = set()
    out: list[Candidate] = []
    for path, source in paths:
        if path in seen:
            continue
        seen.add(path)
        candidate = inspect(path, source)
        if with_version and candidate.executable and not candidate.is_self:
            candidate.version = claude_version(path)
        out.append(candidate)
    return out


def install_method(candidate: Candidate) -> str:
    probe = (candidate.target or candidate.path).replace("\\", "/")
    if "/.local/share/claude/versions/" in probe:
        return "native"
    if "/.claude/local/" in candidate.path.replace("\\", "/"):
        return "legacy local npm"
    if "/node_modules/" in probe:
        return "npm"
    if "/Cellar/" in probe or "/homebrew/" in probe:
        return "homebrew"
    return "unknown"
