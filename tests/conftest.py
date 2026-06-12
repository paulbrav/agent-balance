import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_balance as ab  # noqa: E402

NOW = 1_700_000_000


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Config rooted in tmp; HOME is repointed so ~/.claude resolves inside
    the sandbox (discover_accounts reads it for 'main')."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return ab.Config(
        root=tmp_path / "accounts",
        cache=tmp_path / "cache",
        threshold=85,
        min_gap=300,
        interval=60,
        draw=10,
        pull_hours=0,  # the deadline pull is opt-in per test
        pull_margin=20,
    )


def add_account(
    cfg, name, email=None, expires_ms=(NOW + 6 * 3600) * 1000, capacity=None
):
    """A logged-in Anthropic account dir with a distinctive token."""
    home = cfg.root / name
    home.mkdir(parents=True, exist_ok=True)
    (home / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": f"tok-{name}",
                    "refreshToken": f"ref-{name}",
                    "expiresAt": expires_ms,
                }
            }
        )
    )
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {"emailAddress": email or f"{name}@example.com"},
                "hasCompletedOnboarding": True,
            }
        )
    )
    if capacity is not None:
        (home / "agent-pick.json").write_text(json.dumps({"capacity": capacity}))
    return home


def make_fetcher(table):
    """Map token -> Usage | status word; unmapped tokens error loudly."""

    def fetcher(token):
        assert token in table, f"unexpected probe for token {token}"
        return table[token]

    return fetcher


def usage(five, seven, r5=0, r7=0):
    return ab.Usage(five, seven, r5, r7)
