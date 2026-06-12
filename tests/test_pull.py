"""Deadline pull — proactively rotate toward a week expiring with
allowance unused, even while the installed account is fine."""

import dataclasses

from conftest import NOW, add_account, make_fetcher, usage
from test_tick import H2, setup_installed

import agent_balance as ab

D4 = NOW + 4 * 86400  # installed week: 4 days out (~43% elapsed)
H12 = NOW + 12 * 3600  # candidate week: 12h until reset (~93% elapsed)
D3 = NOW + 3 * 86400  # too far for a 24h pull window


def pull_cfg(cfg):
    return dataclasses.replace(cfg, pull_hours=24, pull_margin=20)


def run(cfg, table):
    out = []
    rc = ab.tick(cfg, now=NOW, fetcher=make_fetcher(table), out=out.append)
    return rc, out


def test_pull_rotates_to_expiring_week(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))

    # alt1 fine (30% 5h, on pace); alt2 has 80% of its week unused with
    # 12h left -> pace ~-73 vs alt1's ~-13: well past the 20-point margin.
    rc, out = run(
        cfg,
        {
            "tok-alt1": usage(30, 30, H2, D4),
            "tok-alt2": usage(10, 20, H2, H12),
        },
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt2"
    assert any("deadline pull" in line for line in out)


def test_no_pull_when_reset_is_far(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))

    rc, out = run(
        cfg,
        {
            "tok-alt1": usage(30, 30, H2, D4),
            "tok-alt2": usage(10, 5, H2, D3),  # huge headroom, but 3 days out
        },
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert any("ok" in line for line in out)


def test_no_pull_inside_margin(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))

    # alt2's week expires soon but is nearly spent: pace -8 vs alt1's -13
    # does not clear a 20-point margin.
    rc, out = run(
        cfg,
        {
            "tok-alt1": usage(30, 30, H2, D4),
            "tok-alt2": usage(10, 85, H2, H12),
        },
    )

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"


def test_pull_check_is_rate_limited(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))
    cfg.cache.mkdir(parents=True, exist_ok=True)
    (cfg.cache / "pull-check").write_text(str(NOW - 60))  # checked recently

    # The fleet must NOT be probed: a table without alt2 would assert if
    # the pull path ran.
    rc, out = run(cfg, {"tok-alt1": usage(30, 30, H2, D4)})

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert any("ok" in line for line in out)


def test_pull_disabled_by_zero_hours(cfg):
    add_account(cfg, "alt1")  # fixture cfg has pull_hours=0
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))

    rc, out = run(cfg, {"tok-alt1": usage(30, 30, H2, D4)})
    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert not (cfg.cache / "pull-check").exists()
