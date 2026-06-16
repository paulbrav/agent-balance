# calibrate parses all four real on-disk formats from committed fixtures —
# never depending on ~/.cache.

from sim.calibrate import (
    installed_at,
    load_real,
    read_history,
    read_snapshot,
    read_swaps,
    read_throttle_timestamps,
)
from sim.tests.conftest import FIXTURES


def test_read_history_parses_series():
    hist = read_history(FIXTURES / "main.history")
    assert len(hist) == 20
    assert hist[0] == (1781573842, 0.0)
    assert hist == sorted(hist)  # sorted by epoch


def test_read_history_skips_malformed(tmp_path):
    p = tmp_path / "x.history"
    p.write_text("1781573842 0.0\ngarbage line\n1781573900 5.0\nbad two\n")
    assert read_history(p) == [(1781573842, 0.0), (1781573900, 5.0)]


def test_read_snapshot_via_usage_from_cache():
    u = read_snapshot(FIXTURES / "alt2")
    assert u is not None
    assert u.five == 39.0
    assert u.seven == 47.0
    assert u.r5 == 1781591000


def test_read_swaps_timeline():
    swaps = read_swaps(FIXTURES / "swaps")
    assert [s.to for s in swaps] == ["main", "alt2", "main"]
    assert installed_at(swaps, 1781574000) == "main"
    assert installed_at(swaps, 1781581000) == "alt2"
    assert installed_at(swaps, 1781572000) == "unknown"  # before the first swap


def test_throttle_timestamps_deduped():
    # parent has 3 markers; the subagent copy re-stamps 2 of them. Dedup by
    # exact ISO timestamp -> 3 unique incidents.
    epochs = read_throttle_timestamps(FIXTURES / "projects")
    assert len(epochs) == 3
    assert epochs == sorted(epochs)


def test_load_real_seeds_world():
    cal = load_real(FIXTURES, FIXTURES / "projects")
    assert set(cal.accounts) == {"main", "alt1", "alt2", "alt3"}
    # snapshot seeded the quota windows
    assert cal.accounts["alt2"].five == 39.0
    # flat-zero accounts are starved
    assert cal.fits["alt1"].starved
    assert cal.fits["alt3"].starved
    assert not cal.fits["main"].starved
    # learned caps.json applied
    assert cal.accounts["main"].k_a == 10.0
