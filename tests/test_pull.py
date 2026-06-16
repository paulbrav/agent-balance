"""Rebalance pull — proactively rotate toward a markedly more urgent
account (higher required burn rate), even while the installed one is fine."""

import dataclasses
import os

from conftest import D4, D6, H2, H12, NOW, add_account, install_pool, run_tick, usage

import agent_balance as ab


def touch_pool_session(cfg, age_s):
    """A pool transcript last written `age_s` ago — a (warm or cold) session."""
    jl = cfg.pool / "projects" / "proj" / "session.jsonl"
    jl.parent.mkdir(parents=True, exist_ok=True)
    jl.write_text("{}\n")
    os.utime(jl, (NOW - age_s, NOW - age_s))


def pull_cfg(cfg):
    return dataclasses.replace(cfg, pull_margin=10)


def test_pull_rotates_to_urgent_account(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))

    # alt1 needs 70/4 = 17.5%/day; alt2 needs 80/0.5 = 160%/day with 12h
    # left on its week — far past the margin.
    rc, out = run_tick(
        cfg,
        {
            "tok-alt1": usage(30, 30, H2, D4),
            "tok-alt2": usage(10, 20, H2, H12),
        },
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt2"
    assert any("rebalance" in line for line in out)


def test_no_pull_inside_margin(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))

    # alt1 17.5%/day vs alt2 80/6 = 13.3%/day: alt2 is LESS urgent.
    rc, out = run_tick(
        cfg,
        {
            "tok-alt1": usage(30, 30, H2, D4),
            "tok-alt2": usage(10, 20, H2, D6),
        },
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert any("ok" in line for line in out)


def test_pull_target_needs_5h_headroom(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))

    # alt2 is hugely urgent but nearly walled in its 5h window: a
    # proactive swap onto an almost-walled account is refused.
    rc, out = run_tick(
        cfg,
        {
            "tok-alt1": usage(30, 30, H2, D4),
            "tok-alt2": usage(92, 20, H2, H12),
        },
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"


def test_pull_respects_min_gap(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    state = ab.read_state(cfg, NOW)
    state["last_swap_epoch"] = NOW - 30  # swapped 30s ago, gap is 300
    ab.write_state(cfg, state)

    rc, out = run_tick(
        cfg,
        {
            "tok-alt1": usage(30, 30, H2, D4),
            "tok-alt2": usage(10, 20, H2, H12),
        },
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert any("inside the" in line for line in out)


def test_pull_check_is_rate_limited(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    cfg.cache.mkdir(parents=True, exist_ok=True)
    (cfg.cache / "pull-check").write_text(str(NOW - 60))  # checked recently

    # The fleet must NOT be probed: a table without alt2 would assert if
    # the pull path ran.
    rc, out = run_tick(cfg, {"tok-alt1": usage(30, 30, H2, D4)})

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert any("ok" in line for line in out)


def test_pull_held_while_session_warm(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    touch_pool_session(cfg, age_s=10)  # a turn landed 10s ago — cache is warm

    # alt2 is far more urgent, but a discretionary rebalance must not bust a
    # warm session's prompt cache.
    rc, out = run_tick(
        cfg,
        {"tok-alt1": usage(30, 30, H2, D4), "tok-alt2": usage(10, 20, H2, H12)},
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert any("prompt cache warm" in line for line in out)


def test_pull_proceeds_when_session_cold(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    touch_pool_session(cfg, age_s=1000)  # last turn >cache_ttl ago — cold

    rc, out = run_tick(
        cfg,
        {"tok-alt1": usage(30, 30, H2, D4), "tok-alt2": usage(10, 20, H2, H12)},
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt2"


def test_roll_swaps_through_warm_session(cfg):
    # A 5h-wall roll must NOT be held by the cache-warmth guard: hitting the
    # wall is worse than a cache miss.
    cfg = dataclasses.replace(cfg, threshold=85)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    touch_pool_session(cfg, age_s=5)  # warm, but the installed account is walling

    rc, out = run_tick(
        cfg,
        {"tok-alt1": usage(92, 30, H2, D4), "tok-alt2": usage(10, 20, H2, D6)},
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt2"


def test_pull_disabled_by_zero_margin(cfg):
    add_account(cfg, "alt1")  # fixture cfg has pull_margin=0
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))

    rc, out = run_tick(cfg, {"tok-alt1": usage(30, 30, H2, D4)})
    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert not (cfg.cache / "pull-check").exists()
