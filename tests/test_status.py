"""cmd_status — the human text path (byte-path preserved) and the new
`status --json` DATA contract (ISSUE 1)."""

import json
import time

import pytest
from conftest import H2, HD, add_account, install_pool, usage

import agent_balance as ab


def live(cfg, name, **kw):
    """add_account with a token whose expiry is in the REAL future, so
    cred_status treats it as usable and the seeded cache is what surfaces.
    (conftest's NOW-based default expiry is in 2023, i.e. already expired
    against time.time(), which would short-circuit to 'expired'.)"""
    return add_account(cfg, name, expires_ms=(int(time.time()) + 6 * 3600) * 1000, **kw)


def seed_cache(cfg, name, u, epoch):
    """A deterministic last-known probe on disk (copy of test_resilience's)."""
    cfg.cache.mkdir(parents=True, exist_ok=True)
    (cfg.cache / name).write_text(
        json.dumps(
            {"epoch": epoch, "five": u.five, "seven": u.seven, "r5": u.r5, "r7": u.r7}
        )
    )


def capture(cfg, capsys, **kwargs):
    rc = ab.cmd_status(cfg, **kwargs)
    return rc, capsys.readouterr().out


def test_status_text_lists_accounts_and_pool(cfg, capsys, monkeypatch):
    # No subprocess in the text path: stub the timer + version probes.
    monkeypatch.setattr(ab, "query_timer", lambda: "active")
    monkeypatch.setattr(
        ab, "check_claude_version", lambda: ab.ClaudeVersion("2.1.175", None, False)
    )
    live(cfg, "alt1")
    live(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    # Fresh cache (epoch=now) keeps the network probe short-circuited.
    now = time.time()
    seed_cache(cfg, "alt1", usage(40, 20, H2, HD), now)
    seed_cache(cfg, "alt2", usage(10, 10, H2, HD), now)

    rc, out = capture(cfg, capsys)
    assert rc == 0
    assert f"accounts ({cfg.root}):" in out
    assert "alt1" in out
    assert "<- installed" in out
    assert "pool:" in out
    assert "installed: alt1" in out
    assert "timer:" in out


def test_status_text_byte_path_unchanged_for_no_accounts(cfg, capsys, monkeypatch):
    monkeypatch.setattr(ab, "query_timer", lambda: "no systemd")
    monkeypatch.setattr(
        ab, "check_claude_version", lambda: ab.ClaudeVersion("2.1.175", None, False)
    )
    rc, out = capture(cfg, capsys)
    assert rc == 0
    assert "(none logged in)" in out


def test_status_json_shape(cfg, capsys, monkeypatch):
    monkeypatch.setattr(ab, "query_timer", lambda: "active")
    monkeypatch.setattr(
        ab, "check_claude_version", lambda: ab.ClaudeVersion("2.1.175", "2.1.180", True)
    )
    live(cfg, "alt1")
    add_account(cfg, "alt2", expires_ms=(int(time.time()) - 60) * 1000)  # expired
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    seed_cache(cfg, "alt1", usage(37, 12, H2, HD), time.time() - 600)

    rc, out = capture(cfg, capsys, as_json=True)
    assert rc == 0
    doc = json.loads(out)

    assert set(doc) >= {
        "version",
        "now",
        "root",
        "accounts",
        "pool",
        "installed",
        "timer",
        "recent_swaps",
        "claude_version",
        "health",
    }
    assert doc["version"] == ab.VERSION
    assert doc["root"] == str(cfg.root)
    # The tray's sole indicator signal: snapshot_to_json must emit it (the tray
    # fails open, so a dropped key would silently go dark). No 429s here -> ok.
    assert doc["health"] == {"state": "ok", "label": ""}

    by = {a["name"]: a for a in doc["accounts"]}
    # INVARIANT: usage XOR status — exactly one is non-null, per account.
    for a in doc["accounts"]:
        assert (a["usage"] is None) != (a["status"] is None)

    alt1 = by["alt1"]
    assert alt1["usage"]["five"] == 37
    assert alt1["installed"] is True
    assert alt1["status"] is None

    alt2 = by["alt2"]
    assert alt2["usage"] is None
    assert alt2["status"] == "expired"

    assert doc["pool"]["exists"] is True
    assert doc["pool"]["path"] == str(cfg.pool)
    assert doc["installed"]["name"] == "alt1"
    assert isinstance(doc["installed"]["last_swap_epoch"], int)
    assert isinstance(doc["recent_swaps"], list)
    assert doc["claude_version"]["verified"] == "2.1.175"
    assert doc["claude_version"]["mismatch"] is True


def test_status_json_no_accounts(cfg, capsys, monkeypatch):
    monkeypatch.setattr(ab, "query_timer", lambda: "no systemd")
    monkeypatch.setattr(
        ab, "check_claude_version", lambda: ab.ClaudeVersion("2.1.175", None, False)
    )
    rc, out = capture(cfg, capsys, as_json=True)
    assert rc == 0
    doc = json.loads(out)
    assert doc["accounts"] == []
    assert doc["claude_version"]["installed"] is None


def test_status_refresh_runs_fleet_probe(cfg, capsys, monkeypatch):
    monkeypatch.setattr(ab, "query_timer", lambda: "active")
    monkeypatch.setattr(
        ab, "check_claude_version", lambda: ab.ClaudeVersion("2.1.175", None, False)
    )
    live(cfg, "alt1")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    # Fresh cache so the per-account probe short-circuits (no real network).
    seed_cache(cfg, "alt1", usage(40, 20, H2, HD), time.time())

    called = []
    real_probe_fleet = ab.probe_fleet

    def spy(accounts, c, now, fetcher=None):
        called.append(True)
        return real_probe_fleet(accounts, c, now, fetcher)

    monkeypatch.setattr(ab, "probe_fleet", spy)

    rc, out = capture(cfg, capsys, as_json=True, refresh=True)
    assert rc == 0
    assert called  # --refresh ran the staggered fleet sweep
    doc = json.loads(out)
    assert doc["accounts"][0]["usage"] is not None


def test_status_json_pool_absent(cfg, capsys, monkeypatch):
    monkeypatch.setattr(ab, "query_timer", lambda: "no systemd")
    monkeypatch.setattr(
        ab, "check_claude_version", lambda: ab.ClaudeVersion("2.1.175", None, False)
    )
    live(cfg, "alt1")
    seed_cache(cfg, "alt1", usage(40, 20, H2, HD), time.time())
    rc, out = capture(cfg, capsys, as_json=True)
    assert rc == 0
    doc = json.loads(out)
    assert doc["pool"]["exists"] is False
    assert doc["installed"]["name"] == "unknown"


@pytest.mark.parametrize("flag", [{}, {"as_json": True}])
def test_status_returns_zero(cfg, capsys, monkeypatch, flag):
    monkeypatch.setattr(ab, "query_timer", lambda: "no systemd")
    monkeypatch.setattr(
        ab, "check_claude_version", lambda: ab.ClaudeVersion("2.1.175", None, False)
    )
    rc, _ = capture(cfg, capsys, **flag)
    assert rc == 0
