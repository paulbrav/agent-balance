"""pick_target — port of agent-pick's best_account scenarios."""

import agent_balance as ab
from conftest import NOW, add_account, usage

D4 = NOW + 4 * 86400      # weekly reset 4 days out (~43% of week elapsed)
D7 = NOW + 7 * 86400      # weekly window just rolled over
HD = NOW + 302400         # half the week left (50% elapsed)
H2 = NOW + 7200           # active 5h window, 2h to reset


def accounts_for(cfg, specs):
    accts = []
    for name, cap in specs:
        add_account(cfg, name, capacity=cap)
    accts = ab.discover_accounts(cfg)
    return accts


def test_pace_rules_when_far_apart(cfg):
    accts = accounts_for(cfg, [("a-main", None), ("alt1", None),
                               ("alt2", None)])
    stats = {
        "a-main": usage(0, 79, 0, D4),
        "alt1": usage(45, 29, H2, D4),
        "alt2": usage(29, 6, NOW + 14400, D7),
    }
    target, feasible = ab.pick_target(accts, stats, None, NOW, cfg)
    assert (target.name, feasible) == ("alt1", True)


def test_capacity_weighting_flips_the_pick(cfg):
    accts = accounts_for(cfg, [("biga", 20), ("smallb", None)])
    stats = {"biga": usage(0, 45, 0, HD), "smallb": usage(0, 30, 0, HD)}
    target, feasible = ab.pick_target(accts, stats, None, NOW, cfg)
    assert (target.name, feasible) == ("biga", True)


def test_feasibility_gate_at_draw(cfg):
    accts = accounts_for(cfg, [("full", None), ("free", None)])
    stats = {"full": usage(91, 10, H2, HD), "free": usage(50, 50, H2, HD)}
    target, feasible = ab.pick_target(accts, stats, None, NOW, cfg)
    assert (target.name, feasible) == ("free", True)


def test_imminent_reset_restores_feasibility(cfg):
    accts = accounts_for(cfg, [("full", None), ("free", None)])
    stats = {"full": usage(91, 10, NOW + 600, HD),
             "free": usage(50, 50, H2, HD)}
    target, feasible = ab.pick_target(accts, stats, None, NOW, cfg)
    assert (target.name, feasible) == ("full", True)


def test_all_infeasible_flags_it(cfg):
    accts = accounts_for(cfg, [("gone", None)])
    stats = {"gone": usage(95, 99, H2, HD)}
    target, feasible = ab.pick_target(accts, stats, None, NOW, cfg)
    assert (target.name, feasible) == ("gone", False)


def test_passed_reset_counts_as_zero(cfg):
    accts = accounts_for(cfg, [("stale", None), ("live", None)])
    stats = {"stale": usage(80, 20, NOW - 10, HD),
             "live": usage(10, 20, H2, HD)}
    target, feasible = ab.pick_target(accts, stats, None, NOW, cfg)
    assert (target.name, feasible) == ("stale", True)


def test_exclusion_skips_the_installed_account(cfg):
    accts = accounts_for(cfg, [("best", None), ("other", None)])
    stats = {"best": usage(0, 0, 0, HD), "other": usage(50, 50, H2, HD)}
    target, _ = ab.pick_target(accts, stats, None, NOW, cfg)
    assert target.name == "best"
    target, feasible = ab.pick_target(accts, stats, "best", NOW, cfg)
    assert (target.name, feasible) == ("other", True)


def test_exclusion_of_only_account_yields_none(cfg):
    accts = accounts_for(cfg, [("solo", None)])
    stats = {"solo": usage(10, 10, H2, HD)}
    assert ab.pick_target(accts, stats, "solo", NOW, cfg) == (None, False)


def test_non_usage_stats_are_skipped(cfg):
    accts = accounts_for(cfg, [("dead", None), ("ok", None)])
    stats = {"dead": "expired", "ok": usage(40, 40, H2, HD)}
    target, feasible = ab.pick_target(accts, stats, None, NOW, cfg)
    assert (target.name, feasible) == ("ok", True)
