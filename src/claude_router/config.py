"""Configuration model for claude-router (``~/.claude-router.yaml``).

Only the standard library and PyYAML are imported here: this module sits on the
``route-claude`` hot path.

Paths work the same way on POSIX and Windows: they are compared case-insensitively on
Windows, and globs/regexes are always matched against paths written with ``/``
(``C:/Users/me/dev`` on Windows), so one config style works everywhere.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

IS_WINDOWS = os.name == "nt"
SEPARATORS = "/\\" if IS_WINDOWS else "/"
MATCH_FLAGS = re.IGNORECASE if IS_WINDOWS else 0  # Windows paths are case-insensitive

CONFIG_ENV = "CLAUDE_ROUTER_CONFIG"
DEFAULT_CONFIG_NAME = ".claude-router.yaml"
CONFIG_VERSION = 1
ANNOUNCE_MODES = ("auto", "always", "never")
REGEX_PREFIX = "re:"
GLOB_CHARS = "*?["
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_REGEX_META = set(".^$*+?{}[]\\|()")
_BARE_DRIVE = re.compile(r"^[A-Za-z]:$")
_ROOTED_MATCH_FORM = re.compile(r"^(?:/|[A-Za-z]:/)")
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
    config_dir: str  # canonical: absolute, ``~`` expanded, normalised, *not* realpath'd
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    unset_env: list[str] = field(default_factory=list)


@dataclass
class Route:
    match: str  # as written in the file (for display and round-tripping)
    account: str
    kind: RouteKind
    pattern: str  # path: native absolute path; glob/regex: pattern over "/"-separated paths
    specificity: int  # components in the literal directory prefix
    index: int  # position in the file (tie breaker)
    realpath: str | None = None  # path kind only: realpath when it differs from ``pattern``
    compiled: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def matcher(self) -> re.Pattern[str]:
        if self.compiled is None:
            source = fnmatch.translate(self.pattern) if self.kind == "glob" else self.pattern
            self.compiled = re.compile(source, MATCH_FLAGS)
        return self.compiled

    def key(self) -> tuple[str, str]:
        """Identity used to spot duplicate routes."""
        if self.kind == "path":
            return (self.kind, os.path.normcase(self.pattern))
        return (self.kind, self.pattern.lower() if IS_WINDOWS else self.pattern)


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


def is_absolute(path: str) -> bool:
    """Absolute on this platform. On Windows that needs a drive (or UNC share) and a root."""
    if IS_WINDOWS:
        drive, rest = os.path.splitdrive(path)
        return bool(drive) and rest[:1] in ("\\", "/")
    return os.path.isabs(path)


def expand_path(raw: str) -> str:
    """Expand ``~``, normalise to NFC and to the platform's separators; no trailing separator."""
    text = unicodedata.normalize("NFC", str(raw).strip())
    if not text:
        return text
    return os.path.normpath(os.path.expanduser(text))


def canonical_dir(raw: str, *, what: str = "path") -> str:
    """The exact string exported as ``CLAUDE_CONFIG_DIR`` (Claude Code hashes it for the Keychain)."""
    text = expand_path(raw)
    if not text or not is_absolute(text):
        raise ConfigError(f"{what} must be an absolute path or start with '~' (got {raw!r})")
    return text


def same_path(first: str, second: str) -> bool:
    """True when both name the same file or directory (case-insensitive where the OS is)."""
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def is_stock_dir(path: str) -> bool:
    return same_path(path, stock_config_dir())


def match_form(path: str) -> str:
    """The form globs and regexes are matched against: ``/`` as separator on every platform."""
    return path.replace("\\", "/") if IS_WINDOWS else path


def path_components(path: str) -> list[str]:
    return [part for part in re.split(r"[\\/]+" if IS_WINDOWS else r"/+", path) if part]


def display_path(path: str) -> str:
    """``~/...`` for paths inside the home directory (always with ``/``), the path itself otherwise."""
    if not path:
        return path
    home = os.path.normpath(home_dir())
    norm = os.path.normpath(path)
    if os.path.normcase(norm) == os.path.normcase(home):
        return "~"
    prefix = home.rstrip(SEPARATORS) + os.sep
    if os.path.normcase(norm).startswith(os.path.normcase(prefix)):
        return "~/" + match_form(norm[len(prefix) :])
    return path


def portable_path(path: str) -> str:
    """How paths are stored in the config file: ``~`` for home and ``/`` separators everywhere."""
    return match_form(display_path(path))


def split_command(text: str) -> list[str]:
    """Split a command line roughly the way the platform's shell would."""
    if not IS_WINDOWS:
        return shlex.split(text)
    tokens: list[str] = []
    for token in shlex.split(text, posix=False):  # keeps backslashes in Windows paths
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        tokens.append(token)
    return tokens


# --------------------------------------------------------------------------- routes


def _strip_trailing_separators(text: str) -> str:
    stripped = text.rstrip(SEPARATORS)
    if not stripped:
        return text[:1]  # the root itself
    if IS_WINDOWS and _BARE_DRIVE.match(stripped):
        return stripped + "\\"  # "C:" alone means "C:\"
    return stripped


def normalise_match(raw: str) -> str:
    """Normalise a user supplied match for storage: ``X/``, ``X/*`` and ``X/**`` all become ``X``."""
    text = raw.strip()
    if text.startswith(REGEX_PREFIX):
        return text
    text = _strip_trailing_separators(text)
    for sep in SEPARATORS:
        for suffix in (sep + "**", sep + "*"):
            if text.endswith(suffix) and len(text) > len(suffix):
                return _strip_trailing_separators(text[: -len(suffix)])
    return text or raw.strip()


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
        home = re.escape(match_form(home_dir()))
        for lead in ("^~/", "~/"):  # a leading ~ means the home directory
            if body.startswith(lead):
                body = lead[:-2] + home + "/" + body[len(lead) :]
                break
        try:
            compiled = re.compile(body, MATCH_FLAGS)
        except re.error as exc:
            raise ConfigError(f"routes[{index}]: invalid regex {match!r}: {exc}") from exc
        prefix = _regex_literal_prefix(body)
        specificity = len(path_components(prefix)) if _ROOTED_MATCH_FORM.match(prefix) else 0
        return Route(match, account, "regex", body, specificity, index, compiled=compiled)

    text = expand_path(normalise_match(match))
    if not is_absolute(text):
        raise ConfigError(
            f"routes[{index}]: {match!r} must be an absolute path, start with '~', or use the 're:' prefix"
        )
    if any(char in text for char in GLOB_CHARS):
        pattern = match_form(text)
        specificity = 0
        for part in path_components(pattern):
            if any(char in part for char in GLOB_CHARS):
                break
            specificity += 1
        compiled = re.compile(fnmatch.translate(pattern), MATCH_FLAGS)
        return Route(match, account, "glob", pattern, specificity, index, compiled=compiled)

    real = os.path.realpath(text)
    alternative = real if os.path.normcase(real) != os.path.normcase(text) else None
    return Route(match, account, "path", text, len(path_components(text)), index, alternative)


# --------------------------------------------------------------------------- (de)serialisation


def _str_list(value: Any, what: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return split_command(value)
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
    out: dict[str, Any] = {"config_dir": portable_path(account.config_dir)}
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


def _replace(source: str, target: str) -> None:
    """``os.replace``, retried briefly on Windows where a concurrent reader can hold the target open."""
    attempts = 20 if IS_WINDOWS else 1
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)


def save_config(cfg: Config) -> None:
    """Write atomically (temp file + rename) with mode 0600 and LF line endings."""
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    text = _HEADER + yaml.safe_dump(to_dict(cfg), sort_keys=False, allow_unicode=True)
    handle, tmp = tempfile.mkstemp(prefix=".claude-router.", suffix=".tmp", dir=str(cfg.path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.chmod(tmp, 0o600)
        _replace(tmp, str(cfg.path))
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

    accounts = list(cfg.accounts.values())
    for position, account in enumerate(accounts):
        for other in accounts[:position]:
            if same_path(account.config_dir, other.config_dir):
                error(
                    f"accounts {other.name!r} and {account.name!r} use the same directory "
                    f"{display_path(account.config_dir)}"
                )
                break
        if not os.path.isdir(account.config_dir):
            warning(
                f"account {account.name!r}: directory {display_path(account.config_dir)} does not exist "
                f"(run `configure-route login {account.name}` to create it and sign in)"
            )

    seen: dict[tuple[str, str], int] = {}
    for route in cfg.routes:
        if route.account not in cfg.accounts:
            error(f"route {route.match!r} points to unknown account {route.account!r}")
        key = route.key()
        if key in seen:
            warning(f"route #{route.index + 1} {route.match!r} duplicates route #{seen[key] + 1} and will never win")
        else:
            seen[key] = route.index
        if route.kind == "path" and not os.path.isdir(route.pattern):
            warning(f"route {route.match!r}: directory does not exist")
    return problems
