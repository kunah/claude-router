from __future__ import annotations

import os
import re
import stat

import pytest
import yaml

from claude_router.config import (
    Account,
    ConfigError,
    canonical_dir,
    display_path,
    expand_path,
    from_dict,
    is_stock_dir,
    load_config,
    normalise_match,
    parse_route,
    save_config,
    stock_config_dir,
    validate,
)


def comps(path: str) -> int:
    return len([part for part in path.split("/") if part])


def test_round_trip_and_permissions(home):
    cfg = load_config()
    assert not cfg.exists
    cfg.claude_path = "/usr/local/bin/claude"
    cfg.default = "work"
    cfg.args = ["--effort", "max"]
    cfg.unset_env = ["ANTHROPIC_API_KEY"]
    cfg.announce = "never"
    cfg.accounts["work"] = Account(name="work", config_dir=str(home / ".claude"))
    cfg.accounts["personal"] = Account(
        name="personal",
        config_dir=str(home / ".claude-personal"),
        args=["--model", "opus"],
        env={"FOO": "1"},
        unset_env=["BAR"],
    )
    cfg.routes = [parse_route({"match": "~/dev", "account": "personal"}, 0)]
    cfg.extra = {"custom": 42}
    save_config(cfg)

    assert stat.S_IMODE(os.stat(cfg.path).st_mode) == 0o600
    raw = yaml.safe_load(cfg.path.read_text())
    assert raw["accounts"]["personal"]["config_dir"] == "~/.claude-personal"  # stored with ~

    again = load_config()
    assert again.exists
    assert again.claude_path == "/usr/local/bin/claude"
    assert again.default == "work"
    assert again.args == ["--effort", "max"]
    assert again.unset_env == ["ANTHROPIC_API_KEY"]
    assert again.announce == "never"
    personal = again.accounts["personal"]
    assert personal.config_dir == str(home / ".claude-personal")
    assert personal.args == ["--model", "opus"]
    assert personal.env == {"FOO": "1"}
    assert personal.unset_env == ["BAR"]
    assert [route.match for route in again.routes] == ["~/dev"]
    assert again.extra == {"custom": 42}


def test_path_helpers(home):
    h = str(home)
    assert stock_config_dir() == f"{h}/.claude"
    assert is_stock_dir("~/.claude/".replace("~", h))
    assert expand_path(" ~/x/ ") == f"{h}/x"
    assert expand_path("/") == "/"
    assert canonical_dir("~/.claude-personal/") == f"{h}/.claude-personal"
    assert canonical_dir("/a/b/../c") == "/a/c"
    with pytest.raises(ConfigError):
        canonical_dir("relative/dir")
    assert display_path(f"{h}/dev") == "~/dev"
    assert display_path(h) == "~"
    assert display_path("/opt/x") == "/opt/x"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("~/dev/work", "~/dev/work"),
        ("~/dev/work/", "~/dev/work"),
        ("~/dev/work/*", "~/dev/work"),
        ("~/dev/work/**", "~/dev/work"),
        ("~/dev/work/**/", "~/dev/work"),
        ("~/dev/*/*", "~/dev/*"),
        ("re:^/x/*", "re:^/x/*"),
        ("/", "/"),
    ],
)
def test_normalise_match(raw, expected):
    assert normalise_match(raw) == expected


def test_parse_route_kinds(home):
    h = str(home)
    base = comps(h)

    route = parse_route({"match": "~/dev/work/", "account": "a"}, 0)
    assert (route.kind, route.pattern, route.specificity) == ("path", f"{h}/dev/work", base + 2)
    assert parse_route({"match": "~/dev/work/*", "account": "a"}, 1).kind == "path"
    assert parse_route({"match": "~/dev/work/**", "account": "a"}, 2).pattern == f"{h}/dev/work"

    route = parse_route({"match": "~/dev/*-corp", "account": "a"}, 3)
    assert (route.kind, route.specificity) == ("glob", base + 1)
    route = parse_route({"match": "~/dev/*/*", "account": "a"}, 4)
    assert (route.kind, route.pattern) == ("glob", f"{h}/dev/*")

    route = parse_route({"match": "re:^/x/y/.*corp$", "account": "a"}, 5)
    assert (route.kind, route.specificity, route.pattern) == ("regex", 2, "^/x/y/.*corp$")
    assert parse_route({"match": "re:-corp$", "account": "a"}, 6).specificity == 0
    route = parse_route({"match": "re:^~/clients/acme", "account": "a"}, 7)
    assert route.pattern == "^" + re.escape(h) + "/clients/acme"
    assert route.specificity == base + 2
    assert re.search(route.pattern, f"{h}/clients/acme/x")


def test_parse_route_symlink_realpath(home, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    route = parse_route({"match": str(link), "account": "a"}, 0)
    assert route.pattern == str(link)
    assert route.realpath == os.path.realpath(str(real))


@pytest.mark.parametrize(
    "raw",
    [
        {"match": "relative/path", "account": "a"},
        {"match": "re:(unclosed", "account": "a"},
        {"match": "re:", "account": "a"},
        {"match": "", "account": "a"},
        {"match": "~/x", "account": ""},
        {"match": "~/x"},
        "not-a-mapping",
    ],
)
def test_parse_route_errors(home, raw):
    with pytest.raises(ConfigError):
        parse_route(raw, 0)


def test_from_dict_leniency_and_errors(home):
    cfg = from_dict(
        {"accounts": {"work": "~/.claude"}, "args": "--effort max", "announce": True},
        home / "c.yaml",
        exists=True,
    )
    assert cfg.accounts["work"].config_dir == str(home / ".claude")
    assert cfg.args == ["--effort", "max"]
    assert cfg.announce == "always"
    assert from_dict(None, home / "c.yaml", exists=True).accounts == {}

    for bad in (
        ["not", "a", "mapping"],
        {"accounts": {"work": {"config_dir": "relative"}}},
        {"accounts": {"bad name!": {"config_dir": "~/x"}}},
        {"accounts": {"work": {}}},
        {"accounts": ["work"]},
        {"announce": "sometimes"},
        {"args": {"a": 1}},
        {"routes": {"match": "~/x"}},
        {"default": 3},
        {"claude_path": 3},
    ):
        with pytest.raises(ConfigError):
            from_dict(bad, home / "c.yaml", exists=True)


def test_load_config_errors(home, write_config):
    write_config("accounts: [\n")
    with pytest.raises(ConfigError):
        load_config()

    write_config(
        """
        accounts:
          work: {config_dir: ~/.claude}
        routes:
          - {match: ~/dev, account: ghost}
        """
    )
    cfg = load_config()  # non-strict: loads, validate() reports the problem
    assert [p.level for p in validate(cfg)].count("error") == 1
    with pytest.raises(ConfigError, match="ghost"):
        load_config(strict=True)


def test_validate(home, tmp_path):
    (home / ".claude-x").mkdir()
    link = home / "link-to-x"
    link.symlink_to(home / ".claude-x")
    cfg = from_dict(
        {
            "default": "nobody",
            "accounts": {"x": "~/.claude-x", "y": str(link), "missing": "~/.claude-missing"},
            "routes": [
                {"match": "~/nope", "account": "x"},
                {"match": "~/nope/", "account": "y"},
                {"match": "~/dev", "account": "ghost"},
            ],
        },
        home / "c.yaml",
        exists=True,
    )
    messages = {(p.level, p.message) for p in validate(cfg)}
    levels = [level for level, _ in messages]
    assert levels.count("error") == 3  # unknown default, duplicate dir, unknown route account
    assert any("same directory" in m for _, m in messages)
    assert any("ghost" in m for _, m in messages)
    assert any("will never win" in m and level == "warning" for level, m in messages)
    assert any("does not exist" in m and level == "warning" for level, m in messages)
