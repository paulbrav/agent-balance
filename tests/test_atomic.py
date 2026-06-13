"""atomic_write — the temp file is created 0600, so a credentials blob is
never momentarily world/group-readable before the final chmod (ISSUE 2)."""

import stat

import agent_balance as ab


def test_atomic_write_creds_mode_is_0600(tmp_path):
    p = tmp_path / "creds"
    ab.atomic_write(p, b"secret")  # default mode = 0o600
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_atomic_write_0600_never_group_or_other_readable(tmp_path):
    p = tmp_path / "creds"
    ab.atomic_write(p, b"secret", mode=0o600)
    m = p.stat().st_mode
    assert not (
        m & stat.S_IRGRP
        or m & stat.S_IROTH
        or m & stat.S_IWGRP
        or m & stat.S_IWOTH
    )


def test_atomic_write_widens_to_0644_for_nonsecret(tmp_path):
    p = tmp_path / "cache"
    ab.atomic_write(p, b"not a secret", mode=0o644)
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_atomic_write_replaces_existing(tmp_path):
    p = tmp_path / "blob"
    p.write_bytes(b"old")
    ab.atomic_write(p, b"new")
    assert p.read_bytes() == b"new"
