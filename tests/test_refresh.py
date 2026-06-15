"""OAuth access-token refresh: the network primitive (refresh_oauth, urlopen
monkeypatched — no real network), the in-place rewrite (refresh_creds_file),
and probe()'s expired -> rotate -> probe integration. Mirrors agent-pick's
refresh so an idle account doesn't stay dark in the tray until next launch."""

import json
import urllib.error
from email.message import Message

from conftest import NOW, add_account, make_fetcher, usage
from test_fetch import patch_body, patch_raise

import agent_balance as ab


def test_refresh_oauth_parses_grant(monkeypatch):
    patch_body(
        monkeypatch,
        {"access_token": "new-tok", "refresh_token": "new-ref", "expires_in": 28800},
    )
    got = ab.refresh_oauth("old-ref", NOW)
    assert got == ("new-tok", "new-ref", int((NOW + 28800) * 1000))


def test_refresh_oauth_keeps_old_refresh_when_grant_omits_it(monkeypatch):
    patch_body(monkeypatch, {"access_token": "new-tok", "expires_in": 3600})
    got = ab.refresh_oauth("old-ref", NOW)
    assert got == ("new-tok", "old-ref", int((NOW + 3600) * 1000))


def test_refresh_oauth_none_on_http_error(monkeypatch):
    patch_raise(
        monkeypatch,
        urllib.error.HTTPError(ab.OAUTH_TOKEN_URL, 400, "Bad", Message(), None),
    )
    assert ab.refresh_oauth("dead-ref", NOW) is None


def test_refresh_oauth_none_on_missing_fields(monkeypatch):
    patch_body(monkeypatch, {"refresh_token": "r", "expires_in": 100})  # no access
    assert ab.refresh_oauth("old-ref", NOW) is None


def test_refresh_creds_file_rewrites_in_place_preserving_other_fields(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "stale",
                    "refreshToken": "old-ref",
                    "expiresAt": 1,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        )
    )

    def fake(refresh, now):
        assert refresh == "old-ref"
        return "fresh-tok", "rotated-ref", 999

    fresh = ab.refresh_creds_file(path, "old-ref", NOW, fake)
    assert fresh == ab.OauthCreds("fresh-tok", 999, "rotated-ref")

    on_disk = json.loads(path.read_text())["claudeAiOauth"]
    assert on_disk["accessToken"] == "fresh-tok"
    assert on_disk["refreshToken"] == "rotated-ref"
    assert on_disk["expiresAt"] == 999
    # Untouched fields survive the rewrite.
    assert on_disk["scopes"] == ["user:inference"]
    assert on_disk["subscriptionType"] == "max"


def test_refresh_creds_file_writes_0600(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    ab.refresh_creds_file(path, "ref", NOW, lambda r, n: ("a", "b", 1))
    assert path.stat().st_mode & 0o777 == 0o600


def test_refresh_creds_file_none_leaves_file_untouched(tmp_path):
    path = tmp_path / ".credentials.json"
    original = json.dumps({"claudeAiOauth": {"accessToken": "keep", "expiresAt": 1}})
    path.write_text(original)
    assert ab.refresh_creds_file(path, "dead", NOW, lambda r, n: None) is None
    assert path.read_text() == original  # grant failed -> nothing rewritten


def test_probe_rotates_expired_then_fetches(cfg):
    """An expired-but-refreshable account: probe rotates the token in place
    (injected refresher) and goes on to a normal usage fetch."""
    add_account(cfg, "alt1", expires_ms=(NOW - 60) * 1000)
    (acct,) = ab.discover_accounts(cfg)
    assert ab.cred_status(ab.read_oauth(acct.creds), NOW) == "expired"

    def refresher(refresh, now):
        assert refresh == "ref-alt1"
        return "fresh-alt1", "ref-alt1", int((now + 8 * 3600) * 1000)

    st = ab.probe(
        acct,
        cfg,
        NOW,
        fetcher=make_fetcher({"fresh-alt1": usage(5, 9)}),
        refresher=refresher,
    )
    assert isinstance(st, ab.Usage)
    assert (st.five, st.seven) == (5, 9)
    # The rotated token is persisted, so the account is no longer expired.
    assert ab.cred_status(ab.read_oauth(acct.creds), NOW) is None
    assert ab.read_oauth(acct.creds).token == "fresh-alt1"


def test_probe_expired_no_refresher_stays_expired(cfg):
    """Regression guard: an injected fetcher with no refresher is a hermetic
    test — probe must not reach the network, so expired stays expired."""
    add_account(cfg, "alt1", expires_ms=(NOW - 60) * 1000)
    (acct,) = ab.discover_accounts(cfg)
    assert ab.probe(acct, cfg, NOW, fetcher=make_fetcher({})) == "expired"


def test_probe_expired_failed_refresh_stays_expired(cfg):
    add_account(cfg, "alt1", expires_ms=(NOW - 60) * 1000)
    (acct,) = ab.discover_accounts(cfg)
    st = ab.probe(
        acct, cfg, NOW, fetcher=make_fetcher({}), refresher=lambda r, n: None
    )
    assert st == "expired"
