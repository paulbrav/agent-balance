"""Read-only instruments: throttle-event scan, swap churn, pool-session age,
the status metrics line, and the bench aggregation (token-free, injected
runner/clock)."""

import os
from pathlib import Path

from conftest import NOW, add_account

import agent_balance as ab


def write_jsonl(path: Path, text: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.utime(path, (mtime, mtime))


# ----------------------------------------------------- scan_throttle_events ---


def test_scan_counts_marker(tmp_path):
    home = tmp_path / "acct"
    line = '{"err":"... ' + ab.THROTTLE_MARKER + ' (not your usage limit) ..."}\n'
    write_jsonl(home / "projects" / "p" / "s.jsonl", '{"x":1}\n' + line, NOW - 100)
    r = ab.scan_throttle_events([home], NOW)
    assert r["total"] == 1
    assert r["recent"] == 1
    assert r["files"] == 1


def test_scan_recent_is_subset_of_total(tmp_path):
    home = tmp_path / "acct"
    mark = ab.THROTTLE_MARKER + "\n"
    write_jsonl(home / "projects" / "old.jsonl", mark, NOW - 7200)
    write_jsonl(home / "projects" / "new.jsonl", mark, NOW - 100)
    r = ab.scan_throttle_events([home], NOW)  # window 3600, lookback 86400
    assert r["total"] == 2  # both within lookback
    assert r["recent"] == 1  # only the fresh file within window


def test_scan_skips_beyond_lookback(tmp_path):
    home = tmp_path / "acct"
    old = home / "projects" / "ancient.jsonl"
    write_jsonl(old, ab.THROTTLE_MARKER + "\n", NOW - 200000)
    assert ab.scan_throttle_events([home], NOW)["total"] == 0


def test_scan_dedupes_by_resolved_path(tmp_path):
    home = tmp_path / "acct"
    write_jsonl(home / "projects" / "s.jsonl", ab.THROTTLE_MARKER + "\n", NOW - 50)
    assert ab.scan_throttle_events([home, home], NOW)["total"] == 1


def test_scan_failsoft_on_missing(tmp_path):
    r = ab.scan_throttle_events([tmp_path / "nope"], NOW)
    assert r["total"] == 0 and r["recent"] == 0 and r["files"] == 0
    assert r["window_s"] == 3600 and r["lookback_s"] == 86400


# ------------------------------------------------------------- swap_churn ---


def test_swap_churn_counts_recent_and_busts(cfg):
    cfg.cache.mkdir(parents=True)
    (cfg.cache / "swaps").write_text(
        f"{NOW - 1000} SWAP a b 50 reason\n"
        f"{NOW - 900} SWAP b c 60 reason\n"  # 100s after prev (<300) -> warm bust
        f"{NOW - 100} SWAP c d 70 reason\n"  # 800s after prev -> not a bust
    )
    r = ab.swap_churn(cfg, NOW, ttl=300)
    assert r["recent"] == 3
    assert r["warm_busts"] == 1


def test_swap_churn_window_excludes_old(cfg):
    cfg.cache.mkdir(parents=True)
    (cfg.cache / "swaps").write_text(
        f"{NOW - 200000} SWAP a b 50 r\n{NOW - 100} SWAP b c 60 r\n"
    )
    r = ab.swap_churn(cfg, NOW, ttl=300, window=86400)
    assert r["recent"] == 1  # the ancient swap is outside the 24h window


def test_swap_churn_failsoft(cfg):
    r = ab.swap_churn(cfg, NOW, ttl=300)
    assert r["recent"] == 0 and r["warm_busts"] == 0


# -------------------------------------------------------- pool_session_age ---


def test_pool_session_age(cfg):
    write_jsonl(cfg.pool / "projects" / "p" / "s.jsonl", "x\n", NOW - 42)
    age = ab.pool_session_age(cfg, NOW)
    assert age is not None and abs(age - 42) < 1.5


def test_pool_session_age_none_when_empty(cfg):
    assert ab.pool_session_age(cfg, NOW) is None


# ----------------------------------------------------- format_metrics_line ---


def test_format_metrics_line_empty():
    assert ab.format_metrics_line({}, NOW) == ""


def test_format_metrics_line_renders():
    m = {
        "throttle_events": {
            "total": 5,
            "recent": 2,
            "window_s": 3600,
            "lookback_s": 86400,
        },
        "swaps": {"recent": 3, "warm_busts": 1},
        "pool_session_age_s": 120,
    }
    line = ab.format_metrics_line(m, NOW)
    assert "2 rate-limit hits in 60m" in line
    assert "5 in 24h" in line
    assert "1 cache-warm" in line
    assert "active 2m ago" in line


# --------------------------------------------------------------- bench ---


def make_clock(values):
    it = iter(values)

    def clock():
        return next(it)

    return clock


def test_bench_env_strips_api_key_and_pins_dir():
    base = {
        "ANTHROPIC_API_KEY": "sk",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "ANTHROPIC_BASE_URL": "u",
        "PATH": "/x",
    }
    env = ab.bench_env(Path("/acct"), base)
    assert not any(k in env for k in ab.BENCH_ENV_DROP)
    assert env["PATH"] == "/x"
    assert env["CLAUDE_CONFIG_DIR"] == "/acct"


def test_bench_env_none_keeps_ambient():
    env = ab.bench_env(None, {"PATH": "/x", "ANTHROPIC_API_KEY": "sk"})
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/x"


def test_run_bench_batch_aggregates():
    runs = 0

    def runner(prompt, d):
        nonlocal runs
        runs += 1
        return 50, True

    r = ab.run_bench_batch("x", "p", [None, None], runner, make_clock([100.0, 102.5]))
    assert runs == 2
    assert r.runs == 2 and r.ok == 2 and r.out_tokens == 100
    assert r.wall_s == 2.5
    assert r.tps == 40.0  # 100 tok / 2.5 s


def test_run_bench_batch_counts_failures():
    def runner(prompt, d):
        return (0, False) if d is None else (10, True)

    r = ab.run_bench_batch("x", "p", [None, Path("/a")], runner, make_clock([0.0, 1.0]))
    assert r.ok == 1 and r.out_tokens == 10


def test_cmd_bench_reports_scaling(cfg):
    add_account(cfg, "alt1")

    def runner(prompt, d):
        return 10, True

    out = []
    # three batches (baseline, same, distinct) -> two clock reads each
    clock = make_clock([0, 1, 0, 1, 0, 1])
    rc = ab.cmd_bench(cfg, "p", 2, ["alt1"], runner=runner, clock=clock, out=out.append)
    text = "\n".join(out)
    assert rc == 0
    assert "same-account scaling" in text
    assert "distinct-account scaling" in text


def test_cmd_bench_skips_unknown_account(cfg):
    def runner(prompt, d):
        return 10, True

    out = []
    clock = make_clock([0, 1, 0, 1])  # no distinct batch -> only two batches
    ab.cmd_bench(cfg, "p", 1, ["ghost"], runner=runner, clock=clock, out=out.append)
    text = "\n".join(out)
    assert "unknown account 'ghost'" in text
    assert "distinct-account scaling" not in text
