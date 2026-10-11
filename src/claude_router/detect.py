"""Find the Claude Code binary on macOS, Linux and Windows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

IS_WINDOWS = os.name == "nt"
# What CreateProcess can start directly (.ps1/.py/... need an interpreter).
WINDOWS_LAUNCHABLE = (".exe", ".cmd", ".bat", ".com")
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
    own = sys.argv[0] if sys.argv else ""
    if own and os.path.isfile(own):
        try:
            if os.path.samefile(path, own):
                return True
        except OSError:
            pass
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    return any(marker in head for marker in SELF_MARKERS)


def _launchable(path: str | None) -> bool:
    if not path or not os.path.isfile(path):
        return False
    if IS_WINDOWS:
        return os.path.splitext(path)[1].lower() in WINDOWS_LAUNCHABLE
    return os.access(path, os.X_OK)


def usable(path: str | None) -> bool:
    return _launchable(path) and not is_self(path)  # type: ignore[arg-type]


def _variants(path: str) -> list[str]:
    """On Windows a path without a launchable extension may mean ``claude.exe`` or ``claude.cmd``."""
    if IS_WINDOWS and os.path.splitext(path)[1].lower() not in WINDOWS_LAUNCHABLE:
        return [path + ext for ext in WINDOWS_LAUNCHABLE]
    return [path]


def _first_usable(path: str | None) -> str | None:
    if not path:
        return None
    for variant in _variants(path):
        if usable(variant):
            return variant
    return None


def known_locations() -> list[tuple[str, str]]:
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
        local_appdata = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return [
            (os.path.join(home, ".local", "bin", "claude.exe"), "native installer"),
            (os.path.join(appdata, "npm", "claude.cmd"), "npm global"),
            (os.path.join(local_appdata, "Microsoft", "WinGet", "Links", "claude.exe"), "winget"),
            (os.path.join(home, ".claude", "local", "claude.cmd"), "legacy local install"),
        ]
    return [
        (os.path.join(home, ".local", "bin", "claude"), "native installer"),
        (os.path.join(home, ".claude", "local", "claude"), "legacy local install"),
        ("/opt/homebrew/bin/claude", "homebrew"),
        ("/home/linuxbrew/.linuxbrew/bin/claude", "homebrew"),
        ("/usr/local/bin/claude", "/usr/local/bin"),
        ("/usr/bin/claude", "system package"),
    ]


def find_claude(hint: str | None = None) -> str | None:
    """Fast lookup for the hot path (no subprocesses): hint, then PATH, then the known locations."""
    if hint:
        # A bare name is looked up on PATH; anything with a directory part is used as is.
        found = _first_usable(hint if os.path.dirname(hint) else shutil.which(hint))
        if found:
            return found
    found = _first_usable(shutil.which("claude"))
    if found:
        return found
    for path, _source in known_locations():
        found = _first_usable(path)
        if found:
            return found
    return None


def inspect(path: str, source: str) -> Candidate:
    exists = os.path.lexists(path)
    is_symlink = os.path.islink(path)
    target = os.path.realpath(path) if is_symlink else None
    broken = is_symlink and not os.path.exists(path)
    executable = _launchable(path)
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
    env = {key: value for key, value in os.environ.items() if key.upper() != "CLAUDE_CONFIG_DIR"}
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
    npm = shutil.which("npm")  # npm.cmd on Windows: must be called by its full path
    if npm is None:
        return None
    try:
        result = subprocess.run(
            [npm, "prefix", "-g"], capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL
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
        # npm puts global commands in <prefix>\claude.cmd on Windows and <prefix>/bin/claude elsewhere
        npm_bin = os.path.join(npm_prefix, "claude.cmd") if IS_WINDOWS else os.path.join(npm_prefix, "bin", "claude")
        paths.append((npm_bin, "npm global"))

    seen: set[str] = set()
    out: list[Candidate] = []
    for path, source in paths:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        candidate = inspect(path, source)
        if with_version and candidate.executable and not candidate.is_self:
            candidate.version = claude_version(path)
        out.append(candidate)
    return out


def is_batch_file(path: str) -> bool:
    return IS_WINDOWS and os.path.splitext(path)[1].lower() in (".cmd", ".bat")


def install_method(candidate: Candidate) -> str:
    probe = (candidate.target or candidate.path).replace("\\", "/").lower()
    path = candidate.path.replace("\\", "/").lower()
    if "/.local/share/claude/versions/" in probe or path.endswith("/.local/bin/claude.exe"):
        return "native"
    if "/.claude/local/" in path:
        return "legacy local npm"
    if "/node_modules/" in probe or "/npm/" in path:
        return "npm"
    if "/cellar/" in probe or "/homebrew/" in probe or "linuxbrew" in probe:
        return "homebrew"
    if "/winget/" in path:
        return "winget"
    return "unknown"
