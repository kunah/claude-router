"""``route-claude``: pick the account for the current directory and exec Claude Code.

Every argument is forwarded to ``claude`` untouched; nothing is ever written to stdout except in
dry-run mode, so ``route-claude -p ... | ...`` stays clean.
"""

from __future__ import annotations

import json
import os
import sys

from .config import ConfigError, display_path, load_config
from .detect import find_claude
from .resolve import (
    ACCOUNT_ENV,
    Decision,
    ResolveError,
    build_argv,
    build_env,
    candidate_cwds,
    env_changes,
    resolve_account,
)

PROG = "claude-router"
QUIET_ENV = "CLAUDE_ROUTER_QUIET"
DRY_RUN_ENV = "CLAUDE_ROUTER_DRY_RUN"


def _err(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def dry_run_payload(decision: Decision, claude: str, argv: list[str], env: dict[str, str]) -> dict:
    account = decision.account
    return {
        "account": account.name if account else None,
        "config_dir": account.config_dir if account else None,
        "reason": decision.reason,
        "route": decision.route.match if decision.route else None,
        "cwd": decision.cwd,
        "claude": claude,
        "argv": argv,
        "env_changes": env_changes(os.environ, env),
    }


def _announce(cfg, decision: Decision) -> None:
    if os.environ.get(QUIET_ENV):
        return
    mode = cfg.announce
    if mode == "never":
        return
    if mode == "auto" and not (sys.stderr.isatty() and sys.stdout.isatty()):
        return
    if decision.account is None:
        if cfg.exists:
            _err("no route matched and no default account is set; launching claude unchanged")
        return
    how = {
        "override": f"via {ACCOUNT_ENV}",
        "route": f"via route {decision.route.match}" if decision.route else "via route",
        "default": "default account",
    }.get(decision.reason, decision.reason)
    _err(f"{decision.account.name} ({display_path(decision.account.config_dir)}) {how}")


def launch(argv: list[str], env: dict[str, str]) -> int:
    """Replace this process with claude (POSIX) or run it and return its exit code (Windows)."""
    if os.name == "nt":
        import subprocess

        try:
            return subprocess.run(argv, env=env).returncode
        except OSError as exc:
            _err(f"failed to start {argv[0]}: {exc}")
            return 127
    try:
        os.execve(argv[0], argv, env)
    except OSError as exc:
        _err(f"failed to exec {argv[0]}: {exc}")
        return 127
    return 0  # unreachable


def main(argv: list[str] | None = None) -> int:
    user_args = list(sys.argv[1:] if argv is None else argv)

    try:
        cfg = load_config(strict=True)
    except ConfigError as exc:
        _err(f"config error: {exc}")
        _err("fix the file or run `configure-route doctor`; nothing was launched")
        return 2

    try:
        decision = resolve_account(cfg, candidate_cwds(), os.environ.get(ACCOUNT_ENV) or None)
    except ResolveError as exc:
        _err(str(exc))
        return 2

    claude = find_claude(cfg.claude_path)
    if claude is None:
        _err("could not find the `claude` binary; install Claude Code or run `configure-route doctor`")
        return 127
    if cfg.claude_path and claude != cfg.claude_path:
        _err(f"configured claude_path {cfg.claude_path} is unusable; using {claude} (run `configure-route doctor`)")

    env = build_env(cfg, decision.account, os.environ)
    argv_out = build_argv(claude, cfg, decision.account, user_args)

    if os.environ.get(DRY_RUN_ENV):
        print(json.dumps(dry_run_payload(decision, claude, argv_out, env), indent=2))
        return 0

    _announce(cfg, decision)
    return launch(argv_out, env)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
