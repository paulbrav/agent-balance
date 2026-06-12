"""harvest — write-back sync between the pool and canonical account dirs."""

import json

from conftest import NOW, add_account, install_pool

import agent_balance as ab


def write_pool_blob(cfg, name, expires_ms):
    (cfg.pool / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": f"tok-{name}-refreshed",
                    "refreshToken": f"ref-{name}-rotated",
                    "expiresAt": expires_ms,
                }
            }
        )
    )


def test_refreshed_pool_blob_harvested_home(cfg):
    add_account(cfg, "alt1")
    accts = ab.discover_accounts(cfg)
    state = install_pool(cfg, accts[0])
    # Claude Code refreshed the token in the pool: same email, newer expiry.
    write_pool_blob(cfg, "alt1", (NOW + 12 * 3600) * 1000)

    out = []
    state = ab.harvest(cfg, accts, state, NOW, out.append)

    home = (cfg.root / "alt1" / ".credentials.json").read_bytes()
    pool = (cfg.pool / ".credentials.json").read_bytes()
    assert home == pool
    assert state["blob_sha256"] == ab.sha256_file(cfg.pool / ".credentials.json")
    assert state["installed"] == "alt1"
    assert any("harvested" in line for line in out)


def test_stale_pool_loses_to_newer_home(cfg):
    add_account(cfg, "alt1")
    accts = ab.discover_accounts(cfg)
    state = install_pool(cfg, accts[0])
    # The account was used directly and refreshed at home.
    (cfg.root / "alt1" / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "tok-alt1-newer",
                    "expiresAt": (NOW + 12 * 3600) * 1000,
                }
            }
        )
    )

    state = ab.harvest(cfg, accts, ab.read_state(cfg, NOW), NOW, lambda *_: None)

    pool = (cfg.pool / ".credentials.json").read_bytes()
    home = (cfg.root / "alt1" / ".credentials.json").read_bytes()
    assert pool == home
    assert state["blob_sha256"] == ab.sha256_file(cfg.pool / ".credentials.json")


def test_manual_login_to_known_account_adopted(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    state = install_pool(cfg, ab.by_name(accts, "alt1"))
    # The user ran /login in a pool session and picked alt2: Claude rewrote
    # both the creds and the pool .claude.json email.
    write_pool_blob(cfg, "alt2", (NOW + 12 * 3600) * 1000)
    (cfg.pool / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "alt2@example.com"}})
    )

    out = []
    state = ab.harvest(cfg, accts, state, NOW, out.append)

    assert state["installed"] == "alt2"
    assert state["last_swap_epoch"] == NOW
    home = (cfg.root / "alt2" / ".credentials.json").read_bytes()
    assert home == (cfg.pool / ".credentials.json").read_bytes()
    assert any("adopting" in line for line in out)


def test_unknown_email_warns_once_touches_nothing(cfg):
    add_account(cfg, "alt1")
    accts = ab.discover_accounts(cfg)
    state = install_pool(cfg, accts[0])
    home_before = (cfg.root / "alt1" / ".credentials.json").read_bytes()
    write_pool_blob(cfg, "mystery", (NOW + 12 * 3600) * 1000)
    (cfg.pool / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "stranger@example.com"}})
    )

    out = []
    state = ab.harvest(cfg, accts, state, NOW, out.append)
    assert state["installed"] == "unknown"
    assert (cfg.root / "alt1" / ".credentials.json").read_bytes() == home_before
    assert any("unrecognized" in line for line in out)

    # Second pass over the same blob is silent — the hash was recorded.
    out2 = []
    ab.harvest(cfg, accts, state, NOW, out2.append)
    assert out2 == []


def test_corrupt_state_recovers_via_email(cfg):
    add_account(cfg, "alt1")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, accts[0])
    cfg.state_file.write_text("not json {{{")

    state = ab.read_state(cfg, NOW)
    assert state["installed"] == "unknown"
    state = ab.harvest(cfg, accts, state, NOW, lambda *_: None)
    assert state["installed"] == "alt1"


def test_missing_pool_is_a_noop(cfg):
    add_account(cfg, "alt1")
    accts = ab.discover_accounts(cfg)
    state = ab.read_state(cfg, NOW)
    assert ab.harvest(cfg, accts, state, NOW, lambda *_: None) == state


def journal_swap(cfg, state, target):
    """The state a tick leaves behind right before the pool writes."""
    state["pending"] = {"target": target.name, "src_sha": ab.sha256_file(target.creds)}
    ab.write_state(cfg, state)


def test_pending_swap_finishes_when_the_copy_landed(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    state = install_pool(cfg, ab.by_name(accts, "alt1"))
    alt2 = ab.by_name(accts, "alt2")
    assert alt2 is not None
    home_before = (cfg.root / "alt1" / ".credentials.json").read_bytes()
    # Crash window: journal + pool writes landed, the final state write
    # did not. The pool now holds alt2's blob under alt1's recorded state.
    journal_swap(cfg, state, alt2)
    ab.install_creds(cfg, alt2)

    out = []
    state = ab.harvest(cfg, accts, ab.read_state(cfg, NOW), NOW, out.append)

    assert state["installed"] == "alt2"
    assert "pending" not in ab.read_state(cfg, NOW)
    assert any("interrupted swap" in line for line in out)
    # The poison the journal exists to prevent: alt2's token must never be
    # synced into alt1's home as a "refresh".
    assert (cfg.root / "alt1" / ".credentials.json").read_bytes() == home_before


def test_pending_swap_cleared_when_nothing_landed(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    state = install_pool(cfg, ab.by_name(accts, "alt1"))
    pool_before = (cfg.pool / ".credentials.json").read_bytes()
    # Crash window: the journal landed but no pool write did.
    journal_swap(cfg, state, ab.by_name(accts, "alt2"))

    out = []
    state = ab.harvest(cfg, accts, ab.read_state(cfg, NOW), NOW, out.append)

    assert state["installed"] == "alt1"
    assert "pending" not in ab.read_state(cfg, NOW)
    assert (cfg.pool / ".credentials.json").read_bytes() == pool_before


def test_pending_swap_with_foreign_pool_blob_goes_unknown(cfg):
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    state = install_pool(cfg, ab.by_name(accts, "alt1"))
    alt1_home_before = (cfg.root / "alt1" / ".credentials.json").read_bytes()
    alt2_home_before = (cfg.root / "alt2" / ".credentials.json").read_bytes()
    # Crash window plus a concurrent writer: the pool blob matches neither
    # the journaled copy nor the recorded one. Ownership is ambiguous.
    journal_swap(cfg, state, ab.by_name(accts, "alt2"))
    write_pool_blob(cfg, "alt1", (NOW + 12 * 3600) * 1000)

    state = ab.harvest(cfg, accts, ab.read_state(cfg, NOW), NOW, lambda *_: None)

    assert state["installed"] == "unknown"
    assert "pending" not in ab.read_state(cfg, NOW)
    assert (cfg.root / "alt1" / ".credentials.json").read_bytes() == alt1_home_before
    assert (cfg.root / "alt2" / ".credentials.json").read_bytes() == alt2_home_before


def test_meta_is_stamped_before_creds_are_copied(cfg, monkeypatch):
    """A crash inside install_creds must leave the pool meta naming the
    incoming account — the old email over the new blob is what poisoned
    the old account's home before the reorder."""
    add_account(cfg, "alt1")
    add_account(cfg, "alt2")
    accts = ab.discover_accounts(cfg)
    install_pool(cfg, ab.by_name(accts, "alt1"))

    def boom(src, dst):
        raise OSError("crash before the creds copy")

    alt2 = ab.by_name(accts, "alt2")
    assert alt2 is not None
    monkeypatch.setattr(ab, "copy_creds", boom)
    try:
        ab.install_creds(cfg, alt2)
    except OSError:
        pass
    assert ab.meta_email(cfg.pool) == "alt2@example.com"
