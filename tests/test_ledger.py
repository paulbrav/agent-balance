"""Throttle ledger: detect real instance 429s per account and stamp them with
the live concurrency n_a, so the unobserved per-minute knee becomes data."""

import json
import os

from conftest import D4, H2, NOW, add_account, install_pool, run_tick, usage

import agent_balance as ab


def write_marker(home, mtime, count=1):
    f = home / "projects" / "p" / "s.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text((ab.THROTTLE_MARKER + " (not your usage limit)\n") * count)
    os.utime(f, (mtime, mtime))


def ledger_rows(cfg):
    p = cfg.cache / "throttle_ledger.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]


def seed_seen(cfg, name, epoch):
    cfg.cache.mkdir(parents=True, exist_ok=True)
    ab.stamp_set(cfg.cache / f"{name}.throttle-seen", epoch)


def test_record_throttles_writes_row_with_account_and_n_a(cfg):
    home = add_account(cfg, "alt1")
    seed_seen(cfg, "alt1", NOW - 1000)
    write_marker(home, NOW - 100)
    hits = ab.record_throttles(cfg, home, "alt1", 3, NOW)
    assert hits == 1
    rows = ledger_rows(cfg)
    assert len(rows) == 1
    assert rows[0]["account"] == "alt1"
    assert rows[0]["n_a"] == 3
    assert rows[0]["hits"] == 1


def test_ledger_idempotent_no_double_count(cfg):
    home = add_account(cfg, "alt1")
    seed_seen(cfg, "alt1", NOW - 1000)
    write_marker(home, NOW - 100)
    ab.record_throttles(cfg, home, "alt1", 1, NOW)
    ab.record_throttles(cfg, home, "alt1", 1, NOW)  # same now, file untouched
    assert len(ledger_rows(cfg)) == 1


def test_ledger_counts_new_hits_after_retouch(cfg):
    home = add_account(cfg, "alt1")
    seed_seen(cfg, "alt1", NOW - 1000)
    write_marker(home, NOW - 100)
    ab.record_throttles(cfg, home, "alt1", 1, NOW)
    write_marker(home, NOW + 10)  # a new turn writes another 429
    ab.record_throttles(cfg, home, "alt1", 2, NOW + 10)
    rows = ledger_rows(cfg)
    assert len(rows) == 2
    assert rows[1]["n_a"] == 2


def test_first_scan_starts_clock_no_backfill(cfg):
    home = add_account(cfg, "alt1")
    write_marker(home, NOW - 100)  # a historical 429, no seen marker yet
    assert ab.record_throttles(cfg, home, "alt1", 5, NOW) == 0
    assert ledger_rows(cfg) == []  # history is not backfilled (its n_a is unknown)
    write_marker(home, NOW + 10)  # but a NEW one afterwards is logged
    ab.record_throttles(cfg, home, "alt1", 5, NOW + 10)
    assert len(ledger_rows(cfg)) == 1


def test_ledger_ignores_limited_flag(cfg):
    home = add_account(cfg, "alt1")
    seed_seen(cfg, "alt1", NOW - 1000)
    ab.stamp_set(cfg.cache / "alt1.limited", NOW - 50)  # the WRONG stream
    ab.record_throttles(cfg, home, "alt1", 1, NOW)  # no transcript marker
    assert ledger_rows(cfg) == []


def test_scan_new_throttles_respects_lookback(cfg):
    home = add_account(cfg, "alt1")
    write_marker(home, NOW - 200000)
    assert ab.scan_new_throttles(home, NOW - 300000, NOW) == 0


def test_scan_new_throttles_failsoft_missing_projects(cfg):
    home = add_account(cfg, "alt1")  # no projects/ dir
    assert ab.scan_new_throttles(home, NOW - 1000, NOW) == 0


def test_ledger_throttles_stamps_live_n_a(cfg):
    home = add_account(cfg, "alt1")
    seed_seen(cfg, "alt1", NOW - 1000)
    write_marker(home, NOW - 100)
    accounts = ab.discover_accounts(cfg)
    leases = {os.getpid(): {"account": "alt1", "started": NOW}}
    ab.ledger_throttles(cfg, accounts, NOW, leases)
    rows = ledger_rows(cfg)
    assert any(r["account"] == "alt1" and r["n_a"] == 1 for r in rows)


def test_read_throttle_ledger_summary(cfg):
    cfg.cache.mkdir(parents=True)
    (cfg.cache / "throttle_ledger.jsonl").write_text(
        json.dumps({"epoch": NOW - 100, "account": "a", "n_a": 2, "hits": 1})
        + "\n"
        + json.dumps({"epoch": NOW - 50, "account": "b", "n_a": 5, "hits": 3})
        + "\n"
        + json.dumps({"epoch": NOW - 200000, "account": "c", "n_a": 9, "hits": 1})
        + "\n"
    )
    s = ab.read_throttle_ledger(cfg, NOW)
    assert s["rows"] == 2  # the ancient row is outside the 24h window
    assert s["hits"] == 4
    assert s["max_n_a"] == 5


def test_format_metrics_line_shows_ledger():
    m = {
        "throttle_events": {
            "recent": 0,
            "total": 0,
            "window_s": 3600,
            "lookback_s": 86400,
        },
        "swaps": {"recent": 0, "warm_busts": 0},
        "pool_session_age_s": None,
        "throttle_ledger": {"rows": 2, "hits": 4, "max_n_a": 5, "window_s": 86400},
    }
    assert "ledger 4 429s (n_a≤5)" in ab.format_metrics_line(m, NOW)


def test_tick_writes_ledger_row(cfg):
    home = add_account(cfg, "alt1")
    seed_seen(cfg, "alt1", NOW - 1000)
    write_marker(home, NOW - 100)
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    run_tick(cfg, {"tok-alt1": usage(30, 30, H2, D4)})
    assert any(r["account"] == "alt1" for r in ledger_rows(cfg))
