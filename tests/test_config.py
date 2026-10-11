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
    is_absolute,
    is_stock_dir,
    load_config,
    normalise_match,
    parse_route,
    portable_path,
    save_config,
    split_command,
    stock_config_dir,
    validate,
)
from helpers import IS_WINDOWS, ROOT, fwd, posix_only, symlink_or_skip, windows_only


def comps(path) -> int:
    return len([part for part in re.split(r"[\\/]+" if IS_WINDOWS else r"/+", str(path)) if part])


def test_round_trip(home):
    cfg = load_config()
    assert not cfg.exists
    claude = os.path.join(ROOT, "usr", "local", "bin", "claude")
    outside = os.path.join(ROOT, "claude-accounts", "ext")
    cfg.claude_path = claude
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
    cfg.accounts["ext"] = Account(name="ext", config_dir=outside)
    cfg.routes = [parse_route({"match": "~/dev", "account": "personal"}, 0)]
    cfg.extra = {"custom": 42}
    save_config(cfg)

    raw = yaml.safe_load(cfg.path.read_text())
    assert raw["accounts"]["personal"]["config_dir"] == "~/.claude-personal"  # stored with ~ and /
    assert raw["accounts"]["ext"]["config_dir"] == fwd(outside)
    assert b"\r\n" not in cfg.path.read_bytes()

    again = load_config()
    assert again.exists
    assert again.claude_path == claude
    assert again.default == "work"
    assert again.args == ["--effort", "max"]
    assert again.unset_env == ["ANTHROPIC_API_KEY"]
    assert again.announce == "never"
    personal = again.accounts["personal"]
    assert personal.config_dir == str(home / ".claude-personal")
    assert personal.args == ["--model", "opus"]
    assert personal.env == {"FOO": "1"}
    assert personal.unset_env == ["BAR"]
    assert again.accounts["ext"].config_dir == outside
    assert [route.match for route in again.routes] == ["~/dev"]
    assert again.extra == {"custom": 42}


@posix_only
def test_config_file_permissions(home):
    cfg = load_config()
    save_config(cfg)
    assert stat.S_IMODE(os.stat(cfg.path).st_mode) == 0o600


def test_path_helpers(home):
    h = str(home)
    assert stock_config_dir() == os.path.join(h, ".claude")
    assert is_stock_dir(h + "/.claude/")
    assert expand_path(" ~/x/ ") == os.path.join(h, "x")
    assert expand_path("/") == os.path.normpath("/")
    assert canonical_dir("~/.claude-personal/") == os.path.join(h, ".claude-personal")
    assert canonical_dir(os.path.join(ROOT, "a", "b", "..", "c")) == os.path.join(ROOT, "a", "c")
    with pytest.raises(ConfigError):
        canonical_dir("relative/dir")
    assert display_path(os.path.join(h, "dev", "x")) == "~/dev/x"
    assert display_path(h) == "~"
    outside = os.path.join(ROOT, "opt", "x")
    assert display_path(outside) == outside
    assert portable_path(outside) == fwd(outside)
    assert is_absolute(ROOT) and not is_absolute("relative")


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


@windows_only
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("C:\\dev\\work\\*", "C:\\dev\\work"),
        ("C:\\dev\\work\\", "C:\\dev\\work"),
        ("C:\\dev\\work\\**\\", "C:\\dev\\work"),
        ("C:\\*", "C:\\"),
        ("C:\\", "C:\\"),
        ("C:/dev/*", "C:/dev"),
    ],
)
def test_normalise_match_windows(raw, expected):
    assert normalise_match(raw) == expected


def test_parse_route_kinds(home):
    h = str(home)
    base = comps(h)

    route = parse_route({"match": "~/dev/work/", "account": "a"}, 0)
    assert (route.kind, route.pattern, route.specificity) == ("path", os.path.join(h, "dev", "work"), base + 2)
    assert parse_route({"match": "~/dev/work/*", "account": "a"}, 1).kind == "path"
    assert parse_route({"match": "~/dev/work/**", "account": "a"}, 2).pattern == os.path.join(h, "dev", "work")

    route = parse_route({"match": "~/dev/*-corp", "account": "a"}, 3)
    assert (route.kind, route.specificity) == ("glob", base + 1)
    route = parse_route({"match": "~/dev/*/*", "account": "a"}, 4)
    assert (route.kind, route.pattern) == ("glob", fwd(h) + "/dev/*")

    route = parse_route({"match": "re:^/x/y/.*corp$", "account": "a"}, 5)
    assert (route.kind, route.specificity, route.pattern) == ("regex", 2, "^/x/y/.*corp$")
    assert parse_route({"match": "re:-corp$", "account": "a"}, 6).specificity == 0
    route = parse_route({"match": "re:^~/clients/acme", "account": "a"}, 7)
    assert route.pattern == "^" + re.escape(fwd(h)) + "/clients/acme"
    assert route.specificity == base + 2
    assert route.matcher().search(fwd(h) + "/clients/acme/x")


def test_parse_route_symlink_realpath(home, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    symlink_or_skip(link, real, directory=True)
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


def test_split_command():
    assert split_command("--effort max") == ["--effort", "max"]
    assert split_command('--append-system-prompt "be brief"') == ["--append-system-prompt", "be brief"]
    if IS_WINDOWS:
        assert split_command('"C:\\Program Files\\x\\ed.exe" --wait') == ["C:\\Program Files\\x\\ed.exe", "--wait"]
        assert split_command("--add-dir C:\\work") == ["--add-dir", "C:\\work"]


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


def test_validate(home):
    (home / ".claude-x").mkdir()
    cfg = from_dict(
        {
            "default": "nobody",
            "accounts": {"x": "~/.claude-x", "y": str(home) + "/./.claude-x/", "missing": "~/.claude-missing"},
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


@windows_only
def test_validate_windows_is_case_insensitive(home):
    (home / ".claude-x").mkdir()
    cfg = from_dict(
        {
            "accounts": {"x": "~/.claude-x", "y": "~/.CLAUDE-X"},
            "routes": [{"match": "~/Dev", "account": "x"}, {"match": "~/dev", "account": "y"}],
        },
        home / "c.yaml",
        exists=True,
    )
    messages = [p.message for p in validate(cfg)]
    assert any("same directory" in m for m in messages)
    assert any("will never win" in m for m in messages)
