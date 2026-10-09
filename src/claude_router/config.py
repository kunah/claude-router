"""Configuration model for claude-router (``~/.claude-router.yaml``).

Only the standard library and PyYAML are imported here: this module sits on the
``route-claude`` hot path.
"""

from __future__ import annotations

import os
import re
import shlex
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

CONFIG_ENV = "CLAUDE_ROUTER_CONFIG"
DEFAULT_CONFIG_NAME = ".claude-router.yaml"
CONFIG_VERSION = 1
ANNOUNCE_MODES = ("auto", "always", "never")
REGEX_PREFIX = "re:"
GLOB_CHARS = "*?["
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_REGEX_META = set(".^$*+?{}[]\\|()")
_KNOWN_KEYS = {"version", "claude_path", "default", "args", "unset_env", "announce", "accounts", "routes"}
_HEADER = (
    "# claude-router configuration.\n"
    "# Edit by hand or with `configure-route`. Comments are dropped whenever the tool rewrites the file.\n"
)

RouteKind = Literal["path", "glob", "regex"]


class ConfigError(Exception):
    """The config file is unusable; ``route-claude`` must not launch anything."""


@dataclass
class Account:
    name: str
    config_dir: str  # canonical: absolute, ``~`` expanded, no trailing slash, *not* realpath'd
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    unset_env: list[str] = field(default_factory=list)


@dataclass
class Route:
    match: str  # as written in the file (for display and round-tripping)
    account: str
    kind: RouteKind
    pattern: str  # expanded pattern used for matching
    specificity: int  # components in the literal directory prefix
    index: int  # position in the file (tie breaker)
    realpath: str | None = None  # path kind only: realpath when it differs from ``pattern``


@dataclass
class Problem:
    level: Literal["error", "warning"]
    message: str


@dataclass
class Config:
    path: Path
    exists: bool = False
    claude_path: str | None = None
    default: str | None = None
    args: list[str] = field(default_factory=list)
    unset_env: list[str] = field(default_factory=list)
    announce: str = "auto"
    accounts: dict[str, Account] = field(default_factory=dict)
    routes: list[Route] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)  # unknown top-level keys, preserved on save


# --------------------------------------------------------------------------- paths


def home_dir() -> str:
    return os.path.expanduser("~")


def config_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(os.path.expanduser(override))
    return Path(home_dir()) / DEFAULT_CONFIG_NAME


def stock_config_dir() -> str:
    """The directory Claude Code uses when ``CLAUDE_CONFIG_DIR`` is not set."""
    return os.path.join(home_dir(), ".claude")


def is_stock_dir(path: str) -> bool:
    return os.path.realpath(path) == os.path.realpath(stock_config_dir())


def expand_path(raw: str) -> str:
    """Expand ``~``, normalise to NFC and strip trailing slashes. Keeps symlinks and relative paths."""
    text = unicodedata.normalize("NFC", str(raw).strip())
    text = os.path.expanduser(text)
    if len(text) > 1:
        text = text.rstrip("/") or "/"
    return text


def canonical_dir(raw: str, *, what: str = "path") -> str:
    """The exact string exported as ``CLAUDE_CONFIG_DIR`` (Claude Code hashes it for the Keychain)."""
    text = expand_path(raw)
    if not os.path.isabs(text):
        raise ConfigError(f"{what} must be an absolute path or start with '~' (got {raw!r})")
    return os.path.normpath(text)


def display_path(path: str) -> str:
    home = home_dir()
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


# --------------------------------------------------------------------------- routes


def normalise_match(raw: str) -> str:
    """Normalise a user supplied match for storage: ``X/``, ``X/*`` and ``X/**`` all become ``X``."""
    text = raw.strip()
    if text.startswith(REGEX_PREFIX):
        return text
    if len(text) > 1:
        text = text.rstrip("/") or "/"
    for suffix in ("/**", "/*"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text or raw.strip()


def _components(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def _regex_literal_prefix(body: str) -> str:
    """Literal directory prefix of a regex (after an optional ``^``), honouring escaped punctuation."""
    literal = body[1:] if body.startswith("^") else body
    out: list[str] = []
    i = 0
    while i < len(literal):
        char = literal[i]
        if char == "\\":
            if i + 1 < len(literal) and not literal[i + 1].isalnum():
                out.append(literal[i + 1])
                i += 2
                continue
            break
        if char in _REGEX_META:
            break
        out.append(char)
        i += 1
    return "".join(out)


def parse_route(raw: Any, index: int) -> Route:
    if not isinstance(raw, dict):
        raise ConfigError(f"routes[{index}] must be a mapping with 'match' and 'account'")
    match = raw.get("match")
    account = raw.get("account")
    if not isinstance(match, str) or not match.strip():
        raise ConfigError(f"routes[{index}]: 'match' must be a non-empty string")
    if not isinstance(account, str) or not account.strip():
        raise ConfigError(f"routes[{index}]: 'account' must be a non-empty string")
    match = match.strip()
    account = account.strip()

    if match.startswith(REGEX_PREFIX):
        body = match[len(REGEX_PREFIX) :]
        if not body:
            raise ConfigError(f"routes[{index}]: empty regex")
        for lead in ("^~/", "~/"):  # a leading ~ means the home directory
            if body.startswith(lead):
                body = lead[:-2] + re.escape(home_dir()) + "/" + body[len(lead) :]
                break
        try:
            re.compile(body)
        except re.error as exc:
            raise ConfigError(f"routes[{index}]: invalid regex {match!r}: {exc}") from exc
        prefix = _regex_literal_prefix(body)
        specificity = len(_components(prefix)) if prefix.startswith("/") else 0
        return Route(match, account, "regex", body, specificity, index)

    text = expand_path(normalise_match(match))
    if not os.path.isabs(text):
        raise ConfigError(
            f"routes[{index}]: {match!r} must be an absolute path, start with '~', or use the 're:' prefix"
        )
    if any(char in text for char in GLOB_CHARS):
        specificity = 0
        for part in _components(text):
            if any(char in part for char in GLOB_CHARS):
                break
            specificity += 1
        return Route(match, account, "glob", text, specificity, index)

    text = os.path.normpath(text)
    real = os.path.realpath(text)
    return Route(match, account, "path", text, len(_components(text)), index, real if real != text else None)


# --------------------------------------------------------------------------- (de)serialisation


def _str_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    if isinstance(value, list) and all(isinstance(item, (str, int, float)) for item in value):
        return [str(item) for item in value]
    raise ConfigError(f"{what} must be a list of strings")


def _str_dict(value: Any, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): str(val) for key, val in value.items()}
    raise ConfigError(f"{what} must be a mapping of NAME: value")


def from_dict(data: Any, path: Path, *, exists: bool) -> Config:
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: the top level must be a mapping")
    cfg = Config(path=path, exists=exists)
    cfg.extra = {key: value for key, value in data.items() if key not in _KNOWN_KEYS}

    claude_path = data.get("claude_path")
    if claude_path is not None:
        if not isinstance(claude_path, str):
            raise ConfigError("claude_path must be a string")
        cfg.claude_path = expand_path(claude_path) or None

    default = data.get("default")
    if default is not None:
        if not isinstance(default, str) or not default.strip():
            raise ConfigError("default must be an account name")
        cfg.default = default.strip()

    cfg.args = _str_list(data.get("args"), "args")
    cfg.unset_env = _str_list(data.get("unset_env"), "unset_env")

    announce = data.get("announce", "auto")
    if announce is True:
        announce = "always"
    elif announce is False:
        announce = "never"
    if announce not in ANNOUNCE_MODES:
        raise ConfigError(f"announce must be one of {', '.join(ANNOUNCE_MODES)} (got {announce!r})")
    cfg.announce = announce

    accounts = data.get("accounts") or {}
    if not isinstance(accounts, dict):
        raise ConfigError("accounts must be a mapping of name -> settings")
    for raw_name, spec in accounts.items():
        name = str(raw_name)
        if not NAME_RE.match(name):
            raise ConfigError(f"account name {name!r} is invalid (letters, digits, '.', '-' and '_' only)")
        if spec is None:
            spec = {}
        if isinstance(spec, str):
            spec = {"config_dir": spec}
        if not isinstance(spec, dict):
            raise ConfigError(f"account {name!r} must be a mapping with config_dir")
        config_dir = spec.get("config_dir")
        if not isinstance(config_dir, str) or not config_dir.strip():
            raise ConfigError(f"account {name!r}: config_dir is required")
        cfg.accounts[name] = Account(
            name=name,
            config_dir=canonical_dir(config_dir, what=f"account {name!r} config_dir"),
            args=_str_list(spec.get("args"), f"account {name!r} args"),
            env=_str_dict(spec.get("env"), f"account {name!r} env"),
            unset_env=_str_list(spec.get("unset_env"), f"account {name!r} unset_env"),
        )

    routes = data.get("routes") or []
    if not isinstance(routes, list):
        raise ConfigError("routes must be a list")
    cfg.routes = [parse_route(raw, index) for index, raw in enumerate(routes)]
    return cfg


def _account_dict(account: Account) -> dict[str, Any]:
    out: dict[str, Any] = {"config_dir": display_path(account.config_dir)}
    if account.args:
        out["args"] = list(account.args)
    if account.env:
        out["env"] = dict(account.env)
    if account.unset_env:
        out["unset_env"] = list(account.unset_env)
    return out


def to_dict(cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {"version": CONFIG_VERSION}
    if cfg.claude_path:
        out["claude_path"] = cfg.claude_path
    if cfg.default:
        out["default"] = cfg.default
    if cfg.args:
        out["args"] = list(cfg.args)
    if cfg.unset_env:
        out["unset_env"] = list(cfg.unset_env)
    if cfg.announce != "auto":
        out["announce"] = cfg.announce
    out["accounts"] = {name: _account_dict(account) for name, account in cfg.accounts.items()}
    out["routes"] = [{"match": route.match, "account": route.account} for route in cfg.routes]
    for key, value in cfg.extra.items():
        out.setdefault(key, value)
    return out


def load_config(path: Path | None = None, *, strict: bool = False) -> Config:
    """Load the config. A missing file yields an empty ``Config`` with ``exists=False``.

    With ``strict=True`` validation errors raise ``ConfigError`` (used by ``route-claude``).
    """
    path = path or config_path()
    if not path.exists():
        return Config(path=path, exists=False)
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    cfg = from_dict(data, path, exists=True)
    if strict:
        errors = [problem.message for problem in validate(cfg) if problem.level == "error"]
        if errors:
            raise ConfigError(f"{path}: " + "; ".join(errors))
    return cfg


def save_config(cfg: Config) -> None:
    """Write atomically (temp file + rename) with mode 0600."""
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    text = _HEADER + yaml.safe_dump(to_dict(cfg), sort_keys=False, allow_unicode=True)
    handle, tmp = tempfile.mkstemp(prefix=".claude-router.", suffix=".tmp", dir=str(cfg.path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, cfg.path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    cfg.exists = True


def validate(cfg: Config) -> list[Problem]:
    problems: list[Problem] = []

    def error(message: str) -> None:
        problems.append(Problem("error", message))

    def warning(message: str) -> None:
        problems.append(Problem("warning", message))

    if cfg.default and cfg.default not in cfg.accounts:
        error(f"default account {cfg.default!r} is not defined")

    by_realpath: dict[str, str] = {}
    for account in cfg.accounts.values():
        real = os.path.realpath(account.config_dir)
        if real in by_realpath:
            error(
                f"accounts {by_realpath[real]!r} and {account.name!r} use the same directory "
                f"{display_path(account.config_dir)}"
            )
        else:
            by_realpath[real] = account.name
        if not os.path.isdir(account.config_dir):
            warning(
                f"account {account.name!r}: directory {display_path(account.config_dir)} does not exist "
                f"(run `configure-route login {account.name}` to create it and sign in)"
            )

    seen: dict[tuple[str, str], int] = {}
    for route in cfg.routes:
        if route.account not in cfg.accounts:
            error(f"route {route.match!r} points to unknown account {route.account!r}")
        key = (route.kind, route.pattern)
        if key in seen:
            warning(f"route #{route.index + 1} {route.match!r} duplicates route #{seen[key] + 1} and will never win")
        else:
            seen[key] = route.index
        if route.kind == "path" and not os.path.isdir(route.pattern):
            warning(f"route {route.match!r}: directory does not exist")
    return problems
