"""Burn-rate projection — riding the 5h window to a high threshold safely."""

import dataclasses

import agent_balance as ab
from conftest import NOW, add_account, make_fetcher, usage
from test_tick import setup_installed, H2, HD


def write_history(cfg, name, points):
    cfg.cache.mkdir(parents=True, exist_ok=True)
    (cfg.cache / f"{name}.history").write_text(
        "".join(f"{epoch} {five}\n" for epoch, five in points))


def test_burn_rate_from_history(cfg):
    write_history(cfg, "alt1", [(NOW - 600, 60), (NOW - 300, 80)])
    assert ab.burn_rate(cfg, "alt1", NOW) == (80 - 60) / 300


def test_burn_rate_needs_signal(cfg):
    assert ab.burn_rate(cfg, "alt1", NOW) == 0.0          # no history
    write_history(cfg, "alt1", [(NOW - 30, 50), (NOW, 60)])
    assert ab.burn_rate(cfg, "alt1", NOW) == 0.0          # span < 60s
    write_history(cfg, "alt1", [(NOW - 600, 80), (NOW, 10)])
    assert ab.burn_rate(cfg, "alt1", NOW) == 0.0          # window reset


def test_fast_burn_swaps_below_threshold(cfg):
    cfg = dataclasses.replace(cfg, threshold=99)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))
    # ~4%/min burn observed; 95% now + 120s lookahead projects past 100%.
    write_history(cfg, "alt1", [(NOW - 600, 55), (NOW - 60, 91)])

    out = []
    rc = ab.tick(cfg, now=NOW, fetcher=make_fetcher({
        "tok-alt1": usage(95, 40, H2, HD),
        "tok-alt2": usage(20, 30, H2, HD),
    }), out=out.append)

    assert ab.read_state(cfg, NOW)["installed"] == "alt2"
    assert any("projected past 100%" in line for line in out)


def test_slow_burn_rides_to_high_threshold(cfg):
    cfg = dataclasses.replace(cfg, threshold=99)
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))
    # Flat usage: 98% < 99 threshold and no slope -> stay put.
    write_history(cfg, "alt1", [(NOW - 600, 98), (NOW - 60, 98)])

    out = []
    ab.tick(cfg, now=NOW, fetcher=make_fetcher(
        {"tok-alt1": usage(98, 40, H2, HD)}), out=out.append)

    assert ab.read_state(cfg, NOW)["installed"] == "alt1"
    assert any("ok" in line for line in out)


def test_fresh_probes_append_history(cfg):
    add_account(cfg, "alt1")
    accts = ab.discover_accounts(cfg)
    def history():
        return [(int(e), float(f)) for e, f in
                (line.split() for line in
                 (cfg.cache / "alt1.history").read_text().splitlines())]

    ab.probe(accts[0], cfg, NOW, make_fetcher(
        {"tok-alt1": usage(42, 10, H2, HD)}))
    assert history() == [(NOW, 42.0)]
    # A cache hit must not append a flattening duplicate.
    ab.probe(accts[0], cfg, NOW + 10, make_fetcher({}))
    assert history() == [(NOW, 42.0)]