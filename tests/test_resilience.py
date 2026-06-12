"""Rate-limited endpoint: stale fallback, cooldown, extrapolation."""

import json

from conftest import NOW, add_account, make_fetcher, usage
from test_tick import H2, HD, setup_installed

import agent_balance as ab


def seed_cache(cfg, name, u, epoch):
    cfg.cache.mkdir(parents=True, exist_ok=True)
    (cfg.cache / name).write_text(
        json.dumps(
            {"epoch": epoch, "five": u.five, "seven": u.seven, "r5": u.r5, "r7": u.r7}
        )
    )


def test_limited_falls_back_to_stale(cfg):
    add_account(cfg, "alt1")
    (acct,) = ab.discover_accounts(cfg)
    seed_cache(cfg, "alt1", usage(60, 30, H2, HD), NOW - 300)

    st = ab.probe(acct, cfg, NOW, lambda token: "limited")

    assert isinstance(st, ab.Usage)
    assert (st.five, st.asof) == (60, NOW - 300)
    assert (cfg.cache / "alt1.limited").exists()  # cooldown recorded


def test_cooldown_skips_refetch(cfg):
    add_account(cfg, "alt1")
    (acct,) = ab.discover_accounts(cfg)
    seed_cache(cfg, "alt1", usage(60, 30, H2, HD), NOW - 300)
    (cfg.cache / "alt1.limited").write_text(str(NOW - 30))

    # A fetch would assert (empty table); the cooldown must prevent it.
    st = ab.probe(acct, cfg, NOW, make_fetcher({}))
    assert isinstance(st, ab.Usage) and st.asof == NOW - 300


def test_stale_beyond_max_returns_limited(cfg):
    add_account(cfg, "alt1")
    (acct,) = ab.discover_accounts(cfg)
    seed_cache(cfg, "alt1", usage(60, 30, H2, HD), NOW - ab.STALE_MAX - 100)

    assert ab.probe(acct, cfg, NOW, lambda token: "limited") == "limited"


def test_fresh_success_clears_cooldown(cfg):
    add_account(cfg, "alt1")
    (acct,) = ab.discover_accounts(cfg)
    (cfg.cache / "alt1").parent.mkdir(parents=True, exist_ok=True)
    (cfg.cache / "alt1.limited").write_text(str(NOW - ab.LIMITED_COOLDOWN - 10))

    st = ab.probe(acct, cfg, NOW, make_fetcher({"tok-alt1": usage(10, 10, H2, HD)}))
    assert isinstance(st, ab.Usage) and st.asof == NOW
    assert not (cfg.cache / "alt1.limited").exists()


def test_tick_extrapolates_stale_data(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    setup_installed(cfg, ab.by_name(accts, "alt1"))
    # Last good numbers are 5 minutes old at 80%; observed slope 0.05%/s
    # means ~95% now — past the 85 threshold even though the endpoint is
    # rate-limiting.
    seed_cache(cfg, "alt1", usage(80, 40, H2, HD), NOW - 300)
    (cfg.cache / "alt1.history").write_text(f"{NOW - 900} 50\n{NOW - 300} 80\n")

    out = []
    table = {"tok-alt2": usage(20, 30, H2, HD)}

    def fetcher(token):
        if token == "tok-alt1":
            return "limited"
        return table[token]

    ab.tick(cfg, now=NOW, fetcher=fetcher, out=out.append)

    assert ab.read_state(cfg, NOW)["installed"] == "alt2"
    assert any("estimated from" in line for line in out)


def test_offline_view_never_fetches(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2", expires_ms=(NOW - 60) * 1000)
    a1, a2 = ab.discover_accounts(cfg)
    seed_cache(cfg, "alt1", usage(42, 10, H2, HD), NOW - 600)

    st = ab.offline_view(a1, cfg, NOW)
    assert isinstance(st, ab.Usage) and (st.five, st.asof) == (42, NOW - 600)
    assert ab.offline_view(a2, cfg, NOW) == "expired"
    (cfg.cache / "alt1").unlink()
    assert ab.offline_view(a1, cfg, NOW) == "no data yet"
