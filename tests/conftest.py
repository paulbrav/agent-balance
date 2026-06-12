import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_balance as ab  # noqa: E402

NOW = 1_700_000_000

# Epoch landmarks shared across the suite.
H2 = NOW + 7200  # active 5h window, 2h to reset
H12 = NOW + 12 * 3600  # 12h to weekly reset
HD = NOW + 302400  # half the week left (50% elapsed)
D4 = NOW + 4 * 86400  # weekly reset 4 days out (~43% of week elapsed)
D6 = NOW + 6 * 86400  # fresh-ish week
D7 = NOW + 7 * 86400  # weekly window just rolled over


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
        pull_margin=0,  # the rebalance pull is opt-in per test
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


def install_pool(cfg, account, email=None):
    """Pool holding a copy of the account's creds, with matching state."""
    cfg.pool.mkdir(parents=True)
    sha = ab.copy_creds(account.creds, cfg.pool / ".credentials.json")
    (cfg.pool / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email or account.email}})
    )
    state = {
        "installed": account.name,
        "blob_sha256": sha,
        "last_swap_epoch": NOW - 3600,
    }
    ab.write_state(cfg, state)
    return state


def run_tick(cfg, table, now=NOW):
    out = []
    rc = ab.tick(cfg, now=now, fetcher=make_fetcher(table), out=out.append)
    return rc, out
