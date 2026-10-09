# claude-router

Run several Claude Code accounts (work, personal, a client's org, ...) on one machine and let the
**current directory** decide which one to use. No more logging in and out.

Claude Code keeps everything about an account (login, settings, sessions, plugins) in a config
directory, selected with the `CLAUDE_CONFIG_DIR` environment variable. `claude-router` gives each
account its own directory and launches `claude` with the right one.

Two commands:

| Command | Purpose |
|---|---|
| `route-claude [claude args...]` | Pick the account for the current directory and start Claude Code. **Every argument is forwarded to `claude` untouched** (`route-claude -p "hi"`, `route-claude --resume`, `route-claude mcp list`, ...). |
| `configure-route ...` | Manage accounts, routes, defaults and find the `claude` binary. |

Configuration lives in `~/.claude-router.yaml`.

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone <this repo> ~/Developer/claude-router
cd ~/Developer/claude-router
uv tool install --editable .     # puts route-claude + configure-route in ~/.local/bin
```

`--editable` means edits to the source take effect immediately. Without it, re-run
`uv tool install . --reinstall` after pulling changes.

## Quick start

```sh
# 1. Register the config dirs already on disk (~/.claude and any ~/.claude-*), detect claude
configure-route init --name work

# 2. Add another account: creates ~/.claude-personal (mode 0700) and registers it
configure-route add personal
configure-route login personal          # browser login for that account only

# 3. Decide where each account is used
configure-route route add ~/Developer/oss personal     # this folder and everything below
configure-route set-default work                       # everywhere else

# 4. Flags you always pass to claude (optional)
configure-route args -- --effort max

# 5. Check
configure-route list
configure-route which ~/Developer/oss/some-repo
configure-route doctor --auth
```

Then make plain `claude` go through the router by adding this to `~/.zshrc` (or your shell's rc file):

```sh
alias claude='route-claude'
```

`configure-route` never edits shell files itself; `init` and `doctor` only print this hint.

## Commands

| Command | What it does |
|---|---|
| `init [--name NAME] [--yes]` | Create the config, register `~/.claude` (as NAME) and every `~/.claude-*` dir that looks like a Claude config dir, make the stock dir the default, run `doctor`. |
| `add NAME [--config-dir DIR] [--seed-from ACCOUNT] [--default] [--arg A]... [--env K=V]...` | Create `~/.claude-NAME` (or register an existing dir). `--seed-from` copies `settings.json`, `keybindings.json`, `CLAUDE.md`, `skills/`, `agents/`, `commands/`, `output-styles/` from another account. |
| `remove NAME [--keep-dir] [--keep-keychain] [-y]` | Unregister the account and its routes, delete its config dir (after confirmation) and, on macOS, its Keychain credentials. Never deletes the stock `~/.claude`, your home, or anything outside your home. |
| `list [--fast]` | Accounts with login state (via `claude auth status`), Keychain presence, default marker, then the routes. |
| `set-default NAME` | Account used when no route matches. |
| `args [-a ACCOUNT] [--clear] -- ARGS...` | Show or set the default `claude` arguments, globally or per account. |
| `route add MATCH ACCOUNT` / `route remove N\|MATCH` / `route list` | Manage routes. |
| `which [DIR] [--json]` | Explain which account a directory gets, which routes matched, and the exact environment and command `route-claude` would use. |
| `run NAME [claude args...]` | Launch claude with a specific account, ignoring the routes. |
| `login NAME [--email X] [--sso] [--console]` / `logout NAME` | `claude auth login` / `logout` inside that account's config dir. |
| `doctor [--auth]` | Find and record the `claude` binary, check every account (dir, login, Keychain), validate the routes, warn about environment variables that would interfere. |
| `edit` | Open the config in `$EDITOR` and validate it afterwards. |

## Configuration reference

`~/.claude-router.yaml` (override the location with `CLAUDE_ROUTER_CONFIG`):

```yaml
version: 1
claude_path: /Users/me/.local/bin/claude   # written by init/doctor; re-detected on the fly if stale
default: work                              # account when no route matches (optional)
args: ["--effort", "max"]                  # claude args added to every launch (optional)
unset_env: []                              # env vars removed before launch, e.g. ANTHROPIC_API_KEY (optional)
announce: auto                             # auto | always | never: "claude-router: work (~/.claude) ..." on stderr
accounts:
  work:
    config_dir: ~/.claude                  # the stock dir: router launches claude with CLAUDE_CONFIG_DIR unset
  personal:
    config_dir: ~/.claude-personal
    args: ["--model", "opus"]              # per-account args (optional)
    env:                                   # extra env for this account (optional)
      CLAUDE_CODE_USE_BEDROCK: "1"
    unset_env: []                          # (optional)
routes:
  - match: ~/Developer/work                # plain path: this folder and everything below
    account: work
  - match: "~/clients/*/oss"               # glob (fnmatch on the absolute path)
    account: personal
  - match: "re:^~/clients/acme(/|$)"       # regex (re.search); a leading ~ means your home dir
    account: acme
```

### How a directory is matched

- A route matches when its pattern matches the current directory **or any parent directory**, so
  every route means "this folder and everything below". `X/`, `X/*` and `X/**` are the same as `X`.
- Three kinds: a plain path, a glob (contains `*`, `?` or `[`), or a regex (prefix `re:`).
- When several routes match, the one with the **longest literal directory prefix** wins
  (`~/Developer/work/oss-contrib` beats `~/Developer/work`, whatever their order in the file).
  Ties go to the route listed first. `configure-route which` shows every matching route and its
  specificity.
- No match: the `default` account. No default either: plain `claude`, environment untouched.
- Symlinked paths work: both the shell's logical `$PWD` and the physical path are tried.

### What `route-claude` does

1. Loads the config (a broken config aborts with exit code 2 instead of launching the wrong account; a
   missing config just runs `claude`).
2. Picks the account: `CLAUDE_ROUTER_ACCOUNT=<name>` forces one; otherwise the routes, then the default.
3. Builds the environment: sets `CLAUDE_CONFIG_DIR` to the account's dir, or **unsets** it for the stock
   `~/.claude` (Claude Code keys its macOS Keychain entry on the raw value of this variable, so pointing
   it at `~/.claude` explicitly would look like a different, logged-out account). Removes the
   `unset_env` names, applies the account's `env`, and exports `CLAUDE_ROUTER_ACCOUNT` so anything started
   inside the session sticks to the same account.
4. Runs `claude <global args> <account args> <your args>` by replacing its own process (`exec`), so
   signals, the terminal and the exit code behave exactly as with `claude` itself.

Nothing is written to stdout, so `route-claude -p ... | jq` is safe. The one-line announcement goes to
stderr, and only when both stdout and stderr are terminals (`announce: auto`).

### Environment variables

| Variable | Effect |
|---|---|
| `CLAUDE_ROUTER_ACCOUNT=name` | Force an account for this launch (also exported to the session). |
| `CLAUDE_ROUTER_CONFIG=path` | Use another config file. |
| `CLAUDE_ROUTER_QUIET=1` | Suppress the announcement line. |
| `CLAUDE_ROUTER_DRY_RUN=1` | Print the decision, command and environment changes as JSON instead of launching. |

## Good to know

- Everything is per account because it lives in the config dir: sessions (`--continue` / `--resume`
  only see that account's sessions), history, plugins, auto-memory and per-project trust prompts.
- A new account dir needs its own browser login (`configure-route login NAME`). Credentials are never copied.
- On macOS, credentials live in the Keychain under `Claude Code-credentials` (stock dir) or
  `Claude Code-credentials-<sha256(dir)[:8]>` (other dirs). `remove` offers to delete the item;
  `doctor` reports whether it exists without reading it.
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_PROFILE` override
  any login. `doctor` warns when they are exported; list them under `unset_env` if that is unintended.
- Anthropic Console (API-billing) logins are stored outside the config dir and are not isolated per account.
- Claude Code's own `claude doctor` / `claude update` scan `~/.zshrc` for a `claude` alias and may
  mention `alias claude='route-claude'`. That is harmless.
- `claude update` keeps working: the router stores the stable launcher path (`~/.local/bin/claude`),
  not the versioned binary behind it.
- Comments in `~/.claude-router.yaml` are lost whenever `configure-route` rewrites the file.

## Development

```sh
uv sync
uv run pytest
```
