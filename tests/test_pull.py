"""Rebalance pull — proactively rotate toward a markedly more urgent
account (higher required burn rate), even while the installed one is fine."""

import dataclasses

from conftest import NOW, add_account, make_fetcher, usage
from test_tick import H2, setup_installed

import agent_balance as ab

D4 = NOW + 4 * 86400  # 4 days to reset
H12 = NOW + 12 * 3600  # 12h to reset
D6 = NOW + 6 * 86400  # fresh-ish week


def pull_cfg(cfg):
    return dataclasses.replace(cfg, pull_margin=10)


def run(cfg, table):
    out = []
    rc = ab.tick(cfg, now=NOW, fetcher=make_fetcher(table), out=out.append)
    return rc, out


def test_pull_rotates_to_urgent_account(cfg):
    cfg = pull_cfg(cfg)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))

    # alt1 needs 70/4 = 17.5%/day; alt2 needs 80/0.5 = 160%/day with 12h
    # left on its week — far past the margin.
    rc, out = run(
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
    setup_installed(cfg, ab.by_name(accts, "alt1"))

    # alt1 17.5%/day vs alt2 80/6 = 13.3%/day: alt2 is LESS urgent.
    rc, out = run(
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
    setup_installed(cfg, ab.by_name(accts, "alt1"))

    # alt2 is hugely urgent but nearly walled in its 5h window: a
    # proactive swap onto an almost-walled account is refused.
    rc, out = run(
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
    setup_installed(cfg, ab.by_name(accts, "alt1"))
    state = ab.read_state(cfg, NOW)
    state["last_swap_epoch"] = NOW - 30  # swapped 30s ago, gap is 300
    ab.write_state(cfg, state)

    rc, out = run(
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
    setup_installed(cfg, ab.by_name(accts, "alt1"))
    cfg.cache.mkdir(parents=True, exist_ok=True)
    (cfg.cache / "pull-check").write_text(str(NOW - 60))  # checked recently

    # The fleet must NOT be probed: a table without alt2 would assert if
    # the pull path ran.
    rc, out = run(cfg, {"tok-alt1": usage(30, 30, H2, D4)})

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert any("ok" in line for line in out)


def test_pull_disabled_by_zero_margin(cfg):
    add_account(cfg, "alt1")  # fixture cfg has pull_margin=0
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))

    rc, out = run(cfg, {"tok-alt1": usage(30, 30, H2, D4)})
    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert not (cfg.cache / "pull-check").exists()
