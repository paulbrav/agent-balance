"""Account discovery against agent-pick's directory layout."""

import json
import os
from pathlib import Path

import agent_balance as ab
from conftest import NOW, add_account


def test_main_meta_lives_at_home_root(cfg):
    """The default ~/.claude account keeps its .claude.json at ~/.claude.json
    (home root), not inside the config dir — discovery must read it there."""
    home = Path(os.path.expanduser("~"))
    claude = home / ".claude"
    claude.mkdir()
    (claude / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "tok-main",
                          "expiresAt": (NOW + 3600) * 1000}}))
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "main@example.com"}}))

    accounts = ab.discover_accounts(cfg)
    main = ab.by_name(accounts, "main")
    assert main is not None
    assert main.email == "main@example.com"
    assert ab.meta_path(main.home) == home / ".claude.json"


def test_non_claude_dirs_are_skipped(cfg):
    add_account(cfg, "alt1")
    # Codex dir (auth.json), foreign kind, API-key backend, not logged in.
    codex = cfg.root / "codex2"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text("{}")
    grok = cfg.root / "grok-work"
    grok.mkdir()
    (grok / "agent-pick.json").write_text(json.dumps({"kind": "grok"}))
    (grok / ".credentials.json").write_text("{}")
    kimi = cfg.root / "kimi"
    kimi.mkdir()
    (kimi / "settings.json").write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "https://api.moonshot.ai/anthropic"}}))
    (kimi / ".credentials.json").write_text("{}")
    (cfg.root / "alt-fresh").mkdir()  # no credentials yet

    names = [a.name for a in ab.discover_accounts(cfg)]
    assert names == ["alt1"]


def test_capacity_read_from_agent_pick_json(cfg):
    add_account(cfg, "alt1", capacity=20)
    add_account(cfg, "alt2")
    accounts = ab.discover_accounts(cfg)
    assert ab.by_name(accounts, "alt1").capacity == 20
    assert ab.by_name(accounts, "alt2").capacity == 1
