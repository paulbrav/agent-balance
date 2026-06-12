"""pick_target — port of agent-pick's best_account scenarios."""

import dataclasses

from conftest import D4, D7, H2, HD, NOW, add_account, usage

import agent_balance as ab


def accounts_for(cfg, specs):
    for name, cap in specs:
        add_account(cfg, name, capacity=cap)
    return ab.discover_accounts(cfg)


def picked(cfg, accts, stats, exclude=None):
    """pick_target unwrapped to a name, asserting a pick exists."""
    target = ab.pick_target(accts, stats, exclude, NOW, cfg)
    assert target is not None
    return target.name


def test_pace_rules_when_far_apart(cfg):
    accts = accounts_for(cfg, [("a-main", None), ("alt1", None), ("alt2", None)])
    stats = {
        "a-main": usage(0, 79, 0, D4),
        "alt1": usage(45, 29, H2, D4),
        "alt2": usage(29, 6, NOW + 14400, D7),
    }
    assert picked(cfg, accts, stats) == "alt1"


def test_capacity_weighting_flips_the_pick(cfg):
    accts = accounts_for(cfg, [("biga", 20), ("smallb", None)])
    stats = {"biga": usage(0, 45, 0, HD), "smallb": usage(0, 30, 0, HD)}
    assert picked(cfg, accts, stats) == "biga"


def test_feasibility_gate_at_draw(cfg):
    accts = accounts_for(cfg, [("full", None), ("free", None)])
    stats = {"full": usage(91, 10, H2, HD), "free": usage(50, 50, H2, HD)}
    assert picked(cfg, accts, stats) == "free"


def test_imminent_reset_restores_feasibility(cfg):
    accts = accounts_for(cfg, [("full", None), ("free", None)])
    stats = {"full": usage(91, 10, NOW + 600, HD), "free": usage(50, 50, H2, HD)}
    assert picked(cfg, accts, stats) == "full"


def test_all_infeasible_yields_none(cfg):
    accts = accounts_for(cfg, [("gone", None)])
    stats = {"gone": usage(95, 99, H2, HD)}
    assert ab.pick_target(accts, stats, None, NOW, cfg) is None


def test_passed_reset_counts_as_zero(cfg):
    accts = accounts_for(cfg, [("stale", None), ("live", None)])
    stats = {"stale": usage(80, 20, NOW - 10, HD), "live": usage(10, 20, H2, HD)}
    assert picked(cfg, accts, stats) == "stale"


def test_exclusion_skips_the_installed_account(cfg):
    accts = accounts_for(cfg, [("best", None), ("other", None)])
    stats = {"best": usage(0, 0, 0, HD), "other": usage(50, 50, H2, HD)}
    assert picked(cfg, accts, stats) == "best"
    assert picked(cfg, accts, stats, exclude="best") == "other"


def test_exclusion_of_only_account_yields_none(cfg):
    accts = accounts_for(cfg, [("solo", None)])
    stats = {"solo": usage(10, 10, H2, HD)}
    assert ab.pick_target(accts, stats, "solo", NOW, cfg) is None


def test_non_usage_stats_are_skipped(cfg):
    accts = accounts_for(cfg, [("dead", None), ("ok", None)])
    stats = {"dead": "expired", "ok": usage(40, 40, H2, HD)}
    assert picked(cfg, accts, stats) == "ok"


def pull_target(cfg, accts, stats, installed_name):
    installed = ab.by_name(accts, installed_name)
    assert installed is not None
    return ab.pick_pull_target(accts, stats, installed, stats[installed_name], cfg, NOW)


def test_pull_wins_exactly_at_the_margin(cfg):
    cfg = dataclasses.replace(cfg, pull_margin=10)
    accts = accounts_for(cfg, [("inst", None), ("win", None)])
    stats = {
        "inst": usage(10, 30, H2, NOW + 5 * 86400),  # 70/5 = 14 %/day
        "win": usage(10, 40, H2, NOW + 216000),  # 60/2.5 = 24 = 14 + margin
    }
    pull = pull_target(cfg, accts, stats, "inst")
    assert pull is not None
    target, u_win, u_inst = pull
    assert (target.name, u_win, u_inst) == ("win", 24.0, 14.0)

    # A hair less urgent and the inclusive margin is no longer met.
    stats["win"] = usage(10, 41, H2, NOW + 216000)  # 59/2.5 = 23.6
    assert pull_target(cfg, accts, stats, "inst") is None


def test_pull_margin_scales_with_high_urgency(cfg):
    cfg = dataclasses.replace(cfg, pull_margin=10)
    accts = accounts_for(cfg, [("inst", None), ("win", None)])
    # The installed account needs 100%/day, so the effective margin is
    # 0.15 * 100 = 15, not the configured 10.
    stats = {
        "inst": usage(10, 0, H2, NOW + 86400),
        "win": usage(10, 0, H2, NOW + 77143),  # ~112%/day: beats 110, not 115
    }
    assert pull_target(cfg, accts, stats, "inst") is None
    stats["win"] = usage(10, 0, H2, NOW + 43200)  # 200%/day clears 115
    pull = pull_target(cfg, accts, stats, "inst")
    assert pull is not None and pull[0].name == "win"


def test_pull_rejects_targets_near_the_swap_threshold(cfg):
    cfg = dataclasses.replace(cfg, pull_margin=10)
    accts = accounts_for(cfg, [("inst", None), ("win", None)])
    # win is far more urgent, but a typical session's draw would land it
    # at the swap threshold (75 + 10 >= 85): never pull onto an
    # almost-walled account.
    stats = {
        "inst": usage(10, 30, H2, NOW + 5 * 86400),
        "win": usage(75, 0, H2, NOW + 43200),
    }
    assert pull_target(cfg, accts, stats, "inst") is None
