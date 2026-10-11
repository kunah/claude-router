from __future__ import annotations

import os

import pytest

from claude_router.config import from_dict
from claude_router.resolve import (
    ACCOUNT_ENV,
    CONFIG_DIR_ENV,
    ResolveError,
    ancestors,
    build_argv,
    build_env,
    candidate_cwds,
    env_changes,
    resolve_account,
)
from helpers import ROOT, symlink_or_skip, windows_only


def mk(home, routes, default="work", **extra):
    data = {
        "default": default,
        "accounts": {"work": "~/.claude", "personal": "~/.claude-personal", "acme": "~/.claude-acme"},
        "routes": routes,
    }
    data.update(extra)
    return from_dict(data, home / ".claude-router.yaml", exists=True)


def pick(cfg, path, override=None):
    return resolve_account(cfg, candidate_cwds(str(path)), override)


def test_ancestors():
    assert ancestors("/a/b/c") == ["/a/b/c", "/a/b", "/a", "/"]
    assert ancestors("/") == ["/"]
    chain = ancestors(os.path.join(ROOT, "a", "b"))
    assert chain[-1] == ROOT and len(chain) == 3


def test_path_prefix_boundary(home):
    cfg = mk(home, [{"match": "~/dev/work", "account": "personal"}])
    assert pick(cfg, home / "dev" / "work").account.name == "personal"
    assert pick(cfg, home / "dev" / "work" / "x" / "y").account.name == "personal"
    assert pick(cfg, home / "dev" / "workshop").account.name == "work"
    assert pick(cfg, home / "dev").account.name == "work"
    decision = pick(cfg, home / "dev" / "work" / "x")
    assert decision.reason == "route" and decision.route.match == "~/dev/work"


def test_glob_and_regex_match_ancestors(home):
    cfg = mk(
        home,
        [
            {"match": "~/dev/*-corp", "account": "acme"},
            {"match": "re:/clients/acme(/|$)", "account": "acme"},
            {"match": "~/clients/*/oss", "account": "personal"},
        ],
    )
    assert pick(cfg, home / "dev" / "foo-corp" / "src" / "deep").account.name == "acme"
    assert pick(cfg, home / "dev" / "foo" / "src").account.name == "work"
    assert pick(cfg, home / "clients" / "acme" / "repo").account.name == "acme"
    assert pick(cfg, home / "clients" / "acme-old" / "repo").account.name == "work"
    assert pick(cfg, home / "clients" / "bob" / "oss" / "thing").account.name == "personal"


def test_deepest_specificity_wins_regardless_of_order(home):
    routes = [
        {"match": "~/dev", "account": "personal"},
        {"match": "~/dev/work", "account": "work"},
        {"match": "~/dev/work/oss-contrib", "account": "personal"},
    ]
    for order in (routes, list(reversed(routes))):
        cfg = mk(home, order, default="acme")
        assert pick(cfg, home / "dev" / "x").account.name == "personal"
        assert pick(cfg, home / "dev" / "work" / "x").account.name == "work"
        assert pick(cfg, home / "dev" / "work" / "oss-contrib" / "x").account.name == "personal"
        assert pick(cfg, home / "elsewhere").account.name == "acme"


def test_ties_go_to_file_order_and_matches_listed(home):
    cfg = mk(home, [{"match": "re:corp", "account": "acme"}, {"match": "re:acme", "account": "personal"}])
    decision = pick(cfg, home / "dev" / "acme-corp")
    assert decision.account.name == "acme"
    assert [m.route.account for m in decision.matches] == ["acme", "personal"]
    # a more specific path beats both regexes
    cfg = mk(
        home,
        [
            {"match": "re:corp", "account": "acme"},
            {"match": "~/dev/acme-corp", "account": "work"},
        ],
    )
    assert pick(cfg, home / "dev" / "acme-corp" / "x").account.name == "work"


def test_symlinked_rule_and_cwd(home, tmp_path):
    real = tmp_path / "real" / "proj"
    real.mkdir(parents=True)
    sub = real / "sub"
    sub.mkdir()
    link = tmp_path / "link"
    symlink_or_skip(link, tmp_path / "real", directory=True)

    # rule written through the symlink, cwd physical
    cfg = mk(home, [{"match": str(link / "proj"), "account": "personal"}])
    decision = resolve_account(cfg, candidate_cwds(cwd=str(sub), environ={}))
    assert decision.account.name == "personal"

    # rule physical, shell cwd logical ($PWD through the symlink)
    cfg = mk(home, [{"match": str(real), "account": "personal"}])
    logical = str(link / "proj" / "sub")
    cwds = candidate_cwds(cwd=str(sub), environ={"PWD": logical})
    assert cwds[:2] == [logical, str(sub)]
    decision = resolve_account(cfg, cwds)
    assert decision.account.name == "personal" and decision.cwd == logical

    # explicit path candidates include the realpath
    assert candidate_cwds(str(link / "proj")) == [str(link / "proj"), os.path.realpath(str(real))]


def test_candidate_cwds_ignores_bogus_pwd(home, tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    assert candidate_cwds(cwd=str(d), environ={"PWD": os.path.join(ROOT, "nonexistent")}) == [str(d)]
    assert candidate_cwds(cwd=str(d), environ={"PWD": "relative"}) == [str(d)]
    assert candidate_cwds(cwd=str(d), environ={"PWD": "/c/Users/git-bash-style"}) == [str(d)]


def test_override_default_and_none(home):
    cfg = mk(home, [{"match": "~/dev", "account": "personal"}])
    assert pick(cfg, home / "dev" / "x", override="acme").reason == "override"
    with pytest.raises(ResolveError, match="CLAUDE_ROUTER_ACCOUNT"):
        pick(cfg, home / "dev" / "x", override="ghost")
    assert pick(cfg, home / "other").reason == "default"
    cfg = mk(home, [], default=None)
    decision = pick(cfg, home / "other")
    assert decision.account is None and decision.reason == "none"
    cfg = mk(home, [{"match": "~/dev", "account": "ghost"}])
    with pytest.raises(ResolveError, match="ghost"):
        pick(cfg, home / "dev")


def test_build_env(home):
    cfg = mk(home, [], unset_env=["GLOBAL_SECRET"])
    cfg.accounts["personal"].env = {"CLAUDE_CODE_USE_BEDROCK": "1"}
    cfg.accounts["personal"].unset_env = ["ANTHROPIC_API_KEY"]
    base = {
        CONFIG_DIR_ENV: "/elsewhere",
        "GLOBAL_SECRET": "x",
        "ANTHROPIC_API_KEY": "k",
        "PATH": "/bin",
    }

    env = build_env(cfg, cfg.accounts["work"], base)
    assert CONFIG_DIR_ENV not in env  # stock dir -> unset
    assert env[ACCOUNT_ENV] == "work" and "GLOBAL_SECRET" not in env and env["ANTHROPIC_API_KEY"] == "k"

    env = build_env(cfg, cfg.accounts["personal"], base)
    assert env[CONFIG_DIR_ENV] == str(home / ".claude-personal")
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and "ANTHROPIC_API_KEY" not in env and env["PATH"] == "/bin"
    assert env_changes(base, env) == {
        "ANTHROPIC_API_KEY": None,
        CONFIG_DIR_ENV: str(home / ".claude-personal"),
        "CLAUDE_CODE_USE_BEDROCK": "1",
        ACCOUNT_ENV: "personal",
        "GLOBAL_SECRET": None,
    }

    assert build_env(cfg, None, base) == base


def test_build_argv(home):
    cfg = mk(home, [], args=["--effort", "max"])
    cfg.accounts["personal"].args = ["--model", "opus"]
    assert build_argv("/c", cfg, cfg.accounts["personal"], ["-p", "hi"]) == [
        "/c", "--effort", "max", "--model", "opus", "-p", "hi",
    ]
    assert build_argv("/c", cfg, None, ["--version"]) == ["/c", "--effort", "max", "--version"]
    assert build_argv("/c", cfg, cfg.accounts["personal"], ["auth"], include_defaults=False) == ["/c", "auth"]


@windows_only
def test_windows_matching_is_case_insensitive(home):
    cfg = mk(
        home,
        [
            {"match": str(home / "Dev" / "Work") + "\\*", "account": "personal"},
            {"match": "~/clients/*-corp", "account": "acme"},
            {"match": "re:^~/regex/(a|b)$", "account": "acme"},
        ],
    )
    assert cfg.routes[0].kind == "path"
    assert pick(cfg, str(home / "dev" / "work" / "x").upper()).account.name == "personal"
    assert pick(cfg, home / "CLIENTS" / "Big-CORP" / "src").account.name == "acme"
    assert pick(cfg, home / "regex" / "A").account.name == "acme"
    assert pick(cfg, home / "regex" / "c").account.name == "work"


@windows_only
def test_windows_env_names_are_case_insensitive(home):
    cfg = mk(home, [], unset_env=["anthropic_api_key"])
    cfg.accounts["personal"].env = {"Path": "C:\\only"}
    env = build_env(cfg, cfg.accounts["personal"], {"ANTHROPIC_API_KEY": "k", "PATH": "C:\\Windows"})
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "C:\\only" and "Path" not in env
