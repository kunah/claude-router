from __future__ import annotations

from claude_router import detect


def test_find_claude_order(fake_claude, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(detect, "known_locations", lambda: [])
    assert detect.find_claude() is None
    assert detect.find_claude(str(fake_claude)) == str(fake_claude)
    assert detect.find_claude("/nonexistent/claude") is None

    monkeypatch.setattr(detect, "known_locations", lambda: [(str(fake_claude), "test")])
    assert detect.find_claude("/nonexistent/claude") == str(fake_claude)

    monkeypatch.setattr(detect, "known_locations", lambda: [])
    monkeypatch.setenv("PATH", str(fake_claude.parent))
    assert detect.find_claude() == str(fake_claude)
    assert detect.find_claude("claude") == str(fake_claude)  # bare name resolved through PATH


def test_is_self_guard(tmp_path, fake_claude, monkeypatch):
    shim = tmp_path / "claude-shim"
    shim.write_text("#!/usr/bin/env python3\nimport sys\nfrom claude_router.route import main\nsys.exit(main())\n")
    shim.chmod(0o755)
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("#!/bin/sh\nexec route-claude \"$@\"\n")
    wrapper.chmod(0o755)
    assert detect.is_self(str(shim))
    assert detect.is_self(str(wrapper))
    assert not detect.is_self(str(fake_claude))
    assert not detect.is_self("/nonexistent")
    monkeypatch.setattr(detect, "known_locations", lambda: [])
    monkeypatch.setenv("PATH", str(tmp_path))
    assert detect.find_claude(str(shim)) is None


def test_claude_version(fake_claude):
    assert detect.claude_version(str(fake_claude)) == "9.9.9 (Claude Code)"
    assert detect.claude_version("/nonexistent/claude") is None


def test_all_candidates(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(fake_claude.parent))
    monkeypatch.setattr(detect, "known_locations", lambda: [("/nonexistent/claude", "nowhere")])
    monkeypatch.delenv("NVM_BIN", raising=False)
    candidates = detect.all_candidates(str(fake_claude))
    assert [c.path for c in candidates] == [str(fake_claude), "/nonexistent/claude"]
    assert candidates[0].source == "config claude_path"
    assert candidates[0].version == "9.9.9 (Claude Code)"
    assert candidates[0].executable and not candidates[0].is_self
    assert not candidates[1].exists

    link = tmp_path / "link-claude"
    link.symlink_to("/nonexistent/target")
    broken = detect.inspect(str(link), "test")
    assert broken.is_symlink and broken.broken and not broken.executable


def test_install_method():
    def cand(path, target=None):
        return detect.Candidate(path, "x", True, True, target is not None, target, False, False)

    assert detect.install_method(cand("/u/.local/bin/claude", "/u/.local/share/claude/versions/1.0")) == "native"
    assert detect.install_method(cand("/u/.nvm/bin/claude", "/u/.nvm/lib/node_modules/@anthropic-ai/claude-code/cli.js")) == "npm"
    assert detect.install_method(cand("/opt/homebrew/bin/claude", "/opt/homebrew/Cellar/claude-code/1/bin/claude")) == "homebrew"
    assert detect.install_method(cand("/u/.claude/local/claude")) == "legacy local npm"
    assert detect.install_method(cand("/somewhere/claude")) == "unknown"
