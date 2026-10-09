"""``configure-route``: manage accounts, routes and defaults for ``route-claude``."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NoReturn, Optional

import typer

from . import __version__, accounts, detect, resolve
from .config import (
    NAME_RE,
    Account,
    Config,
    ConfigError,
    canonical_dir,
    display_path,
    home_dir,
    is_stock_dir,
    load_config,
    normalise_match,
    parse_route,
    save_config,
    stock_config_dir,
    validate,
)
from .route import DRY_RUN_ENV, PROG, dry_run_payload

app = typer.Typer(
    help="Configure how `route-claude` picks a Claude Code account for each directory.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)
route_app = typer.Typer(help="Manage directory -> account routes.", no_args_is_help=True, rich_markup_mode=None)
app.add_typer(route_app, name="route")

RED, GREEN, YELLOW = typer.colors.RED, typer.colors.GREEN, typer.colors.YELLOW
SHELL_RC_FILES = (".zshrc", ".bashrc", ".bash_profile", ".profile")
ALIAS_LINE = "alias claude='route-claude'"


# ----------------------------------------------------------------------------- helpers


def _fail(message: str, code: int = 1) -> NoReturn:
    typer.secho(f"error: {message}", fg=RED, err=True)
    raise typer.Exit(code)


def _warn(message: str) -> None:
    typer.secho(f"warning: {message}", fg=YELLOW, err=True)


def _load() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        _fail(str(exc), 2)


def _save(cfg: Config) -> None:
    try:
        save_config(cfg)
    except OSError as exc:
        _fail(f"cannot write {cfg.path}: {exc}")


def _require_account(cfg: Config, name: str) -> Account:
    account = cfg.accounts.get(name)
    if account is None:
        known = ", ".join(cfg.accounts) or "none configured"
        _fail(f"unknown account {name!r} (known: {known})")
    return account


def _account_for_dir(cfg: Config, path: str) -> Account | None:
    real = os.path.realpath(path)
    for account in cfg.accounts.values():
        if os.path.realpath(account.config_dir) == real:
            return account
    return None


def _check_name(cfg: Config, name: str) -> None:
    if not NAME_RE.match(name):
        _fail(f"account name {name!r} is invalid (letters, digits, '.', '-' and '_' only)")
    if name in cfg.accounts:
        _fail(f"account {name!r} already exists")


def _suggest_name(cfg: Config, base: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "-", base).strip("-.") or "account"
    if not NAME_RE.match(base):
        base = "account"
    name, counter = base, 2
    while name in cfg.accounts:
        name = f"{base}-{counter}"
        counter += 1
    return name


def _parse_env(items: Optional[list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            _fail(f"--env expects KEY=VALUE (got {item!r})")
        out[key.strip()] = value
    return out


def _absolute_match(match: str) -> str:
    """Resolve a route match for storage: regexes untouched, relative paths against the shell cwd, home as ~."""
    text = normalise_match(match)
    if text.startswith("re:"):
        return text
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        cwd = os.getcwd()
        logical = os.environ.get("PWD")
        if logical and os.path.isabs(logical):
            try:
                if os.path.samefile(logical, cwd):
                    cwd = logical
            except OSError:
                pass
        expanded = os.path.normpath(os.path.join(cwd, expanded))
    return display_path(normalise_match(expanded))


def _who(config_dir: str) -> str:
    identity = accounts.login_identity(config_dir)
    return f" (logged in as {identity.email})" if identity and identity.email else ""


def _register(cfg: Config, name: str, path: str) -> Account:
    _check_name(cfg, name)
    other = _account_for_dir(cfg, path)
    if other is not None:
        _fail(f"{display_path(path)} is already registered as account {other.name!r}")
    account = Account(name=name, config_dir=path)
    cfg.accounts[name] = account
    return account


def _table(headers: list[str], rows: list[list[object]]) -> str:
    cells = [[str(cell) for cell in row] for row in rows]
    widths = [max(len(headers[i]), *(len(row[i]) for row in cells)) if cells else len(headers[i]) for i in range(len(headers))]
    fmt = "  ".join("{:<" + str(width) + "}" for width in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * width for width in widths))]
    lines.extend(fmt.format(*row) for row in cells)
    return "\n".join(line.rstrip() for line in lines)


def _routes_table(cfg: Config) -> str:
    if not cfg.routes:
        return "No routes: every directory uses the default account."
    rows: list[list[object]] = [
        [index + 1, route.match, route.kind, route.specificity, route.account] for index, route in enumerate(cfg.routes)
    ]
    return _table(["#", "MATCH", "KIND", "SPECIFICITY", "ACCOUNT"], rows)


def shell_alias_lines() -> list[tuple[str, int, str]]:
    """Best-effort scan of shell rc files for an existing ``alias claude=...`` line."""
    out: list[tuple[str, int, str]] = []
    for rc_name in SHELL_RC_FILES:
        rc_path = Path(home_dir()) / rc_name
        try:
            lines = rc_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            if re.match(r"^\s*alias\s+claude\s*=", line):
                out.append((str(rc_path), number, line.strip()))
    return out


def _print_shell_hint() -> None:
    typer.echo()
    typer.echo("To send plain `claude` through the router, add this line to your shell rc file (e.g. ~/.zshrc):")
    typer.echo(f"    {ALIAS_LINE}")
    for path, number, line in shell_alias_lines():
        if "route-claude" in line:
            continue
        typer.echo(f"Note: {display_path(path)}:{number} currently has `{line}`; replace it with the line above.")
        typer.echo("      Extra flags belong in the router config, e.g. `configure-route args -- --effort max`.")


def _exec_account(cfg: Config, account: Account, user_args: list[str], *, include_defaults: bool = True) -> None:
    claude = detect.find_claude(cfg.claude_path)
    if claude is None:
        _fail("could not find the `claude` binary; run `configure-route doctor`", 127)
    env = resolve.build_env(cfg, account, os.environ)
    argv = resolve.build_argv(claude, cfg, account, user_args, include_defaults=include_defaults)
    if os.environ.get(DRY_RUN_ENV):
        decision = resolve.Decision(account=account, reason="override", cwd=os.getcwd())
        typer.echo(json.dumps(dry_run_payload(decision, claude, argv, env), indent=2))
        return
    typer.secho(f"{PROG}: {account.name} ({display_path(account.config_dir)}) via configure-route", err=True, dim=True)
    if os.name == "nt":
        raise typer.Exit(subprocess.run(argv, env=env).returncode)
    os.execve(argv[0], argv, env)


# ----------------------------------------------------------------------------- commands


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"claude-router {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """Configure how `route-claude` picks a Claude Code account for each directory."""


@app.command()
def init(
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Account name for the stock ~/.claude directory."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Accept every suggestion without prompting."),
) -> None:
    """Create the config, register the Claude config dirs already on disk, then run doctor."""
    cfg = _load()
    was_new = not cfg.exists
    stock = stock_config_dir()

    if os.path.isdir(stock):
        if _account_for_dir(cfg, stock) is None:
            if name:
                _check_name(cfg, name)
                account_name = name
            else:
                suggestion = _suggest_name(cfg, "main")
                account_name = (
                    suggestion
                    if yes
                    else typer.prompt(f"Account name for the stock config dir {display_path(stock)}{_who(stock)}", default=suggestion)
                )
            _register(cfg, account_name, stock)
            typer.echo(f"Registered {display_path(stock)} as '{account_name}'{_who(stock)}")
    else:
        typer.echo(f"Stock config dir {display_path(stock)} not found; skipping it")

    for directory in accounts.discover_existing_dirs():
        if is_stock_dir(directory) or _account_for_dir(cfg, directory) is not None:
            continue
        suggestion = _suggest_name(cfg, os.path.basename(directory).removeprefix(".claude-"))
        if yes:
            account_name = suggestion
        else:
            if not typer.confirm(
                f"Register existing config dir {display_path(directory)}{_who(directory)} as an account?", default=True
            ):
                continue
            account_name = typer.prompt("Account name", default=suggestion)
        _register(cfg, account_name, directory)
        typer.echo(f"Registered {display_path(directory)} as '{account_name}'{_who(directory)}")

    if not cfg.default:
        stock_account = _account_for_dir(cfg, stock)
        if stock_account is not None:
            cfg.default = stock_account.name
        elif cfg.accounts:
            cfg.default = next(iter(cfg.accounts))
    _save(cfg)
    typer.echo(f"{'Created' if was_new else 'Updated'} {display_path(str(cfg.path))}")
    typer.echo()
    code = _run_doctor(cfg, auth=False)
    _print_shell_hint()
    raise typer.Exit(code)


@app.command()
def add(
    name: str = typer.Argument(..., help="Account name, e.g. work or personal."),
    config_dir: Optional[str] = typer.Option(None, "--config-dir", "-d", help="Config dir (default: ~/.claude-NAME)."),
    seed_from: Optional[str] = typer.Option(None, "--seed-from", help="Copy settings, keybindings, CLAUDE.md, skills, agents, commands and output styles from this account."),
    default: bool = typer.Option(False, "--default", help="Make this the default account."),
    arg: Optional[list[str]] = typer.Option(None, "--arg", help="Default claude argument for this account (repeatable)."),
    env: Optional[list[str]] = typer.Option(None, "--env", help="KEY=VALUE exported when this account runs (repeatable)."),
) -> None:
    """Add an account: create (or register an existing) Claude config dir."""
    cfg = _load()
    _check_name(cfg, name)
    try:
        path = canonical_dir(config_dir or f"~/.claude-{name}", what="--config-dir")
    except ConfigError as exc:
        _fail(str(exc))
    other = _account_for_dir(cfg, path)
    if other is not None:
        _fail(f"{display_path(path)} is already registered as account {other.name!r}")
    env_map = _parse_env(env)
    seed_source = _require_account(cfg, seed_from) if seed_from else None

    try:
        created = accounts.create_config_dir(path)
    except OSError as exc:
        _fail(f"cannot create {path}: {exc}")
    account = Account(name=name, config_dir=path, args=list(arg or []), env=env_map)
    cfg.accounts[name] = account
    if default or not cfg.default:
        cfg.default = name
    _save(cfg)

    if created:
        typer.echo(f"Created {display_path(path)} and registered it as '{name}'")
    else:
        typer.echo(f"Registered existing {display_path(path)} as '{name}'{_who(path)}")
    if seed_source is not None:
        copied = accounts.seed_dir(seed_source.config_dir, path)
        typer.echo(f"Seeded from '{seed_source.name}': {', '.join(copied) if copied else 'nothing to copy'}")
    if cfg.default == name:
        typer.echo(f"'{name}' is the default account")
    if not _who(path):
        typer.echo(f"Next: `configure-route login {name}` to sign in to this account")


@app.command()
def remove(
    name: str,
    keep_dir: bool = typer.Option(False, "--keep-dir", help="Unregister only; leave the config dir on disk."),
    keep_keychain: bool = typer.Option(False, "--keep-keychain", help="Leave the account's macOS Keychain credentials in place."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
) -> None:
    """Remove an account: drop it and its routes from the config and delete its config dir."""
    cfg = _load()
    account = _require_account(cfg, name)
    routes_using = [route for route in cfg.routes if route.account == name]
    refusal = None if keep_dir else accounts.deletion_refusal(account.config_dir)
    delete_dir = not keep_dir and refusal is None and os.path.isdir(account.config_dir)
    if refusal:
        _warn(f"not deleting {display_path(account.config_dir)}: {refusal}")
    service = accounts.keychain_service(account.config_dir)
    delete_keychain = bool(
        not keep_keychain and not is_stock_dir(account.config_dir) and accounts.keychain_item_exists(service)
    )

    if not yes:
        typer.echo(f"This will remove account '{name}' from {display_path(str(cfg.path))}")
        if routes_using:
            typer.echo(f"  - and {len(routes_using)} route(s) pointing at it: " + ", ".join(r.match for r in routes_using))
        if delete_dir:
            typer.echo(f"  - and DELETE the directory {display_path(account.config_dir)}{_who(account.config_dir)}")
        if delete_keychain:
            typer.echo(f"  - and delete its macOS Keychain credentials ({service})")
        if not typer.confirm("Proceed?", default=False):
            raise typer.Abort()

    cfg.routes = [route for route in cfg.routes if route.account != name]
    for index, route in enumerate(cfg.routes):
        route.index = index
    del cfg.accounts[name]
    if cfg.default == name:
        cfg.default = None
        _warn("that was the default account; set a new one with `configure-route set-default NAME`")
    _save(cfg)
    typer.echo(f"Removed account '{name}'" + (f" and {len(routes_using)} route(s)" if routes_using else ""))
    if delete_dir:
        shutil.rmtree(account.config_dir)
        typer.echo(f"Deleted {display_path(account.config_dir)}")
    if delete_keychain:
        if accounts.keychain_delete(service):
            typer.echo("Deleted its Keychain credentials")
        else:
            _warn(f"could not delete Keychain item {service!r}")


@app.command("list")
def list_(
    fast: bool = typer.Option(False, "--fast", help="Skip `claude auth status`; read the state files instead."),
) -> None:
    """Show the accounts, their login state and the routes."""
    cfg = _load()
    if not cfg.exists:
        _fail(f"no config at {display_path(str(cfg.path))}; run `configure-route init` first")
    claude = None if fast else detect.find_claude(cfg.claude_path)

    def describe(account: Account) -> list[object]:
        name = account.name + (" *" if cfg.default == account.name else "")
        directory = display_path(account.config_dir)
        if not os.path.isdir(account.config_dir):
            return [name, directory, "directory missing", "-"]
        status: str | None = None
        if claude:
            auth = accounts.auth_status(claude, cfg, account)
            if auth is not None:
                if auth.get("loggedIn"):
                    status = f"{auth.get('email')} ({auth.get('orgName')}, {auth.get('subscriptionType')})"
                else:
                    status = "not logged in"
        if status is None:
            identity = accounts.login_identity(account.config_dir)
            status = f"{identity.email} ({identity.organization})" if identity and identity.email else "not logged in"
        present = accounts.keychain_item_exists(accounts.keychain_service(account.config_dir))
        keychain = "-" if present is None else ("yes" if present else "no")
        return [name, directory, status, keychain]

    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(describe, cfg.accounts.values()))
    if rows:
        typer.echo(_table(["ACCOUNT", "CONFIG DIR", "LOGIN", "KEYCHAIN"], rows))
        typer.echo("(* = default account)")
    else:
        typer.echo("No accounts. Run `configure-route init` or `configure-route add NAME`.")
    typer.echo()
    typer.echo(_routes_table(cfg))
    if cfg.args:
        typer.echo()
        typer.echo(f"Default claude args: {shlex.join(cfg.args)}")


@app.command("set-default")
def set_default(name: str) -> None:
    """Choose the account used when no route matches."""
    cfg = _load()
    _require_account(cfg, name)
    cfg.default = name
    _save(cfg)
    typer.echo(f"Default account: {name}")


@app.command("args")
def args_(
    values: Optional[list[str]] = typer.Argument(None, help="Claude arguments, after `--`, e.g. `-- --effort max`."),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Set the per-account args instead of the global ones."),
    clear: bool = typer.Option(False, "--clear", help="Remove the configured args."),
) -> None:
    """Show or set the default claude arguments (global, or per account with --account)."""
    cfg = _load()
    target = _require_account(cfg, account) if account else cfg
    label = f"account '{account}'" if account else "global"
    if clear:
        target.args = []
        _save(cfg)
        typer.echo(f"Cleared {label} args")
        return
    if not values:
        typer.echo(f"global: {shlex.join(cfg.args) or '(none)'}")
        for acct in cfg.accounts.values():
            typer.echo(f"{acct.name}: {shlex.join(acct.args) or '(none)'}")
        return
    target.args = list(values)
    _save(cfg)
    typer.echo(f"Set {label} args: {shlex.join(values)}")


@route_app.command("add")
def route_add(
    match: str = typer.Argument(..., help="Directory (this folder and everything below; relative to the cwd is fine), a glob, or re:REGEX."),
    account: str = typer.Argument(..., help="Account to use there."),
) -> None:
    """Add a route (or update the account of an existing one)."""
    cfg = _load()
    _require_account(cfg, account)
    stored = _absolute_match(match)
    try:
        route = parse_route({"match": stored, "account": account}, len(cfg.routes))
    except ConfigError as exc:
        _fail(str(exc))
    for existing in cfg.routes:
        if existing.match == stored or (existing.kind, existing.pattern) == (route.kind, route.pattern):
            if existing.account == account:
                typer.echo(f"Route {existing.match} -> {account} already exists")
                return
            existing.account = account
            _save(cfg)
            typer.echo(f"Updated route {existing.match} -> {account}")
            return
    cfg.routes.append(route)
    _save(cfg)
    typer.echo(f"Added route {stored} -> {account}  ({route.kind}, specificity {route.specificity})")
    if route.kind == "path" and not os.path.isdir(route.pattern):
        _warn(f"directory {display_path(route.pattern)} does not exist (yet)")


@route_app.command("remove")
def route_remove(target: str = typer.Argument(..., help="Route number (see `route list`) or its match string.")) -> None:
    """Remove a route by number or match string."""
    cfg = _load()
    index: int | None = None
    if target.isdigit():
        number = int(target)
        if 1 <= number <= len(cfg.routes):
            index = number - 1
    else:
        wanted = _absolute_match(target)
        for position, route in enumerate(cfg.routes):
            if route.match in (target, wanted) or _absolute_match(route.match) == wanted:
                index = position
                break
    if index is None:
        _fail(f"no route {target!r}; see `configure-route route list`")
    removed = cfg.routes.pop(index)
    for position, route in enumerate(cfg.routes):
        route.index = position
    _save(cfg)
    typer.echo(f"Removed route {removed.match} -> {removed.account}")


@route_app.command("list")
def route_list() -> None:
    """List the routes in file order with their kind and specificity."""
    cfg = _load()
    typer.echo(_routes_table(cfg))


@app.command()
def which(
    path: Optional[str] = typer.Argument(None, help="Directory to test (default: the current directory)."),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show which account route-claude would use for a directory, and why."""
    cfg = _load()
    for problem in validate(cfg):
        if problem.level == "error":
            _warn(problem.message)
    cwds = resolve.candidate_cwds(path) if path else resolve.candidate_cwds()
    try:
        decision = resolve.resolve_account(cfg, cwds, os.environ.get(resolve.ACCOUNT_ENV) or None)
    except resolve.ResolveError as exc:
        _fail(str(exc), 2)
    claude = detect.find_claude(cfg.claude_path) or "claude"
    env = resolve.build_env(cfg, decision.account, os.environ)
    argv = resolve.build_argv(claude, cfg, decision.account, [])

    if as_json:
        payload = dry_run_payload(decision, claude, argv, env)
        payload["matches"] = [
            {
                "match": item.route.match,
                "account": item.route.account,
                "kind": item.route.kind,
                "specificity": item.route.specificity,
                "matched_dir": item.matched_dir,
            }
            for item in decision.matches
        ]
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"Directory:   {display_path(decision.cwd)}")
    if decision.account is None:
        suffix = "" if cfg.exists else f"  [no config at {display_path(str(cfg.path))}]"
        typer.echo(f"Account:     none -> plain `claude`, environment unchanged{suffix}")
    else:
        how = {
            "override": f"forced by {resolve.ACCOUNT_ENV}",
            "route": f"route {decision.route.match}" if decision.route else "route",
            "default": "default account",
        }.get(decision.reason, decision.reason)
        typer.echo(f"Account:     {decision.account.name} ({display_path(decision.account.config_dir)})  [{how}]")
    if decision.matches:
        typer.echo("Matching routes, best first:")
        for item in decision.matches:
            marker = "*" if item.route is decision.route else " "
            typer.echo(
                f"  {marker} {item.route.match} -> {item.route.account}  "
                f"({item.route.kind}, specificity {item.route.specificity}, matched {display_path(item.matched_dir)})"
            )
    changes = resolve.env_changes(os.environ, env)
    rendered = ", ".join(f"{key}={value}" if value is not None else f"{key} (unset)" for key, value in changes.items())
    typer.echo(f"Environment: {rendered or 'unchanged'}")
    typer.echo(f"Command:     {shlex.join(argv)}")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(ctx: typer.Context, name: str = typer.Argument(..., help="Account to use.")) -> None:
    """Launch claude with a specific account, bypassing the routes. Extra arguments go to claude."""
    cfg = _load()
    account = _require_account(cfg, name)
    _exec_account(cfg, account, list(ctx.args))


@app.command()
def login(
    name: str = typer.Argument(..., help="Account to sign in to."),
    email: Optional[str] = typer.Option(None, "--email", help="Pre-fill the email address on the login page."),
    sso: bool = typer.Option(False, "--sso", help="Force the SSO login flow."),
    console: bool = typer.Option(False, "--console", help="Use an Anthropic Console (API billing) account."),
) -> None:
    """Sign in to an account (`claude auth login` under its config dir)."""
    cfg = _load()
    account = _require_account(cfg, name)
    try:
        if accounts.create_config_dir(account.config_dir):
            typer.echo(f"Created {display_path(account.config_dir)}")
    except OSError as exc:
        _fail(f"cannot create {account.config_dir}: {exc}")
    args = ["auth", "login"]
    if email:
        args += ["--email", email]
    if sso:
        args.append("--sso")
    if console:
        args.append("--console")
    _exec_account(cfg, account, args, include_defaults=False)


@app.command()
def logout(name: str = typer.Argument(..., help="Account to sign out of.")) -> None:
    """Sign out of an account (`claude auth logout` under its config dir)."""
    cfg = _load()
    account = _require_account(cfg, name)
    if not os.path.isdir(account.config_dir):
        _fail(f"{display_path(account.config_dir)} does not exist; nothing to sign out of")
    _exec_account(cfg, account, ["auth", "logout"], include_defaults=False)


@app.command()
def doctor(
    auth: bool = typer.Option(False, "--auth", help="Also run `claude auth status` for every account."),
) -> None:
    """Find the claude binary, record it, and check accounts, routes and the environment."""
    cfg = _load()
    raise typer.Exit(_run_doctor(cfg, auth=auth))


def _run_doctor(cfg: Config, *, auth: bool) -> int:
    counts = {"error": 0, "warning": 0}

    def report(level: str, message: str) -> None:
        if level in counts:
            counts[level] += 1
        prefix, colour = {"ok": ("  ok   ", GREEN), "warning": ("  warn ", YELLOW), "error": ("  FAIL ", RED)}.get(
            level, ("       ", None)
        )
        typer.secho(prefix + message, fg=colour)

    typer.echo("Claude binary")
    candidates = detect.all_candidates(cfg.claude_path)
    selected = detect.find_claude(cfg.claude_path)
    shown = 0
    for candidate in candidates:
        if not candidate.exists:
            continue
        shown += 1
        label = f"{candidate.path} [{candidate.source}]"
        if candidate.version:
            label += f"  {candidate.version}"
        label += f"  ({detect.install_method(candidate)})"
        if candidate.target:
            label += f" -> {candidate.target}"
        if candidate.path == selected:
            report("ok", label + "  <- selected")
        elif candidate.broken:
            report("warning", label + "  broken symlink")
        elif candidate.is_self:
            report("warning", label + "  this is route-claude itself, ignored")
        elif not candidate.executable:
            report("warning", label + "  not executable")
        else:
            report("info", label)
    if shown == 0:
        report("info", "no claude binary found in the usual locations")
    if selected is None:
        report("error", "no usable `claude` binary; install Claude Code (https://code.claude.com/docs/en/setup)")
    elif cfg.claude_path != selected:
        cfg.claude_path = selected
        if cfg.exists:
            _save(cfg)
            report("ok", f"recorded claude_path = {selected}")
    if cfg.claude_path and selected and cfg.claude_path != selected:
        report("warning", f"configured claude_path {cfg.claude_path} is not usable; using {selected}")

    typer.echo()
    typer.echo("Accounts")
    if not cfg.exists:
        report("warning", f"no config at {display_path(str(cfg.path))}; run `configure-route init`")
    elif not cfg.accounts:
        report("warning", "no accounts configured; run `configure-route init` or `configure-route add NAME`")
    for account in cfg.accounts.values():
        tag = f"{account.name} ({display_path(account.config_dir)})" + ("  [default]" if cfg.default == account.name else "")
        if not os.path.isdir(account.config_dir):
            report("warning", f"{tag}: directory missing; `configure-route login {account.name}` creates it and signs in")
            continue
        notes: list[str] = []
        level = "ok"
        identity = accounts.login_identity(account.config_dir)
        if identity and identity.email:
            notes.append(f"state file: {identity.email}")
        else:
            notes.append("state file: no login recorded")
            level = "warning"
        service = accounts.keychain_service(account.config_dir)
        present = accounts.keychain_item_exists(service)
        if present is True:
            notes.append(f"Keychain item {service!r} present")
        elif present is False:
            notes.append(f"no Keychain item {service!r}")
            level = "warning"
        if auth and selected:
            status = accounts.auth_status(selected, cfg, account)
            if status is None:
                notes.append("auth status: failed to run")
                level = "warning"
            elif status.get("loggedIn"):
                notes.append(
                    f"auth status: logged in as {status.get('email')} ({status.get('orgName')}, {status.get('subscriptionType')})"
                )
                level = "ok"
            else:
                notes.append("auth status: NOT logged in")
                level = "error"
        if level != "ok":
            notes.append(f"sign in with `configure-route login {account.name}`")
        report(level, f"{tag}: " + "; ".join(notes))

    typer.echo()
    typer.echo("Routes")
    if cfg.exists and not cfg.routes:
        report("info", "no routes: every directory uses the default account")
    elif cfg.routes:
        report("info", f"{len(cfg.routes)} route(s); run `configure-route route list` to see them")
    if cfg.exists and not cfg.default and cfg.accounts:
        report("warning", "no default account; unrouted directories launch plain `claude` (`configure-route set-default NAME`)")
    for problem in validate(cfg):
        report(problem.level, problem.message)

    typer.echo()
    typer.echo("Environment")
    if os.environ.get(resolve.CONFIG_DIR_ENV):
        report(
            "warning",
            f"{resolve.CONFIG_DIR_ENV}={os.environ[resolve.CONFIG_DIR_ENV]} is exported in this shell; "
            "route-claude overrides it, but plain `claude` will use it",
        )
    exported = [name for name in resolve.AUTH_OVERRIDE_VARS if os.environ.get(name)]
    if exported:
        report(
            "warning",
            f"{', '.join(exported)} exported: these override the login of every account "
            "(add them to `unset_env` in the config if unintended)",
        )
    if shutil.which("route-claude"):
        report("ok", f"route-claude on PATH: {shutil.which('route-claude')}")
    else:
        report("warning", "route-claude is not on PATH (install with `uv tool install .`; uv puts it in ~/.local/bin)")
    for path, number, line in shell_alias_lines():
        if "route-claude" in line:
            report("ok", f"{display_path(path)}:{number} aliases claude to route-claude")
        else:
            report("info", f"{display_path(path)}:{number} has `{line}` (see the alias hint from `configure-route init`)")

    typer.echo()
    summary = f"{counts['error']} error(s), {counts['warning']} warning(s)"
    typer.secho(summary, fg=RED if counts["error"] else (YELLOW if counts["warning"] else GREEN))
    return 1 if counts["error"] else 0


@app.command()
def edit() -> None:
    """Open the config file in $VISUAL / $EDITOR and validate it afterwards."""
    cfg = _load()
    if not cfg.exists:
        _save(cfg)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    code = subprocess.call([*shlex.split(editor), str(cfg.path)])
    if code != 0:
        _fail(f"editor exited with status {code}", code)
    try:
        cfg = load_config()
    except ConfigError as exc:
        _fail(str(exc), 2)
    problems = validate(cfg)
    for problem in problems:
        (_warn if problem.level == "warning" else lambda m: typer.secho(f"error: {m}", fg=RED, err=True))(problem.message)
    if not any(problem.level == "error" for problem in problems):
        typer.echo(f"{display_path(str(cfg.path))} is valid")
    else:
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
