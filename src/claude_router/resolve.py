"""Pure routing logic: which account for which directory, and the env/argv to launch it with."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from .config import IS_WINDOWS, Account, Config, Route, expand_path, is_absolute, is_stock_dir, match_form

ACCOUNT_ENV = "CLAUDE_ROUTER_ACCOUNT"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
AUTH_OVERRIDE_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_PROFILE")


class ResolveError(Exception):
    """The config cannot produce a decision (unknown override/default/route account)."""


@dataclass
class Match:
    route: Route
    matched_dir: str  # the cwd or ancestor that matched


@dataclass
class Decision:
    account: Account | None
    reason: str  # "override" | "route" | "default" | "none"
    route: Route | None = None
    matches: list[Match] = field(default_factory=list)  # every matching route, best first
    cwd: str = ""


def env_key(name: str) -> str:
    """Environment variable names are case-insensitive on Windows (Python keeps them upper-case)."""
    return name.upper() if IS_WINDOWS else name


def candidate_cwds(
    path: str | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> list[str]:
    """Directory strings to match against.

    For an explicit ``path``: the expanded absolute path plus its realpath. Otherwise the logical
    ``$PWD`` (when it names the same directory, so symlinked paths keep matching), the physical cwd,
    and its realpath when that differs. A non-native ``$PWD`` (Git Bash's ``/c/...``) is ignored.
    """
    if path is not None:
        expanded = os.path.abspath(expand_path(path))
        real = os.path.realpath(expanded)
        return [expanded] + ([real] if os.path.normcase(real) != os.path.normcase(expanded) else [])
    environ = os.environ if environ is None else environ
    physical = cwd or os.getcwd()
    out: list[str] = []
    logical = environ.get("PWD")
    if logical and is_absolute(logical) and logical != physical:
        try:
            if os.path.samefile(logical, physical):
                out.append(expand_path(logical))
        except OSError:
            pass
    out.append(physical)
    real = os.path.realpath(physical)  # subst drives, junctions and short names on Windows
    if os.path.normcase(real) != os.path.normcase(physical):
        out.append(real)
    return out


def ancestors(path: str) -> list[str]:
    """``/a/b/c`` -> ``['/a/b/c', '/a/b', '/a', '/']`` (``C:\\a`` -> ``['C:\\a', 'C:\\']`` on Windows)."""
    out = [path]
    while True:
        parent = os.path.dirname(path)
        if parent == path:
            break
        out.append(parent)
        path = parent
    return out


def _is_within(child: str, parent: str) -> bool:
    child, parent = os.path.normcase(child), os.path.normcase(parent)
    if child == parent:
        return True
    if not parent.endswith(os.sep):
        parent += os.sep
    return child.startswith(parent)


def route_matches(route: Route, cwd: str) -> str | None:
    """Return the directory (cwd or an ancestor) the route matched, or None."""
    if route.kind == "path":
        for prefix in (route.pattern, route.realpath):
            if prefix and _is_within(cwd, prefix):
                return prefix
        return None
    matcher = route.matcher()
    for directory in ancestors(cwd):
        form = match_form(directory)
        found = matcher.match(form) if route.kind == "glob" else matcher.search(form)
        if found:
            return directory
    return None


def resolve_account(cfg: Config, cwds: list[str], override: str | None = None) -> Decision:
    cwd = cwds[0] if cwds else os.getcwd()
    if override:
        account = cfg.accounts.get(override)
        if account is None:
            known = ", ".join(cfg.accounts) or "none"
            raise ResolveError(f"{ACCOUNT_ENV}={override!r} is not a configured account (known: {known})")
        return Decision(account=account, reason="override", cwd=cwd)

    matches: list[Match] = []
    for route in cfg.routes:
        for candidate in cwds:
            matched = route_matches(route, candidate)
            if matched is not None:
                matches.append(Match(route=route, matched_dir=matched))
                break
    if matches:
        matches.sort(key=lambda m: (-m.route.specificity, m.route.index))
        best = matches[0].route
        account = cfg.accounts.get(best.account)
        if account is None:
            raise ResolveError(f"route {best.match!r} points to unknown account {best.account!r}")
        return Decision(account=account, reason="route", route=best, matches=matches, cwd=cwd)

    if cfg.default:
        account = cfg.accounts.get(cfg.default)
        if account is None:
            raise ResolveError(f"default account {cfg.default!r} is not defined")
        return Decision(account=account, reason="default", cwd=cwd)
    return Decision(account=None, reason="none", cwd=cwd)


def build_env(cfg: Config, account: Account | None, base: Mapping[str, str]) -> dict[str, str]:
    """Environment for the child process. ``CLAUDE_CONFIG_DIR`` is unset for the stock directory."""
    env = {env_key(key): value for key, value in base.items()}
    if account is None:
        return env
    env.pop(CONFIG_DIR_ENV, None)
    if not is_stock_dir(account.config_dir):
        env[CONFIG_DIR_ENV] = account.config_dir
    for name in (*cfg.unset_env, *account.unset_env):
        env.pop(env_key(name), None)
    for key, value in account.env.items():
        env[env_key(key)] = value
    env[ACCOUNT_ENV] = account.name
    return env


def env_changes(base: Mapping[str, str], env: Mapping[str, str]) -> dict[str, str | None]:
    """Variables that differ between ``base`` and ``env`` (None = removed)."""
    before = {env_key(key): value for key, value in base.items()}
    out: dict[str, str | None] = {}
    for key in set(before) | set(env):
        if before.get(key) != env.get(key):
            out[key] = env.get(key)
    return dict(sorted(out.items()))


def build_argv(
    claude_path: str,
    cfg: Config,
    account: Account | None,
    user_args: list[str],
    *,
    include_defaults: bool = True,
) -> list[str]:
    """``[claude, *global args, *account args, *user args]``; user args last so they win."""
    argv = [claude_path]
    if include_defaults:
        argv.extend(cfg.args)
        if account is not None:
            argv.extend(account.args)
    argv.extend(user_args)
    return argv
