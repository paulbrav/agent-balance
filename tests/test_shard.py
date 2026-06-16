"""Automatic instance sharding: the PID-keyed lease registry and the
water-filling allocator that spreads concurrent instances across accounts."""

import os

from conftest import D4, H2, H12, NOW, add_account, usage

import agent_balance as ab

DEAD_PID = 2**31 - 1  # implausibly high — never a live process


# ------------------------------------------------------------- leases ---


def test_write_read_leases_roundtrip(cfg):
    ab.write_leases(
        cfg,
        {
            111: {"account": "alt1", "started": NOW},
            222: {"account": "alt2", "started": NOW},
        },
    )
    out = ab.read_leases(cfg)
    assert out[111]["account"] == "alt1"
    assert out[222]["account"] == "alt2"


def test_read_leases_drops_malformed(cfg):
    cfg.cache.mkdir(parents=True)
    ab.lease_file(cfg).write_text(
        '{"111": {"account": "alt1"}, "x": {"account": "alt2"}, "333": {"no": "acct"}}'
    )
    out = ab.read_leases(cfg)
    assert set(out) == {111}  # non-int key and account-less entry dropped


def test_account_load_counts_per_account():
    leases = {1: {"account": "a"}, 2: {"account": "a"}, 3: {"account": "b"}}
    assert ab.account_load(leases) == {"a": 2, "b": 1}


def test_pid_alive():
    assert ab.pid_alive(os.getpid()) is True
    assert ab.pid_alive(DEAD_PID) is False


def test_gc_drops_dead_and_persists(cfg):
    ab.write_leases(
        cfg,
        {
            os.getpid(): {"account": "alt1", "started": NOW},
            DEAD_PID: {"account": "alt2", "started": NOW},
        },
    )
    live = ab.gc_leases(cfg)
    assert set(live) == {os.getpid()}
    assert set(ab.read_leases(cfg)) == {os.getpid()}  # written back


def test_add_lease_gcs_first(cfg):
    ab.write_leases(cfg, {DEAD_PID: {"account": "alt2", "started": NOW}})
    ab.add_lease(cfg, os.getpid(), "alt1", NOW)
    out = ab.read_leases(cfg)
    assert set(out) == {os.getpid()}  # the dead lease was reaped on add
    assert out[os.getpid()]["account"] == "alt1"


# ----------------------------------------------------- pick_shard_target ---


def shard_accounts(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    return ab.discover_accounts(cfg)


def base_stats():
    # alt2 is far more urgent (12h to weekly reset) than alt1 (4 days).
    return {
        "alt1": usage(30, 30, H2, D4),
        "alt2": usage(10, 20, H2, H12),
        "main": "no data yet",
    }


def test_shard_waterfills_to_most_urgent(cfg):
    accts = shard_accounts(cfg)
    t = ab.pick_shard_target(accts, base_stats(), {}, cap={}, now=NOW, cfg=cfg)
    assert t is not None and t.name == "alt2"  # empty load -> most urgent


def test_shard_spills_when_urgent_account_capped(cfg):
    accts = shard_accounts(cfg)
    t = ab.pick_shard_target(accts, base_stats(), {"alt2": 6}, cap={}, now=NOW, cfg=cfg)
    assert t is not None and t.name == "alt1"  # alt2 at cap -> spill


def test_shard_all_capped_picks_least_loaded(cfg):
    accts = shard_accounts(cfg)
    t = ab.pick_shard_target(
        accts, base_stats(), {"alt1": 7, "alt2": 6}, cap={}, now=NOW, cfg=cfg
    )
    assert t is not None and t.name == "alt2"  # both capped -> emptiest (6<7)


def test_shard_none_when_no_usable_account(cfg):
    add_account(cfg, "alt1")
    accts = ab.discover_accounts(cfg)
    stats = {"alt1": "no data yet", "main": "no data yet"}
    assert ab.pick_shard_target(accts, stats, {}, cap={}, now=NOW, cfg=cfg) is None


def test_per_account_cap_respected(cfg):
    accts = shard_accounts(cfg)
    # alt2 is most urgent but its per-account cap (2) is reached -> spill to alt1
    t = ab.pick_shard_target(accts, base_stats(), {"alt2": 2}, {"alt2": 2}, NOW, cfg)
    assert t is not None and t.name == "alt1"


def test_cap_default_when_missing(cfg):
    accts = shard_accounts(cfg)
    # empty cap map -> every account defaults to cfg.instances_per_account (6)
    t = ab.pick_shard_target(accts, base_stats(), {"alt2": 5}, {}, NOW, cfg)
    assert t is not None and t.name == "alt2"  # 5 < default 6


def test_effective_caps_applies_alpha_and_seeds_default(cfg):
    accts = shard_accounts(cfg)
    ab.write_caps(cfg, {"alt2": 8})
    eff = ab.effective_caps(cfg, accts)
    assert eff["alt2"] == 6  # floor(0.75 * 8)
    assert eff["alt1"] == 4  # floor(0.75 * default 6)


def test_effective_caps_never_zero(cfg):
    accts = shard_accounts(cfg)
    ab.write_caps(cfg, {"alt2": 1})
    assert ab.effective_caps(cfg, accts)["alt2"] >= 1


def test_read_caps_drops_malformed(cfg):
    cfg.cache.mkdir(parents=True)
    ab.caps_file(cfg).write_text('{"alt2": 8, "bad": -1, "x": "nope"}')
    assert ab.read_caps(cfg) == {"alt2": 8}


def test_choose_shard_uses_effective_caps(cfg):
    accts = shard_accounts(cfg)
    ab.cache_put(cfg, "alt1", usage(30, 30, H2, D4))
    ab.cache_put(cfg, "alt2", usage(10, 20, H2, H12))
    ab.write_caps(cfg, {"alt2": 2})  # effective alt2 = floor(0.75*2) = 1
    ab.write_leases(cfg, {os.getpid(): {"account": "alt2", "started": NOW}})
    t = ab.choose_shard_account(cfg, accts, NOW)
    assert t.name != "alt2"  # alt2 at its effective cap (1) -> spill


def test_choose_shard_cold_cache_falls_back_to_least_loaded(cfg):
    accts = shard_accounts(cfg)  # no usage cache written -> all "no data yet"
    ab.write_leases(cfg, {os.getpid(): {"account": "alt1", "started": NOW}})
    t = ab.choose_shard_account(cfg, accts, NOW)
    assert t.name != "alt1"  # never the already-loaded account
