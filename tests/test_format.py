"""reset_in — the reset-countdown format shared by cmd_status and the tray —
plus the soft Claude Code version self-check (ISSUE 5)."""

import subprocess

import pytest
from conftest import NOW

import agent_balance as ab


@pytest.mark.parametrize(
    ("epoch", "label"),
    [
        (0, "-"),  # unknown reset beats "now"
        (NOW - 5, "now"),
        (NOW + 59, "1m"),  # minutes are a ceiling...
        (NOW + 3599, "60m"),  # ...up to the full hour
        (NOW + 3600, "1h"),
        (NOW + 5400, "2h"),  # hours and days round, not floor
        (NOW + 86399, "24h"),
        (NOW + 2 * 86400, "2d"),
    ],
)
def test_reset_in(epoch, label):
    assert ab.reset_in(epoch, NOW) == label


def fake_run(stdout="", returncode=0, raises=None):
    """A subprocess.run stand-in returning a CompletedProcess (or raising)."""

    def run(cmd, **kwargs):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    return run


def test_version_warning_none_when_claude_off_path(monkeypatch):
    monkeypatch.setattr(
        ab.subprocess, "run", fake_run(raises=FileNotFoundError("claude"))
    )
    cv = ab.check_claude_version()
    assert cv.installed is None
    assert cv.mismatch is False
    assert ab.claude_version_warning() is None


def test_version_warning_on_mismatch(monkeypatch):
    monkeypatch.setattr(
        ab.subprocess, "run", fake_run(stdout="9.9.9 (Claude Code)\n")
    )
    cv = ab.check_claude_version()
    assert cv.installed == "9.9.9"
    assert cv.mismatch is True
    warning = ab.claude_version_warning()
    assert warning is not None
    assert "9.9.9" in warning
    assert ab.VERIFIED_CLAUDE_VERSION in warning


def test_version_warning_none_on_same_major_minor(monkeypatch):
    # A patch-level difference within the verified major.minor is not a warn.
    major, minor = ab.VERIFIED_CLAUDE_VERSION.split(".")[:2]
    monkeypatch.setattr(
        ab.subprocess, "run", fake_run(stdout=f"{major}.{minor}.999 (Claude Code)\n")
    )
    cv = ab.check_claude_version()
    assert cv.installed == f"{major}.{minor}.999"
    assert cv.mismatch is False
    assert ab.claude_version_warning() is None


def test_version_timeout_is_non_fatal(monkeypatch):
    monkeypatch.setattr(
        ab.subprocess,
        "run",
        fake_run(raises=subprocess.TimeoutExpired("claude", 5)),
    )
    cv = ab.check_claude_version()
    assert cv.installed is None
    assert cv.mismatch is False
