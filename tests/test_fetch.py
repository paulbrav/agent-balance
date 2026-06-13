"""fetch_usage — endpoint response parsing, and the loud guard against
silent schema drift (ISSUE 4). urlopen is monkeypatched; no real network."""

import io
import json
import urllib.error
from email.message import Message

import agent_balance as ab


class FakeResp:
    """A context-manager stand-in for urlopen's response: .read() yields the
    recorded JSON body as bytes."""

    def __init__(self, body):
        self._buf = io.BytesIO(json.dumps(body).encode())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._buf.read()


def patch_body(monkeypatch, body):
    monkeypatch.setattr(
        ab.urllib.request, "urlopen", lambda req, timeout=8: FakeResp(body)
    )


def patch_raise(monkeypatch, exc):
    def boom(req, timeout=8):
        raise exc

    monkeypatch.setattr(ab.urllib.request, "urlopen", boom)


def test_fetch_parses_usage(monkeypatch):
    patch_body(
        monkeypatch,
        {
            "five_hour": {"utilization": 42, "resets_at": 1700001234},
            "seven_day": {"utilization": 71, "resets_at": 1700099999},
        },
    )
    u = ab.fetch_usage("tok")
    assert isinstance(u, ab.Usage)
    assert (u.five, u.seven, u.r5, u.r7) == (42.0, 71.0, 1700001234, 1700099999)


def test_fetch_iso_reset_string(monkeypatch):
    patch_body(
        monkeypatch,
        {
            "five_hour": {"utilization": 10, "resets_at": "2023-11-14T22:13:54Z"},
            "seven_day": {"utilization": 20},  # no resets_at
        },
    )
    u = ab.fetch_usage("tok")
    assert isinstance(u, ab.Usage)
    assert u.r5 > 0
    assert u.r7 == 0


def test_fetch_present_but_zero_is_usage_not_error(monkeypatch):
    # ISSUE-4 regression guard: a legitimate present-but-zero utilization is a
    # real fresh account, not the renamed-keys schema drift case.
    patch_body(
        monkeypatch,
        {"five_hour": {"utilization": 0}, "seven_day": {"utilization": 0}},
    )
    u = ab.fetch_usage("tok")
    assert isinstance(u, ab.Usage)
    assert (u.five, u.seven) == (0.0, 0.0)


def test_fetch_unrecognized_shape_returns_error(monkeypatch):
    # Neither window key present => keys renamed/removed => loud "error"
    # rather than silently reading a fresh account at 0%.
    patch_body(
        monkeypatch,
        {"fiveHour": {"utilization": 12}, "sevenDay": {"utilization": 34}},
    )
    assert ab.fetch_usage("tok") == "error"


def test_fetch_429_is_limited(monkeypatch):
    patch_raise(
        monkeypatch,
        urllib.error.HTTPError(
            ab.USAGE_URL, 429, "Too Many Requests", Message(), None
        ),
    )
    assert ab.fetch_usage("tok") == "limited"


def test_fetch_500_is_error(monkeypatch):
    patch_raise(
        monkeypatch,
        urllib.error.HTTPError(ab.USAGE_URL, 500, "Server Error", Message(), None),
    )
    assert ab.fetch_usage("tok") == "error"
