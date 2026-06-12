"""tick — the swap state machine, with probes stubbed."""

import fcntl
import json
import os
import stat

from conftest import H2, HD, NOW, add_account, install_pool, run_tick, usage

import agent_balance as ab


def test_below_threshold_noop(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    pool_before = (cfg.pool / ".credentials.json").read_bytes()

    rc, out = run_tick(cfg, {"tok-alt1": usage(50, 30, H2, HD)})

    assert rc == 0
    assert any("ok" in line for line in out)
    assert (cfg.pool / ".credentials.json").read_bytes() == pool_before
    assert ab.read_state(cfg, NOW)["installed"] == "alt1"


def test_first_tick_bootstraps_and_installs_best(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")

    rc, out = run_tick(
        cfg,
        {
            "tok-alt1": usage(60, 70, H2, HD),
            "tok-alt2": usage(10, 20, H2, HD),
        },
    )

    assert rc == 0
    state = ab.read_state(cfg, NOW)
    assert state["installed"] == "alt2"
    pool_creds = cfg.pool / ".credentials.json"
    assert (
        pool_creds.read_bytes()
        == (cfg.root / "alt2" / ".credentials.json").read_bytes()
    )
    assert stat.S_IMODE(pool_creds.stat().st_mode) == 0o600
    # Sessions and skills shared with the source account.
    assert (cfg.pool / "projects").is_symlink()
    assert (cfg.pool / "projects").resolve() == (
        cfg.root / "alt2" / "projects"
    ).resolve()
    assert (
        json.loads((cfg.pool / ".claude.json").read_text())["oauthAccount"][
            "emailAddress"
        ]
        == "alt2@example.com"
    )
    assert "SWAP" in (cfg.cache / "swaps").read_text()


def test_threshold_swap_installs_best_other(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))

    rc, out = run_tick(
        cfg,
        {
            "tok-alt1": usage(90, 40, H2, HD),
            "tok-alt2": usage(20, 30, H2, HD),
        },
    )

    state = ab.read_state(cfg, NOW)
    assert state["installed"] == "alt2"
    assert state["last_swap_epoch"] == NOW
    assert "pending" not in state  # the swap journal must not outlive the swap
    assert (cfg.pool / ".credentials.json").read_bytes() == (
        cfg.root / "alt2" / ".credentials.json"
    ).read_bytes()
    assert any("swapped alt1 -> alt2" in line for line in out)


def test_roll_bypasses_min_gap(cfg):
    """A 5h-wall roll is a safety action: it must never wait out the
    hysteresis gap behind a recent optimization swap."""
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
            "tok-alt1": usage(90, 40, H2, HD),
            "tok-alt2": usage(20, 30, H2, HD),
        },
    )
    assert ab.read_state(cfg, NOW)["installed"] == "alt2"
    assert any("swapped alt1 -> alt2" in line for line in out)


def test_expired_token_bypasses_gap(cfg):
    add_account(cfg, "alt1", expires_ms=(NOW - 60) * 1000)  # already expired
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    state = ab.read_state(cfg, NOW)
    state["last_swap_epoch"] = NOW - 30
    ab.write_state(cfg, state)

    rc, out = run_tick(cfg, {"tok-alt2": usage(20, 30, H2, HD)})

    assert ab.read_state(cfg, NOW)["installed"] == "alt2"
    assert any("expired" in line for line in out)


def test_limited_probe_never_swaps_blind(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))

    rc, out = run_tick(cfg, {"tok-alt1": "limited"})

    assert any("not swapping blind" in line for line in out)
    assert ab.read_state(cfg, NOW)["installed"] == "alt1"


def test_installed_dir_deleted_hard_swaps(cfg):
    add_account(cfg, "alt2")
    # State says alt3 is installed, but no such dir exists; the pool blob
    # matches the recorded hash so harvest has nothing to identify.
    cfg.pool.mkdir(parents=True)
    (cfg.pool / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "tok-alt3",
                    "expiresAt": (NOW + 3600) * 1000,
                }
            }
        )
    )
    ab.write_state(
        cfg,
        {
            "installed": "alt3",
            "blob_sha256": ab.sha256_file(cfg.pool / ".credentials.json"),
            "last_swap_epoch": NOW - 30,
        },
    )

    rc, out = run_tick(cfg, {"tok-alt2": usage(20, 30, H2, HD)})

    assert ab.read_state(cfg, NOW)["installed"] == "alt2"
    assert any("no account installed" in line for line in out)


def test_all_infeasible_leaves_creds_in_place(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))
    pool_before = (cfg.pool / ".credentials.json").read_bytes()

    rc, out = run_tick(
        cfg,
        {
            "tok-alt1": usage(95, 40, H2, HD),
            "tok-alt2": usage(96, 99, H2, HD),
        },
    )

    assert any("no other account is feasible" in line for line in out)
    assert (cfg.pool / ".credentials.json").read_bytes() == pool_before
    assert ab.read_state(cfg, NOW)["installed"] == "alt1"


def test_concurrent_tick_skips_cleanly(cfg):
    add_account(cfg, "alt1")
    cfg.root.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.lock_file, os.O_CREAT | os.O_WRONLY, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rc, out = run_tick(cfg, {})
        assert rc == 0
        assert any("skipping" in line for line in out)
        assert not cfg.state_file.exists()
    finally:
        os.close(fd)
