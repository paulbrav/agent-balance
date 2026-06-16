#!/usr/bin/env python3
# agent-balance — swap Claude accounts beneath running sessions.
#
# A running Claude Code instance re-reads $CLAUDE_CONFIG_DIR/.credentials.json
# from disk on every turn (verified against v2.1.175), so atomically replacing
# that file mid-session reroutes the session to another account — no /login,
# no restart, no lost workflow. This tool automates that: sessions run against
# a balancer-owned pool dir ($ROOT/.active) holding a COPY of one account's
# credentials, and a periodic tick swaps in the best other account before the
# installed one hits its 5-hour limit.
#
# Layout (shared with agent-pick — see github.com/paulbrav/agent-pick):
#   ~/.claude                       the "main" account
#   ~/.claude-accounts/<name>/      one Anthropic login per directory
#   ~/.claude-accounts/.active/     the pool: launch claude with
#                                   CLAUDE_CONFIG_DIR pointing here
#
# Commands: status (default) | tick | watch | install | uninstall | launch
#
# Two rules keep the credential copies coherent:
#   - never symlink .credentials.json (Claude Code replaces it atomically,
#     which would silently turn the symlink into a regular file); swap by copy
#   - whoever holds the newest expiresAt wins: Claude Code refreshes OAuth
#     tokens in the live dir, and refresh tokens must be assumed to rotate,
#     so every tick syncs pool <-> account-home in the fresher direction

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple, NotRequired, TypedDict

VERSION = "0.4.0"
# Claude Code re-reads .credentials.json each turn; this is the version the
# hot-swap was last verified against (see the file header, README, docs).
# claude_version_warning() warns when the installed major.minor differs.
VERIFIED_CLAUDE_VERSION = "2.1.175"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# OAuth refresh-token grant: the same public Claude Code client agent-pick
# (the sister launcher) uses. Lets the balancer rotate an idle account's
# lapsed access token in place instead of leaving it dark until next launch.
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
# platform.claude.com sits behind Cloudflare browser-integrity, which 403s
# (error 1010) the default Python-urllib User-Agent — agent-pick only slips
# through because it shells out to curl. Present as the Claude Code CLI (the
# client this OAuth grant belongs to) so the refresh clears the edge; tracks
# VERIFIED_CLAUDE_VERSION so it stays current as the swap is re-verified.
# (api.anthropic.com, the usage endpoint, has no such gate.)
CLI_USER_AGENT = f"claude-cli/{VERIFIED_CLAUDE_VERSION} (external, cli)"
USAGE_TTL = 45  # seconds a successful usage probe stays cached
USAGE_STALE_AFTER = 2 * USAGE_TTL  # age before status renderers flag the data
RESET_SOON = 900  # a 5h window resetting within this is treated as open
WEEK = 7 * 86400
# The prompt cache is account-scoped with a ~5-minute TTL refreshed on each
# turn. A discretionary (rebalance) swap inside this window busts a warm
# session's cache, re-billing its whole prefix — incl. prior thinking blocks —
# as fresh input (extra quota burn + prefill latency). The soft-swap guard
# holds off while a pool session was active within CACHE_TTL; an imminent 5h
# wall ("roll") or a dead token ("hard") still swaps, cache be damned.
CACHE_TTL = 300
# Sharding's per-account concurrency cap k_a: how many live instances to
# water-fill onto one account before spilling to the next — an estimate of its
# per-minute bucket "knee" in instances. The bench showed 6 concurrent moderate
# calls on one account do NOT throttle, so the real knee is >6; this default is
# deliberately conservative (spreads a little early, costing some cache warmth,
# but never saturates). Tune via AGENT_BALANCE_INSTANCES_PER_ACCOUNT; a later
# AIMD loop can adjust it per-account from the observed 429 rate.
INSTANCES_PER_ACCOUNT = 6


# ---------------------------------------------------------------- config ---


@dataclass(frozen=True)
class Config:
    root: Path  # accounts root (agent-pick's $ROOT)
    cache: Path  # this tool's cache dir
    threshold: float  # swap when the installed account's 5h% reaches this
    min_gap: int  # seconds between threshold-driven swaps
    interval: int  # watch-loop / timer cadence
    draw: float  # 5h points a typical session is assumed to consume
    pull_margin: float  # proactive swap when another account's urgency
    #                     (%/day) beats the installed one's by this (0 = off)
    cache_ttl: int = CACHE_TTL  # hold off discretionary swaps while a pool
    #                             session was active within this many seconds
    instances_per_account: int = INSTANCES_PER_ACCOUNT  # sharding water-fill
    #                             cap k_a (live instances) before spilling

    @property
    def pool(self) -> Path:
        return self.root / ".active"

    @property
    def state_file(self) -> Path:
        return self.root / ".balancer-state.json"

    @property
    def lock_file(self) -> Path:
        return self.root / ".balancer-lock"


# Every knob make_config reads. cmd_install persists the set ones into the
# systemd unit, so the timer sees the same config as the installing shell —
# a knob missing here would silently reset to its default under the timer.
ENV_KEYS = (
    "AGENT_PICK_ROOT",
    "CLAUDE_ACCOUNTS_ROOT",
    "XDG_CACHE_HOME",
    "AGENT_BALANCE_THRESHOLD",
    "AGENT_BALANCE_MIN_GAP",
    "AGENT_BALANCE_INTERVAL",
    "AGENT_BALANCE_DRAW",
    "AGENT_BALANCE_PULL_MARGIN",
    "AGENT_BALANCE_CACHE_TTL",
    "AGENT_BALANCE_INSTANCES_PER_ACCOUNT",
)


def make_config(env: Mapping[str, str] | None = None) -> Config:
    e: Mapping[str, str] = os.environ if env is None else env

    def num(key: str, default: float) -> float:
        try:
            return float(e.get(key, ""))
        except ValueError:
            return default

    root = (
        e.get("AGENT_PICK_ROOT")
        or e.get("CLAUDE_ACCOUNTS_ROOT")
        or os.path.expanduser("~/.claude-accounts")
    )
    cache = (
        Path(e.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"))
        / "agent-balance"
    )
    return Config(
        root=Path(root),
        cache=cache,
        threshold=num("AGENT_BALANCE_THRESHOLD", 99),
        min_gap=int(num("AGENT_BALANCE_MIN_GAP", 300)),
        interval=int(num("AGENT_BALANCE_INTERVAL", 60)),
        draw=num("AGENT_BALANCE_DRAW", 10),
        pull_margin=num("AGENT_BALANCE_PULL_MARGIN", 10),
        cache_ttl=int(num("AGENT_BALANCE_CACHE_TTL", CACHE_TTL)),
        instances_per_account=int(
            num("AGENT_BALANCE_INSTANCES_PER_ACCOUNT", INSTANCES_PER_ACCOUNT)
        ),
    )


# ------------------------------------------------------------- accounts ---


@dataclass
class Account:
    name: str
    home: Path
    email: str
    capacity: float

    @property
    def creds(self) -> Path:
        return self.home / ".credentials.json"


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def meta_path(config_dir: Path) -> Path:
    """The .claude.json that goes with a config dir. The default ~/.claude
    account keeps it at ~/.claude.json (home root); every relocated
    CLAUDE_CONFIG_DIR — including the pool — keeps it inside the dir."""
    if config_dir == Path(os.path.expanduser("~/.claude")):
        return Path(os.path.expanduser("~/.claude.json"))
    return config_dir / ".claude.json"


def meta_email(config_dir: Path) -> str:
    meta = read_json(meta_path(config_dir))
    email = (meta.get("oauthAccount") or {}).get("emailAddress")
    return email if isinstance(email, str) else ""


def is_claude_dir(d: Path) -> bool:
    """Anthropic login dir, by agent-pick's discovery rules: no codex
    auth.json, no foreign agent-pick.json kind, no API-key env backend."""
    if (d / "auth.json").exists():
        return False
    kind = read_json(d / "agent-pick.json").get("kind")
    if kind not in (None, "claude"):
        return False
    base = (read_json(d / "settings.json").get("env") or {}).get("ANTHROPIC_BASE_URL")
    return not base


def discover_accounts(cfg: Config) -> list[Account]:
    """Logged-in Anthropic accounts: ~/.claude as 'main' plus every claude
    dir under the root that holds credentials. Not-yet-logged-in dirs are
    skipped — the balancer can neither probe nor install them."""
    accounts: list[Account] = []

    def add(name: str, home: Path) -> None:
        if not (home / ".credentials.json").is_file():
            return
        cap = read_json(home / "agent-pick.json").get("capacity", 1)
        if not isinstance(cap, (int, float)) or cap <= 0:
            cap = 1
        accounts.append(Account(name, home, meta_email(home), float(cap)))

    add("main", Path(os.path.expanduser("~/.claude")))
    if cfg.root.is_dir():
        for d in sorted(cfg.root.iterdir()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if d.name == "main":
                continue  # reserved for ~/.claude, same rule as agent-pick
            if is_claude_dir(d):
                add(d.name, d)
    return accounts


def by_name(accounts: list[Account], name: str) -> Account | None:
    return next((a for a in accounts if a.name == name), None)


def by_email(accounts: list[Account], email: str) -> Account | None:
    if not email:
        return None
    return next((a for a in accounts if a.email == email), None)


class OauthCreds(NamedTuple):
    token: str  # "" when missing/malformed
    expires_ms: int  # 0 when missing/malformed
    refresh: str = ""  # long-lived refresh token, "" when missing/malformed


def read_oauth(path: Path) -> OauthCreds:
    """The claudeAiOauth shape of a credentials file — the only place that
    knows it. Total: malformed fields coerce to falsy, never raise."""
    oauth = read_json(path).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    exp = oauth.get("expiresAt")
    refresh = oauth.get("refreshToken")
    return OauthCreds(
        token if isinstance(token, str) else "",
        exp if isinstance(exp, int) else 0,
        refresh if isinstance(refresh, str) else "",
    )


def cred_status(creds: OauthCreds, now: float) -> Literal["nologin", "expired"] | None:
    """'nologin' / 'expired' when the credentials can't be probed, None
    when they look usable. A missing or malformed expiresAt counts as
    expired (the field is milliseconds; the conversion lives only here)."""
    if not creds.token:
        return "nologin"
    if creds.expires_ms / 1000 <= now:
        return "expired"
    return None


# ----------------------------------------------------------- usage probe ---


@dataclass(frozen=True)
class Usage:
    five: float  # 5h window utilization %
    seven: float  # 7d window utilization %
    r5: int  # 5h reset epoch (0 = unknown)
    r7: int  # 7d reset epoch (0 = unknown)
    asof: float = 0.0  # epoch the numbers were fetched (0 = unknown/fresh)


ProbeResult = Usage | Literal["nologin", "expired", "limited", "error"]
# What status rows can hold: probe()'s ProbeResult plus offline_view's extra
# "no data yet" word (the JSON-default path renders cache-only views).
StatusView = Usage | Literal["nologin", "expired", "limited", "error", "no data yet"]


def parse_reset(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def fetch_usage(token: str) -> Usage | Literal["limited", "error"]:
    """One GET against the OAuth usage endpoint -> Usage or a status word
    ('limited' on 429, 'error' otherwise). The endpoint enforces a short
    per-IP rate limit; callers must cache."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
            "User-Agent": CLI_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return "limited" if e.code == 429 else "error"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return "error"
    # Loud guard against silent endpoint schema drift: a real response always
    # carries at least one window (present-but-zero still ships the block).
    # Both absent => keys renamed/removed => 'error' rather than mis-reading a
    # fresh account at 0% and never swapping.
    if not isinstance(data, dict) or (
        "five_hour" not in data and "seven_day" not in data
    ):
        return "error"
    five = data.get("five_hour") or {}
    seven = data.get("seven_day") or {}
    if not isinstance(five, dict):
        five = {}
    if not isinstance(seven, dict):
        seven = {}
    return Usage(
        five=float(five.get("utilization") or 0),
        seven=float(seven.get("utilization") or 0),
        r5=parse_reset(five.get("resets_at")),
        r7=parse_reset(seven.get("resets_at")),
    )


def refresh_oauth(refresh: str, now: float) -> tuple[str, str, int] | None:
    """One POST against the OAuth refresh_token grant -> (access_token,
    refresh_token, expires_ms), or None on any failure. The grant may rotate
    the refresh token; the old one is returned when it doesn't. Never raises,
    never logs the token material."""
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": OAUTH_CLIENT_ID,
        }
    ).encode()
    req = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": CLI_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        OSError,
    ):
        return None
    if not isinstance(data, dict):
        return None
    access = data.get("access_token")
    expires_in = data.get("expires_in")
    if not isinstance(access, str) or not access:
        return None
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        return None
    rotated = data.get("refresh_token")
    rotated = rotated if isinstance(rotated, str) and rotated else refresh
    return access, rotated, int((now + expires_in) * 1000)


Refresher = Callable[[str, float], tuple[str, str, int] | None]


def refresh_creds_file(
    path: Path, refresh: str, now: float, refresher: Refresher = refresh_oauth
) -> OauthCreds | None:
    """Rotate the access token of one credentials file in place, preserving
    every other field (scopes, subscriptionType, ...) and the 0600 perms.
    Returns the fresh OauthCreds, or None when the grant or the write fails —
    the on-disk file is left untouched on failure, so the surviving refresh
    token can be retried on the next sweep."""
    rotated = refresher(refresh, now)
    if rotated is None:
        return None
    access, new_refresh, expires_ms = rotated
    doc = read_json(path)
    oauth = doc.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        oauth = {}
    oauth["accessToken"] = access
    oauth["refreshToken"] = new_refresh
    oauth["expiresAt"] = expires_ms
    doc["claudeAiOauth"] = oauth
    try:
        atomic_write(path, json.dumps(doc).encode(), mode=0o600)
    except OSError:
        return None
    return OauthCreds(access, expires_ms, new_refresh)


def usage_to_cache(u: Usage) -> dict:
    """The on-disk cache shape of a Usage — the only place that knows it
    (note the asof <-> epoch rename)."""
    return {
        "epoch": u.asof,
        "five": u.five,
        "seven": u.seven,
        "r5": u.r5,
        "r7": u.r7,
    }


def usage_from_cache(entry: dict) -> Usage | None:
    """Inverse of usage_to_cache; None when a field is missing or malformed."""
    try:
        return Usage(
            entry["five"],
            entry["seven"],
            entry["r5"],
            entry["r7"],
            asof=float(entry["epoch"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def last_known(cfg: Config, name: str) -> Usage | None:
    """The most recent successful probe regardless of age — the fallback
    when the endpoint is rate-limiting. asof carries its timestamp."""
    return usage_from_cache(read_json(cfg.cache / name))


def cache_put(cfg: Config, name: str, usage: Usage) -> None:
    try:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        data = json.dumps(usage_to_cache(usage)).encode()
        atomic_write(cfg.cache / name, data, mode=0o644)
    except OSError:
        pass


STALE_MAX = 900  # serve last-known numbers up to this old when rate-limited
LIMITED_COOLDOWN = 120  # after a 429, don't re-fetch that account for this
PROBE_SPACING = 2.5  # min seconds between real fetches — bursts trip the
#                      per-IP limit faster than sustained rate does


def throttle_fetch(cfg: Config) -> None:
    """Global (cross-process, file-based) spacing between real endpoint
    fetches, so fleet sweeps go out staggered instead of as a burst."""
    marker = cfg.cache / "last-fetch"
    wait = PROBE_SPACING - (time.time() - stamp_read(marker))
    if 0 < wait <= PROBE_SPACING:
        time.sleep(wait)
    stamp_set(marker, time.time())


def probe(
    account: Account,
    cfg: Config,
    now: float,
    fetcher: Callable[[str], ProbeResult] | None = None,
    refresher: Refresher | None = None,
) -> ProbeResult:
    """Usage for one account, or a status word: nologin | expired |
    limited | error. Cache-first; staggered real fetches; on a
    rate-limited endpoint, falls back to the last known numbers (asof
    marks their age) for up to STALE_MAX seconds.

    A lapsed access token that still carries a refresh token is rotated in
    place first (what a launch would do), so an idle account probes and
    shows usage again instead of going dark. The rotation runs only on the
    real network path; an injected fetcher keeps a test hermetic unless it
    also injects a refresher."""
    creds = read_oauth(account.creds)
    word = cred_status(creds, now)
    if word == "expired" and creds.refresh:
        rf = refresher or (refresh_oauth if fetcher is None else None)
        if rf is not None:
            fresh = refresh_creds_file(account.creds, creds.refresh, now, rf)
            if fresh is not None:
                creds = fresh
                word = cred_status(creds, now)
    if word is not None:
        return word
    prev = last_known(cfg, account.name)  # read the cache once, reuse below
    if prev is not None and now - prev.asof < USAGE_TTL:
        return prev  # still inside the probe TTL — a fresh-enough hit

    def stale_or(word: Literal["nologin", "expired", "limited", "error"]):
        if prev is not None and now - prev.asof <= STALE_MAX:
            return prev
        return word

    cooldown = cfg.cache / f"{account.name}.limited"
    if now - stamp_read(cooldown) < LIMITED_COOLDOWN:
        return stale_or("limited")

    if fetcher is None:  # only the real network path is throttled
        throttle_fetch(cfg)
        result = fetch_usage(creds.token)
    else:
        result = fetcher(creds.token)
    if isinstance(result, Usage):
        result = replace(result, asof=now)
        cache_put(cfg, account.name, result)
        record_history(cfg, account.name, result, now)
        stamp_clear(cooldown)
        return result
    if result == "limited":
        stamp_set(cooldown, now)
    return stale_or(result)


def offline_view(
    account: Account, cfg: Config, now: float
) -> Usage | Literal["nologin", "expired", "no data yet"]:
    """Cache-only view for displays (the tray): Usage — possibly stale,
    asof says how stale — or a status word. Never touches the network;
    the balancer tick is the only steady fetcher on the machine."""
    word = cred_status(read_oauth(account.creds), now)
    if word is not None:
        return word
    stale = last_known(cfg, account.name)
    return stale if stale is not None else "no data yet"


def record_history(cfg: Config, name: str, usage: Usage, now: float) -> None:
    """Fresh probes only (cache hits would flatten the slope): an
    append-only 'epoch five%' series feeding burn_rate."""
    append_capped(cfg.cache / f"{name}.history", f"{int(now)} {usage.five}", 200, 100)


def burn_rate(cfg: Config, name: str, now: float, window: int = 900) -> float:
    """5h-window burn in %/second over the recent probe history; 0 when
    there isn't enough signal or usage fell (window reset)."""
    points = []
    try:
        for line in (cfg.cache / f"{name}.history").read_text().splitlines():
            epoch_s, five_s = line.split()
            epoch = int(epoch_s)
            if now - epoch <= window:
                points.append((epoch, float(five_s)))
    except (OSError, ValueError):
        return 0.0
    if len(points) < 2:
        return 0.0
    (e0, f0), (e1, f1) = points[0], points[-1]
    if e1 - e0 < 60:
        return 0.0
    return max((f1 - f0) / (e1 - e0), 0.0)


def probe_fleet(
    accounts: list[Account],
    cfg: Config,
    now: float,
    fetcher: Callable[[str], ProbeResult] | None = None,
) -> dict[str, ProbeResult]:
    """Probe every account once, keyed by name (values are Usage or a status
    word). Cache-first, so real fetches stay staggered by throttle_fetch —
    the whole-fleet sweep the rebalance pull and any swap both need."""
    return {a.name: probe(a, cfg, now, fetcher) for a in accounts}


# ------------------------------------------------------------ pick policy ---


def normalized(usage: Usage, now: float) -> Usage:
    """A window whose known reset has passed counts as 0% — it restarts on
    first use (same rule as agent-pick)."""
    five, seven = usage.five, usage.seven
    if 0 < usage.r5 <= now:
        five = 0.0
    if 0 < usage.r7 <= now:
        seven = 0.0
    return Usage(five, seven, usage.r5, usage.r7)


def urgency(u: Usage, capacity: float, now: float) -> float:
    """Required burn rate in capacity-weighted %/day: weekly allowance
    remaining divided by days until it expires. The account that needs the
    highest sustained rate to avoid wasting its week is served first —
    urgency diverges as a reset nears (recovering EDF's waste behavior)
    while level-loading earlier in the week, which keeps more accounts'
    5h windows alive for bursts. Replaces the old additive pace metric
    (used% - elapsed%), which carried no deadline information."""
    un = normalized(u, now)
    remaining = 100.0 - un.seven
    days = (u.r7 - now) / 86400 if u.r7 > now else 7.0
    return remaining / max(days, 1 / 24) * capacity


def feasible_now(u: Usage, cfg: Config, now: float) -> bool:
    """Room for a typical session in the 5h window (or an imminent reset)
    and a weekly window that isn't spent."""
    soon = 0 < u.r5 and u.r5 - now <= RESET_SOON
    return (u.five + cfg.draw < 100 or soon) and u.seven < 100


class Candidate(NamedTuple):
    account: Account
    usage: Usage  # normalized
    feasible: bool
    urgency: float


def candidates(
    accounts: list[Account],
    stats: Mapping[str, object],
    exclude: str | None,
    now: float,
    cfg: Config,
) -> list[Candidate]:
    """One policy row per account with known usage. Consumes the
    precomputed stats dict — never probes."""
    rows = []
    for a in accounts:
        stat = stats.get(a.name)
        if a.name == exclude or not isinstance(stat, Usage):
            continue
        u = normalized(stat, now)
        rows.append(
            Candidate(a, u, feasible_now(u, cfg, now), urgency(u, a.capacity, now))
        )
    return rows


def rank(c: Candidate, now: float):
    """Urgency first; ties go to the earlier weekly reset (EDF), then 5h
    headroom, then name for determinism."""
    eff_r7 = c.usage.r7 if c.usage.r7 > now else now + WEEK
    return (c.urgency, -eff_r7, 100 - c.usage.five, c.account.name)


def pick_target(
    accounts: list[Account],
    stats: dict[str, ProbeResult],
    exclude: str | None,
    now: float,
    cfg: Config,
) -> Account | None:
    """The highest-urgency feasible account (5h room for a typical
    session, weekly not spent) — the one whose remaining week needs the
    highest sustained burn rate. None when no account is feasible."""
    feas = [c for c in candidates(accounts, stats, exclude, now, cfg) if c.feasible]
    return max(feas, key=lambda c: rank(c, now)).account if feas else None


def pick_pull_target(
    accounts: list[Account],
    stats: dict[str, ProbeResult],
    installed: Account,
    inst_usage: Usage,
    cfg: Config,
    now: float,
) -> tuple[Account, float, float] | None:
    """The rebalance pull's winner, under two pull-specific rules: a
    target must keep a typical session's draw below the swap threshold
    (a proactive swap never moves onto an almost-walled account), and it
    must beat the installed account's urgency by
    max(pull_margin, 0.15 * installed urgency) — the flap hysteresis.
    Ranking is deliberately urgency-only, first account winning ties (do
    not harmonize with rank(), which would change tie outcomes).
    Returns (winner, winner urgency, installed urgency) or None."""
    u_inst = urgency(inst_usage, installed.capacity, now)
    pool = [
        c
        for c in candidates(accounts, stats, installed.name, now, cfg)
        if c.feasible and c.usage.five + cfg.draw < cfg.threshold
    ]
    best = max(pool, key=lambda c: c.urgency, default=None)
    if best is None or best.urgency < u_inst + max(cfg.pull_margin, 0.15 * u_inst):
        return None
    return best.account, best.urgency, u_inst


def pick_shard_target(
    accounts: list[Account],
    stats: Mapping[str, object],
    load: Mapping[str, int],
    cap: int,
    now: float,
    cfg: Config,
) -> Account | None:
    """The sharding allocator: WATER-FILLING. Among feasible accounts, prefer
    the highest-urgency one that still has per-minute bucket headroom (fewer
    than `cap` live instances) — so load fills the most-urgent account up to
    its knee, then spills to the next, burning weekly allowances in urgency
    order while never saturating one bucket and spreading only as much as
    needed (minimal cache fragmentation). When every feasible account is at
    cap, fall back to the least-loaded one so an extra instance still lands on
    the emptiest bucket. None when no account is feasible (the caller then
    falls back to a blind least-loaded pick)."""
    feas = [c for c in candidates(accounts, stats, None, now, cfg) if c.feasible]
    if not feas:
        return None
    under = [c for c in feas if load.get(c.account.name, 0) < cap]
    if under:
        return max(under, key=lambda c: rank(c, now)).account
    return min(
        feas, key=lambda c: (load.get(c.account.name, 0), -c.urgency, c.account.name)
    ).account


def choose_shard_account(
    cfg: Config, accounts: list[Account], now: float
) -> Account:
    """Pick the account for a launching instance: GC dead leases, read n_a from
    the survivors, and water-fill (cached usage only — never blocks a launch on
    the network). When usage is unknown (cold cache / balancer not running),
    fall back to the least-loaded account so a launch still spreads and never
    fails."""
    leases = gc_leases(cfg)
    load = account_load(leases)
    stats = {a.name: offline_view(a, cfg, now) for a in accounts}
    target = pick_shard_target(
        accounts, stats, load, cfg.instances_per_account, now, cfg
    )
    if target is None:
        target = min(accounts, key=lambda a: (load.get(a.name, 0), a.name))
    return target


# ------------------------------------------------------ blobs and state ---


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write atomically. The temp is created 0600 by mkstemp, so a
    credentials blob is never momentarily world/group-readable at umask
    perms before the chmod; mode then sets the final bits (creds 0600,
    cache/state/meta 0644)."""
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def copy_creds(src: Path, dst: Path) -> str:
    """Atomic credentials copy; returns the blob's sha256."""
    data = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dst, data)
    return hashlib.sha256(data).hexdigest()


def stamp_read(path: Path) -> float:
    """Epoch stored in a marker file; 0.0 when absent or malformed."""
    try:
        return float(path.read_text())
    except (OSError, ValueError):
        return 0.0


def stamp_set(path: Path, value: float) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(value))
    except OSError:
        pass


def stamp_clear(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def append_capped(path: Path, line: str, cap: int, keep: int) -> None:
    """Append one line; once the file exceeds cap lines, atomically trim
    it to the newest keep."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(line + "\n")
        lines = path.read_text().splitlines()
        if len(lines) > cap:
            atomic_write(path, ("\n".join(lines[-keep:]) + "\n").encode(), mode=0o644)
    except OSError:
        pass


class PendingSwap(TypedDict):
    target: str
    src_sha: str


class State(TypedDict):
    installed: str
    blob_sha256: str
    last_swap_epoch: int
    pending: NotRequired[PendingSwap]


def read_state(cfg: Config, now: float) -> State:
    state = read_json(cfg.state_file)
    installed = state.get("installed")
    sha = state.get("blob_sha256")
    epoch = state.get("last_swap_epoch")
    if not isinstance(installed, str) or not installed:
        installed = "unknown"
    if not isinstance(sha, str):
        sha = ""
    if not isinstance(epoch, (int, float)) or epoch < 0:
        epoch = 0
    result: State = {
        "installed": installed,
        "blob_sha256": sha,
        "last_swap_epoch": min(int(epoch), int(now)),
    }
    pending = state.get("pending")  # in-flight swap journal (see tick)
    if (
        isinstance(pending, dict)
        and isinstance(pending.get("target"), str)
        and isinstance(pending.get("src_sha"), str)
    ):
        result["pending"] = {
            "target": pending["target"],
            "src_sha": pending["src_sha"],
        }
    return result


def write_state(cfg: Config, state: Mapping[str, object]) -> None:
    # Serializer only — accepts a State (production callers build one) or any
    # state-shaped mapping (test fixtures). The State shape is enforced at the
    # construction sites (read_state/tick/harvest/commit_swap), not here, so a
    # Mapping param keeps tests/ type-checked without an exclude.
    cfg.root.mkdir(parents=True, exist_ok=True)
    atomic_write(cfg.state_file, json.dumps(state, indent=2).encode(), mode=0o644)


def log_swap(cfg: Config, now: float, line: str) -> None:
    append_capped(cfg.cache / "swaps", f"{int(now)} {line}", 2000, 1000)


def commit_swap(state: State, installed: str, blob_sha: str, now: float) -> None:
    """Record a completed swap into state, in place: clear any pending
    journal, name the new installed account, stamp its blob and epoch. The
    one shape both tick's happy path and harvest's crash-recovery write, so
    the two can't drift. (Does not persist — the caller write_states.)"""
    state.pop("pending", None)
    state["installed"] = installed
    state["blob_sha256"] = blob_sha
    state["last_swap_epoch"] = int(now)


# --------------------------------------------------------------- leases ---
# A sharded launch pins one instance to one account for its life. The lease
# registry (pid -> account) is how the allocator knows n_a (live instances per
# account) for water-filling, and how the tick reclaims an account when an
# instance exits. Keyed by PID and GC'd by liveness — os.execvpe annihilates
# the launcher's atexit handlers, so a lease can only be cleaned by checking
# whether its PID is still alive, never by a release hook.


def lease_file(cfg: Config) -> Path:
    return cfg.cache / "leases.json"


def read_leases(cfg: Config) -> dict[int, dict]:
    """pid -> {account, started}. Malformed entries are dropped, never raise."""
    raw = read_json(lease_file(cfg))
    out: dict[int, dict] = {}
    for k, v in raw.items():
        try:
            pid = int(k)
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict) and isinstance(v.get("account"), str):
            started = v.get("started")
            out[pid] = {
                "account": v["account"],
                "started": int(started) if isinstance(started, (int, float)) else 0,
            }
    return out


def write_leases(cfg: Config, leases: Mapping[int, dict]) -> None:
    cfg.cache.mkdir(parents=True, exist_ok=True)
    data = {str(pid): v for pid, v in leases.items()}
    atomic_write(lease_file(cfg), json.dumps(data, indent=2).encode(), mode=0o644)


def pid_alive(pid: int) -> bool:
    """Whether a PID names a live process. A permission error means it exists
    but isn't ours (still alive); no-such-process means dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def gc_leases(cfg: Config, leases: dict[int, dict] | None = None) -> dict[int, dict]:
    """Drop leases whose process has exited; persist only if something changed.
    Returns the live set."""
    leases = read_leases(cfg) if leases is None else leases
    live = {pid: v for pid, v in leases.items() if pid_alive(pid)}
    if len(live) != len(leases):
        write_leases(cfg, live)
    return live


def account_load(leases: Mapping[int, dict]) -> dict[str, int]:
    """Live instance count per account name — the n_a the allocator caps on."""
    load: dict[str, int] = {}
    for v in leases.values():
        load[v["account"]] = load.get(v["account"], 0) + 1
    return load


def add_lease(cfg: Config, pid: int, account: str, now: float) -> None:
    """Record a sharded instance's account, GC'ing dead leases first so the
    file never grows without bound."""
    leases = gc_leases(cfg)
    leases[pid] = {"account": account, "started": int(now)}
    write_leases(cfg, leases)


# ------------------------------------------------- pool install / harvest ---


def install_creds(cfg: Config, account: Account) -> str:
    """Copy the account's credentials into the pool, stamping its identity
    into the pool's .claude.json FIRST: a crash between the two writes must
    leave the meta naming the incoming account, never the outgoing one —
    harvest's identity pass keys off that email, and a stale email would
    make it sync the new account's token into the old account's home."""
    cfg.pool.mkdir(parents=True, exist_ok=True)
    oauth_account = read_json(meta_path(account.home)).get("oauthAccount")
    pool_meta_path = cfg.pool / ".claude.json"
    pool_meta = read_json(pool_meta_path)
    if oauth_account:
        pool_meta["oauthAccount"] = oauth_account
    else:
        pool_meta.pop("oauthAccount", None)  # never keep the previous identity
    atomic_write(pool_meta_path, json.dumps(pool_meta, indent=2).encode(), mode=0o644)
    return copy_creds(account.creds, cfg.pool / ".credentials.json")


def bootstrap_pool(cfg: Config, source: Account, out=print) -> None:
    """First-time pool setup: share sessions and skills with the source
    account (resolved through any agent-pick --sync links to the true
    canonical dirs) and copy its settings/meta wholesale so onboarding
    state, MCP servers, and model prefs carry over."""
    cfg.pool.mkdir(parents=True, exist_ok=True)
    for sub in ("projects", "skills"):
        src, dst = source.home / sub, cfg.pool / sub
        if dst.exists() or dst.is_symlink():
            continue
        if sub == "projects":
            src.mkdir(parents=True, exist_ok=True)
        if src.exists():
            dst.symlink_to(src.resolve())
    for src, dst in (
        (meta_path(source.home), cfg.pool / ".claude.json"),
        (source.home / "settings.json", cfg.pool / "settings.json"),
    ):
        if src.is_file() and not dst.exists():
            atomic_write(dst, src.read_bytes(), mode=0o644)
    out(f"agent-balance: bootstrapped pool {cfg.pool} from {source.name}")


def harvest(
    cfg: Config, accounts: list[Account], state: State, now: float, out=print
) -> State:
    """Reconcile the pool with the canonical account dirs before deciding
    anything. A pending-swap journal entry (a tick died mid-swap) is
    settled first and ends the pass — the email heuristic and the
    freshness sync must never run against a half-applied swap. Then two
    passes:

    1. Identify: a pool blob whose hash differs from the recorded one was
       written by someone else — Claude Code refreshing the token, or a
       manual /login. Identify the owner by the email /login writes into
       the pool's .claude.json; adopt a known account, warn once on an
       unknown one.
    2. Freshness: whichever side (pool or the installed account's home)
       holds the newer expiresAt wins, and the older side is overwritten.
       This both harvests refreshed tokens home (refresh tokens must be
       assumed to rotate) and refreshes a stale pool copy after the
       account was used directly."""
    pool_creds = cfg.pool / ".credentials.json"
    if not pool_creds.is_file():
        return state

    pending = state.pop("pending", None)
    if pending is not None:
        pool_sha = sha256_file(pool_creds)
        if pool_sha == pending["src_sha"]:
            # The journaled copy landed before the crash: finish the commit.
            # pending was already popped above, so commit_swap's pop is a no-op.
            commit_swap(state, pending["target"], pool_sha, now)
            out(f"agent-balance: finished interrupted swap to {pending['target']}")
        elif pool_sha != state["blob_sha256"]:
            # Neither the old blob nor the journaled one — someone else
            # wrote the pool during the crash window. Identity is
            # ambiguous; never copy anything home from here.
            state["installed"] = "unknown"
            state["blob_sha256"] = pool_sha
            out(
                "agent-balance: pool changed during an interrupted swap; "
                "credentials will be replaced on the next swap"
            )
        # else: the swap never started writing — the journal is just stale.
        write_state(cfg, state)
        return state

    pool_sha = sha256_file(pool_creds)
    if pool_sha != state["blob_sha256"]:
        pool_email = meta_email(cfg.pool)
        installed = by_name(accounts, state["installed"])
        if installed is not None and pool_email and pool_email == installed.email:
            pass  # token refresh by Claude Code; freshness pass syncs it home
        else:
            owner = by_email(accounts, pool_email)
            if owner is not None:
                out(f"agent-balance: adopting /login of {owner.name} ({pool_email})")
                state["installed"] = owner.name
                state["last_swap_epoch"] = int(now)
            else:
                if state["installed"] != "unknown" or not state["blob_sha256"]:
                    out(
                        "agent-balance: pool credentials belong to an "
                        f"unrecognized account ({pool_email or 'no email'}); "
                        "they will be replaced on the next swap"
                    )
                state["installed"] = "unknown"
        state["blob_sha256"] = pool_sha
        write_state(cfg, state)

    installed = by_name(accounts, state["installed"])
    if installed is not None:
        pool_exp = read_oauth(pool_creds).expires_ms
        home_exp = read_oauth(installed.creds).expires_ms
        if pool_exp > home_exp:
            copy_creds(pool_creds, installed.creds)
            out(
                f"agent-balance: harvested refreshed token of "
                f"{installed.name} back home"
            )
        elif home_exp > pool_exp:
            state["blob_sha256"] = copy_creds(installed.creds, pool_creds)
            write_state(cfg, state)
            out(
                f"agent-balance: refreshed pool copy of {installed.name} "
                "from its home dir"
            )
    return state


# ------------------------------------------------------------------ tick ---


class BalancerLock:
    def __init__(self, cfg: Config, wait: float = 0.0):
        self.path, self.wait = cfg.lock_file, wait
        self.fd = None

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o644)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(self.fd)
                    self.fd = None
                    return False
                time.sleep(0.25)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)  # closing drops the flock


# Seconds between whole-fleet probes (the rebalance pull, and how stale the
# tray's non-installed rows get). With a 4-account fleet this adds ~0.6
# req/min to the installed account's 1/min — comfortably under the usage
# endpoint's measured ~2-2.5 req/min per-IP allowance.
PULL_CHECK = 300


def pull_due(cfg: Config, now: float) -> bool:
    """The rebalance pull needs the whole fleet probed; rate-limit that to
    once per PULL_CHECK so a steady tick stays well under the endpoint's
    per-IP budget."""
    stamp = cfg.cache / "pull-check"
    if now - stamp_read(stamp) < PULL_CHECK:
        return False
    stamp_set(stamp, int(now))
    return True


def pool_session_age(cfg: Config, now: float) -> float | None:
    """Seconds since the most recent turn written to the pool's transcripts,
    or None when none is found. Claude Code appends to projects/**/*.jsonl
    every turn, so the newest mtime tracks live session activity — the signal
    the soft-swap guard uses to avoid swapping a warm prompt cache out for a
    discretionary rebalance. Fail-soft: any error reads as 'no live session'."""
    newest = 0.0
    try:
        for p in (cfg.pool / "projects").rglob("*.jsonl"):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if m > newest:
                newest = m
    except OSError:
        return None
    if newest == 0.0:
        return None
    return max(now - newest, 0.0)


def tick(
    cfg: Config,
    now: float | None = None,
    fetcher: Callable[[str], ProbeResult] | None = None,
    out=print,
    lock_wait: float = 0.0,
) -> int:
    """One idempotent balance pass. Never raises for operational
    conditions — every outcome is one printed line."""
    now = time.time() if now is None else now
    accounts = discover_accounts(cfg)
    if not accounts:
        out(
            "agent-balance: no logged-in Anthropic accounts found "
            f"(~/.claude or {cfg.root}/<name>/)"
        )
        return 0

    with BalancerLock(cfg, wait=lock_wait) as held:
        if not held:
            out("agent-balance: another tick holds the lock, skipping")
            return 0

        live = gc_leases(cfg)  # reap leases of exited sharded instances
        ledger_throttles(cfg, accounts, now, live)  # log new 429s with n_a
        state = read_state(cfg, now)
        state = harvest(cfg, accounts, state, now, out)
        installed = by_name(accounts, state["installed"])

        need: Literal["hard", "roll", "soft"] | None = None
        reason: str | None = None
        five_now = 0.0
        est = ""
        # the installed probe, or a status word; None when uninstalled
        st: ProbeResult | None = None
        if installed is None:
            need, reason = "hard", "no account installed"
        else:
            st = probe(installed, cfg, now, fetcher)
            if st in ("expired", "nologin"):
                need, reason = "hard", f"installed token {st}"
            elif st in ("limited", "error"):
                out(
                    f"agent-balance: usage probe of {installed.name} is "
                    f"{st}; not swapping blind"
                )
                return 0
            else:
                five_now = normalized(st, now).five
                # Projection guard: a high threshold is only safe if a fast
                # burn can't blow through the blind spot between probes —
                # swap early when the recent slope says 100% lands within
                # two tick intervals. Stale numbers (rate-limited endpoint
                # served last-known data) get extrapolated by the same slope
                # first, so a blind spot doesn't freeze the picture.
                rate = burn_rate(cfg, installed.name, now)
                age = now - st.asof if st.asof else 0.0
                if age > USAGE_TTL:
                    five_now = min(five_now + rate * age, 100.0)
                    est = f" (estimated from {age / 60:.0f}m-old data)"
                lookahead = 2 * cfg.interval
                if five_now >= cfg.threshold:
                    need = "roll"
                    reason = f"5h at {five_now:.0f}%{est} >= {cfg.threshold:.0f}"
                elif five_now + rate * lookahead >= 100:
                    need = "roll"
                    reason = (
                        f"5h at {five_now:.0f}% burning "
                        f"{rate * 60:.1f}%/min — projected past 100% "
                        f"within {lookahead}s{est}"
                    )

        # Rebalance pull: even when the installed account is fine, rotate
        # toward a markedly more urgent account — one whose remaining week
        # needs a much higher burn rate to avoid expiring unused. Sessions
        # don't notice the swap. The margin is the hysteresis: after the
        # pull, no other account clears the bar against the new installed.
        stats: dict[str, ProbeResult] | None = None
        pulled: Account | None = None
        if (
            need is None
            and installed is not None
            and isinstance(st, Usage)
            and cfg.pull_margin > 0
            and pull_due(cfg, now)
        ):
            stats = probe_fleet(accounts, cfg, now, fetcher)
            pull = pick_pull_target(accounts, stats, installed, st, cfg, now)
            if pull is not None:
                pulled, u_best, u_inst = pull
                need = "soft"
                reason = (
                    f"rebalance: {pulled.name} needs {u_best:.0f}%/day to clear "
                    f"its week vs {u_inst:.0f}%/day for {installed.name}"
                )

        if need is None:
            assert installed is not None  # need would be "hard" otherwise
            out(f"agent-balance: {installed.name} at {five_now:.0f}% 5h{est} — ok")
            return 0
        # min_gap and the cache-warmth hold damp optimization churn only — a
        # 5h-wall roll ("roll") or a dead token ("hard") must never wait out a
        # hysteresis gap behind a routine rebalance swap.
        if need == "soft":
            warm = pool_session_age(cfg, now)
            if warm is not None and warm < cfg.cache_ttl:
                out(
                    f"agent-balance: rebalance due ({reason}) but a pool "
                    f"session was active {warm:.0f}s ago — holding to keep its "
                    "prompt cache warm"
                )
                return 0
            if now - state["last_swap_epoch"] < cfg.min_gap:
                out(
                    f"agent-balance: swap due ({reason}) but inside the "
                    f"{cfg.min_gap}s gap since the last one"
                )
                return 0

        if stats is None:
            stats = probe_fleet(accounts, cfg, now, fetcher)
        if pulled is not None:
            target = pulled
        else:
            exclude = installed.name if installed is not None else None
            target = pick_target(accounts, stats, exclude, now, cfg)
        if target is None:
            out(
                f"agent-balance: swap due ({reason}) but no other account "
                "is feasible; leaving credentials in place"
            )
            return 0

        # Journal the swap before touching the pool: if we die between the
        # pool writes and the final state write, the next tick's harvest
        # settles the journal instead of misreading the half-applied pool.
        state["pending"] = {
            "target": target.name,
            "src_sha": sha256_file(target.creds),
        }
        write_state(cfg, state)
        if not cfg.pool.is_dir():
            bootstrap_pool(cfg, target, out)
        sha = install_creds(cfg, target)
        prev = state["installed"]
        commit_swap(state, target.name, sha, now)
        write_state(cfg, state)
        log_swap(cfg, now, f"SWAP {prev} {target.name} {five_now:.0f} {reason}")
        u = stats.get(target.name)
        pct = f" — 5h {u.five:.0f}%, 7d {u.seven:.0f}%" if isinstance(u, Usage) else ""
        out(
            f"agent-balance: swapped {prev} -> {target.name} "
            f"({target.email}){pct} [{reason}]"
        )
        return 0


# ------------------------------------------------------------- commands ---


def reset_in(epoch: int, now: float) -> str:
    """Compact reset countdown — shared by cmd_status and the tray."""
    if epoch == 0:
        return "-"
    s = epoch - now
    if s <= 0:
        return "now"
    if s < 3600:
        return f"{int(s // 60) + 1}m"
    if s < 86400:
        return f"{round(s / 3600)}h"
    return f"{round(s / 86400)}d"


def stale_age_min(u: Usage, now: float) -> float | None:
    """Age of u in minutes, but only once it crosses the display-staleness
    threshold; None when fresh (or asof unknown). The one place that decides
    "show this as stale" — shared by cmd_status and the tray."""
    if u.asof and now - u.asof > USAGE_STALE_AFTER:
        return (now - u.asof) / 60
    return None


# ----------------------------------------------- Claude Code version check ---


class ClaudeVersion(NamedTuple):
    verified: str
    installed: str | None  # None when claude not on PATH / unparseable
    mismatch: bool


def _major_minor(version: str) -> tuple[int, int] | None:
    token = version.strip().split()[0] if version.strip() else ""
    parts = token.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def check_claude_version(timeout: float = 5) -> ClaudeVersion:
    """Run `claude --version` (short timeout, non-fatal). Returns a
    ClaudeVersion; installed=None and mismatch=False on any failure (not on
    PATH, timeout, non-zero, unparseable). Never raises."""
    installed_str: str | None = None
    try:
        r = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=timeout
        )
        if r.returncode == 0:
            installed_str = r.stdout.strip().split()[0] if r.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    inst = _major_minor(installed_str or "")
    ver = _major_minor(VERIFIED_CLAUDE_VERSION)
    mismatch = inst is not None and ver is not None and inst != ver
    return ClaudeVersion(VERIFIED_CLAUDE_VERSION, installed_str, mismatch)


def claude_version_warning_from(cv: ClaudeVersion) -> str | None:
    """Format the soft mismatch warning from an already-gathered
    ClaudeVersion (no subprocess); None when there is nothing to warn about."""
    if not cv.mismatch:
        return None
    return (
        f"agent-balance: Claude Code {cv.installed} differs from the verified "
        f"{cv.verified}; the hot-swap relies on Claude re-reading "
        ".credentials.json each turn — verify swaps still take effect."
    )


def claude_version_warning(timeout: float = 5) -> str | None:
    """Thin string wrapper over check_claude_version for the install path."""
    return claude_version_warning_from(check_claude_version(timeout))


# ----------------------------------------------------- status: gather/render ---


def query_timer() -> str:
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "agent-balance.timer"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return r.stdout.strip() or "inactive"
    except (OSError, subprocess.TimeoutExpired):
        return "no systemd"


def read_swap_tail(cfg: Config) -> list[str]:
    swaps = cfg.cache / "swaps"
    if swaps.is_file():
        return swaps.read_text().splitlines()[-5:]
    return []


# ------------------------------------------------------------- metrics ---
# Read-only instruments. They observe throttling and swap churn; they are
# never control inputs (the balancer must not steer on its own transcripts).

# The literal line Claude Code logs on per-minute THROUGHPUT throttling — the
# "(not your usage limit)" is the API distinguishing it from the 5h/7d wall.
THROTTLE_MARKER = "Server is temporarily limiting requests"


def scan_throttle_events(
    homes: list[Path], now: float, window: int = 3600, lookback: int = 86400
) -> dict:
    """Count per-minute throughput throttling in Claude Code transcripts.
    Scans projects/**/*.jsonl under each account home for THROTTLE_MARKER
    (distinct from the usage wall). Only files modified within `lookback` are
    read; `recent` counts hits in files touched within `window`. Read-only and
    fail-soft — transcripts are never modified, any error skips that file.
    Bounded by lookback, and this is NOT a silent cap: the numbers are
    explicitly 'within the last lookback/window seconds'."""
    total = recent = files = 0
    seen: set[Path] = set()
    for home in homes:
        proj = home / "projects"
        try:
            resolved = proj.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            paths = list(proj.rglob("*.jsonl"))
        except OSError:
            continue
        for p in paths:
            try:
                mtime = p.stat().st_mtime
                if now - mtime > lookback:
                    continue
                hits = p.read_text(errors="replace").count(THROTTLE_MARKER)
            except OSError:
                continue
            if hits:
                total += hits
                files += 1
                if now - mtime <= window:
                    recent += hits
    return {
        "total": total,
        "recent": recent,
        "files": files,
        "window_s": window,
        "lookback_s": lookback,
    }


def scan_new_throttles(
    home: Path, since: float, now: float, lookback: int = 86400
) -> int:
    """Count THROTTLE_MARKER hits in one account's transcripts whose file mtime
    is in (since, now] — the NEW 429s since the last scan. Files older than
    `lookback` are skipped. Read-only and fail-soft. The mtime gate (not a byte
    cursor — transcripts get rewritten/rotated) is what makes the ledger
    idempotent: once the per-account seen-epoch is advanced to `now`, a file is
    re-counted only when Claude Code writes to it again (a new turn / 429)."""
    hits = 0
    try:
        paths = list((home / "projects").rglob("*.jsonl"))
    except OSError:
        return 0
    for p in paths:
        try:
            mtime = p.stat().st_mtime
            if mtime <= since or now - mtime > lookback:
                continue
            hits += p.read_text(errors="replace").count(THROTTLE_MARKER)
        except OSError:
            continue
    return hits


def record_throttles(
    cfg: Config, home: Path, name: str, n_a: int, now: float
) -> int:
    """Fold one account's NEW transcript 429s into throttle_ledger.jsonl, one
    row stamped with the current live-instance count n_a, then advance the
    per-account seen-epoch. Returns the new-hit count. Fail-soft. Reads INSTANCE
    transcripts ONLY — never the balancer's own <name>.limited usage-probe flag
    (a different traffic stream, single-overwritten, no concurrency tag: the
    wrong signal for the throughput knee). On the very first scan it just starts
    the clock (no backfill of historical 429s, whose n_a is unknown)."""
    seen_path = cfg.cache / f"{name}.throttle-seen"
    since = stamp_read(seen_path)
    if since <= 0:
        stamp_set(seen_path, now)
        return 0
    hits = scan_new_throttles(home, since, now)
    if hits:
        row = {"epoch": int(now), "account": name, "n_a": n_a, "hits": hits}
        append_capped(
            cfg.cache / "throttle_ledger.jsonl", json.dumps(row), 2000, 1000
        )
    stamp_set(seen_path, now)
    return hits


def ledger_throttles(
    cfg: Config,
    accounts: list[Account],
    now: float,
    leases: dict[int, dict] | None = None,
) -> None:
    """For each account, fold new transcript 429s into the throttle ledger,
    stamped with that account's current live-instance count — turning the
    censored knee into two-sided-identifiable (account, n_a)-at-429 data. The
    only writer of the ledger. Reuses an already-GC'd lease map when given (the
    tick passes its gc_leases result) to avoid a double GC. Never raises."""
    load = account_load(leases if leases is not None else gc_leases(cfg))
    for a in accounts:
        record_throttles(cfg, a.home, a.name, load.get(a.name, 0), now)


def read_throttle_ledger(cfg: Config, now: float, window: int = 86400) -> dict:
    """Recent throttle-ledger summary for the metrics block: rows + total hits
    within `window`, and the max n_a observed at a 429 — the empirical lower
    bound on the per-account throughput knee. Fail-soft -> zeros."""
    rows = hits = max_n_a = 0
    try:
        text = (cfg.cache / "throttle_ledger.jsonl").read_text()
    except OSError:
        return {"rows": 0, "hits": 0, "max_n_a": 0, "window_s": window}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        epoch = row.get("epoch")
        if not isinstance(epoch, (int, float)) or now - epoch > window:
            continue
        rows += 1
        h, n = row.get("hits"), row.get("n_a")
        hits += int(h) if isinstance(h, (int, float)) else 0
        if isinstance(n, (int, float)):
            max_n_a = max(max_n_a, int(n))
    return {"rows": rows, "hits": hits, "max_n_a": max_n_a, "window_s": window}


def swap_churn(cfg: Config, now: float, ttl: int, window: int = 86400) -> dict:
    """From the swap log: swaps in the recent `window`, and how many landed
    within `ttl` of the prior swap — a cache-warmth-bust proxy, since
    back-to-back swaps re-bill a session's prefix. Fail-soft."""
    epochs: list[int] = []
    try:
        for line in (cfg.cache / "swaps").read_text().splitlines():
            try:
                epochs.append(int(line.split()[0]))
            except (ValueError, IndexError):
                continue
    except OSError:
        return {"recent": 0, "warm_busts": 0, "ttl_s": ttl, "window_s": window}
    epochs.sort()
    recent = busts = 0
    prev: int | None = None
    for e in epochs:
        if now - e <= window:
            recent += 1
            if prev is not None and 0 <= e - prev < ttl:
                busts += 1
        prev = e
    return {"recent": recent, "warm_busts": busts, "ttl_s": ttl, "window_s": window}


def gather_metrics(cfg: Config, accounts: list[Account], now: float) -> dict:
    """The status `metrics` block: throughput-throttle events seen across the
    managed accounts' transcripts, swap churn, and how recently a pool session
    was active. Read-only; an instrument, never a control input. ('main' is
    already in `accounts` as ~/.claude, and per-dir scans dedupe by resolved
    path, so sharded sessions launched via agent-pick --use are covered;
    sessions under a wholly custom CLAUDE_CONFIG_DIR are not.)"""
    return {
        "throttle_events": scan_throttle_events([a.home for a in accounts], now),
        "throttle_ledger": read_throttle_ledger(cfg, now),
        "swaps": swap_churn(cfg, now, cfg.cache_ttl),
        "pool_session_age_s": pool_session_age(cfg, now),
    }


class StatusAccount(NamedTuple):
    name: str
    email: str
    installed: bool
    view: StatusView


@dataclass(frozen=True)
class StatusSnapshot:
    now: float
    root: Path
    accounts: list[StatusAccount]
    pool: Path
    pool_exists: bool
    installed: str
    last_swap_epoch: int
    timer: str
    recent_swaps: list[str]
    claude_version: ClaudeVersion
    metrics: dict = field(default_factory=dict)


def gather_status(cfg: Config, *, network: bool) -> StatusSnapshot:
    now = time.time()
    accounts = discover_accounts(cfg)
    state = read_state(cfg, now)
    if network:
        probe_fleet(accounts, cfg, now)  # --refresh: staggered real sweep, warms cache

    def view(a: Account) -> StatusView:
        return probe(a, cfg, now) if network else offline_view(a, cfg, now)

    rows = [
        StatusAccount(a.name, a.email, a.name == state["installed"], view(a))
        for a in accounts
    ]
    return StatusSnapshot(
        now=now,
        root=cfg.root,
        accounts=rows,
        pool=cfg.pool,
        pool_exists=cfg.pool.is_dir(),
        installed=state["installed"],
        last_swap_epoch=state["last_swap_epoch"],
        timer=query_timer(),
        recent_swaps=read_swap_tail(cfg),
        claude_version=check_claude_version(),
        metrics=gather_metrics(cfg, accounts, now),
    )


def format_metrics_line(metrics: dict, now: float) -> str:
    """One compact 'throttle:' line for the status text, or '' when there is
    nothing to report. Mirrors the JSON metrics block."""
    te = metrics.get("throttle_events") or {}
    sw = metrics.get("swaps") or {}
    if not te and not sw:
        return ""
    win_m = int(te.get("window_s", 3600) // 60)
    look_h = int(te.get("lookback_s", 86400) // 3600)
    age = metrics.get("pool_session_age_s")
    if isinstance(age, (int, float)):
        sess = f"active {age / 60:.0f}m ago" if age >= 60 else "active now"
    else:
        sess = "idle"
    tl = metrics.get("throttle_ledger") or {}
    ledger = (
        f" · ledger {tl.get('hits', 0)} 429s (n_a≤{tl.get('max_n_a', 0)})"
        if tl.get("rows")
        else ""
    )
    return (
        f"throttle: {te.get('recent', 0)} rate-limit hits in {win_m}m "
        f"({te.get('total', 0)} in {look_h}h) · "
        f"swaps {sw.get('recent', 0)} in 24h ({sw.get('warm_busts', 0)} "
        f"cache-warm) · pool session {sess}{ledger}"
    )


def render_status_text(snap: StatusSnapshot) -> None:
    now = snap.now
    print(f"accounts ({snap.root}):")
    for a in snap.accounts:
        st = a.view
        mark = " <- installed" if a.installed else ""
        if isinstance(st, Usage):
            stale = stale_age_min(st, now)
            age = f" [{stale:.0f}m old]" if stale is not None else ""
            print(
                f"  {a.name:<12} {a.email:<32} "
                f"5h {st.five:3.0f}% ({reset_in(st.r5, now)})  "
                f"7d {st.seven:3.0f}% ({reset_in(st.r7, now)}){age}{mark}"
            )
        else:
            print(f"  {a.name:<12} {a.email:<32} {st}{mark}")
    if not snap.accounts:
        print("  (none logged in)")

    print(
        f"\npool: {snap.pool} "
        f"({'exists' if snap.pool_exists else 'not bootstrapped yet'})"
    )
    print(f"installed: {snap.installed}", end="")
    if snap.last_swap_epoch:
        mins = int((now - snap.last_swap_epoch) / 60)
        print(f" (last swap {mins}m ago)")
    else:
        print()

    print(f"timer: {snap.timer}")

    if snap.recent_swaps:
        print("recent swaps:")
        for line in snap.recent_swaps:
            print(f"  {line}")

    line = format_metrics_line(snap.metrics, now)
    if line:
        print(line)

    # Appended LAST so a match/claude-absent run leaves the byte stream above
    # untouched; only a real major.minor mismatch prints this extra line.
    warning = claude_version_warning_from(snap.claude_version)
    if warning:
        print(warning)


def snapshot_to_json(snap: StatusSnapshot) -> dict:
    """The sole author of the `status --json` document shape (mirrors
    usage_to_cache's single-source pattern). Numbers stay raw so the contract
    survives CLI text changes; consumers recompute countdowns from `now`."""

    def acct(a: StatusAccount) -> dict:
        if isinstance(a.view, Usage):
            u = a.view
            return {
                "name": a.name,
                "email": a.email,
                "installed": a.installed,
                "usage": {
                    "five": u.five,
                    "seven": u.seven,
                    "r5": u.r5,
                    "r7": u.r7,
                    "asof": u.asof,
                },
                "status": None,
            }
        return {
            "name": a.name,
            "email": a.email,
            "installed": a.installed,
            "usage": None,
            "status": str(a.view),
        }

    cv = snap.claude_version
    return {
        "version": VERSION,
        "now": snap.now,
        "root": str(snap.root),
        "accounts": [acct(a) for a in snap.accounts],
        "pool": {"path": str(snap.pool), "exists": snap.pool_exists},
        "installed": {
            "name": snap.installed,
            "last_swap_epoch": snap.last_swap_epoch,
        },
        "timer": snap.timer,
        "recent_swaps": snap.recent_swaps,
        "claude_version": {
            "verified": cv.verified,
            "installed": cv.installed,
            "mismatch": cv.mismatch,
        },
        "metrics": snap.metrics,
    }


def render_status_json(snap: StatusSnapshot) -> None:
    print(json.dumps(snapshot_to_json(snap), indent=2))


def cmd_status(cfg: Config, *, as_json: bool = False, refresh: bool = False) -> int:
    # CLI text path uses probe (network=True) == today's behavior, byte-for-byte.
    # JSON default uses offline_view (cache-only) to preserve the tray's zero
    # passive load; JSON --refresh runs a staggered fleet sweep first.
    snap = gather_status(cfg, network=(not as_json) or refresh)
    if as_json:
        render_status_json(snap)
    else:
        render_status_text(snap)
    return 0


def cmd_watch(cfg: Config) -> int:
    print(
        f"agent-balance: watching every {cfg.interval}s "
        f"(threshold {cfg.threshold:.0f}%, ctrl-c to stop)"
    )
    try:
        while True:
            tick(cfg)
            time.sleep(cfg.interval)
    except KeyboardInterrupt:
        return 0


def unit_dir() -> Path:
    return Path(os.path.expanduser("~/.config/systemd/user"))


def cmd_install(cfg: Config) -> int:
    exe = Path(sys.argv[0]).resolve()
    cmd = str(exe) if os.access(exe, os.X_OK) else f"{sys.executable} {exe}"
    warning = claude_version_warning()  # one-time setup nudge; never fatal
    env_lines = "".join(
        f"Environment={key}={os.environ[key]}\n"
        for key in ENV_KEYS
        if os.environ.get(key)
    )
    service = (
        "[Unit]\n"
        "Description=agent-balance account balancer tick\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"{env_lines}"
        f"ExecStart={cmd} tick\n"
    )
    timer = (
        "[Unit]\n"
        "Description=agent-balance every minute\n\n"
        "[Timer]\n"
        f"OnBootSec={cfg.interval}\n"
        f"OnUnitActiveSec={cfg.interval}\n"
        "AccuracySec=5\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )

    d = unit_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent-balance.service").write_text(service)
    (d / "agent-balance.timer").write_text(timer)
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "agent-balance.timer"],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        print(
            "agent-balance: systemd user units written but could not be "
            "enabled (no systemd user session?)."
        )
        print("Run the balancer another way:")
        print("  agent-balance watch                       # foreground loop")
        print(f"  * * * * * {cmd} tick                     # crontab line")
        if warning:
            print(warning)
        return 0
    print(
        f"agent-balance: timer enabled — ticking every {cfg.interval}s. Watch it with:"
    )
    print("  journalctl --user -u agent-balance -f")
    if warning:
        print(warning)
    return 0


def cmd_uninstall(cfg: Config) -> int:
    try:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "agent-balance.timer"],
            capture_output=True,
        )
    except OSError:
        pass
    removed = False
    for name in ("agent-balance.timer", "agent-balance.service"):
        path = unit_dir() / name
        if path.is_file():
            path.unlink()
            removed = True
    if removed:
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], capture_output=True
            )
        except OSError:
            pass
    print(
        "agent-balance: timer removed."
        if removed
        else "agent-balance: no units were installed."
    )
    print(
        f"The pool dir remains at {cfg.pool} — running sessions keep "
        "using whatever is installed there."
    )
    print(f"Delete it once they are done:  rm -rf {cfg.pool}")
    return 0


def claude_argv(args: list[str]) -> list[str]:
    """argv for the wrapped claude. Drops one leading `--` that a shell wrapper
    inserts to stop agent-balance from parsing claude's own flags — so
    `claude -p …` (alias: `agent-balance launch --shard -- -p …`) reaches claude
    as `claude -p …`, not as an unrecognized agent-balance option."""
    return ["claude", *(args[1:] if args[:1] == ["--"] else args)]


def cmd_launch(cfg: Config, args: list[str], shard: bool = False) -> int:
    if shard:
        return launch_sharded(cfg, args)
    tick(cfg, lock_wait=15)
    if not (cfg.pool / ".credentials.json").is_file():
        print(
            "agent-balance: pool has no credentials (no feasible account?)"
            " — run 'agent-balance status'",
            file=sys.stderr,
        )
        return 1
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg.pool))
    try:
        os.execvpe("claude", claude_argv(args), env)
    except OSError as e:
        print(f"agent-balance: could not exec claude: {e}", file=sys.stderr)
        return 1


def launch_sharded(cfg: Config, args: list[str]) -> int:
    """Automatic sharding: assign THIS instance to the best account by the
    water-filling policy, record a PID-keyed lease, and exec claude pinned to
    that account's home dir for its whole life. Concurrent launches spread
    across accounts (each its own per-minute bucket); each stays put (warm
    cache, no churn). The lock serializes concurrent launches so two starting
    at once see each other's leases and pick distinct accounts."""
    now = time.time()
    accounts = discover_accounts(cfg)
    if not accounts:
        print(
            "agent-balance: no logged-in Anthropic accounts found "
            f"(~/.claude or {cfg.root}/<name>/)",
            file=sys.stderr,
        )
        return 1
    with BalancerLock(cfg, wait=15):
        target = choose_shard_account(cfg, accounts, now)
        add_lease(cfg, os.getpid(), target.name, now)
    print(
        f"agent-balance: instance pinned to {target.name} ({target.email})",
        file=sys.stderr,
    )
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(target.home))
    try:
        os.execvpe("claude", claude_argv(args), env)
    except OSError as e:
        print(f"agent-balance: could not exec claude: {e}", file=sys.stderr)
        return 1


# --------------------------------------------------------------- bench ---
# A throughput instrument: run identical claude calls at concurrency 1, then N
# on one account, then N across distinct accounts, and report output tokens/s.
# If N-on-one-account scales sublinearly while N-across-accounts scales ~N, the
# per-minute bucket is per-account — i.e. sharding instances across accounts is
# the throughput lever. Consumes REAL subscription tokens.

# A throughput bench must measure GENERATION, not process startup — so the
# default prompt forces a substantial, steady block of output tokens. A tiny
# reply (4 tokens) would time claude's spawn + one round-trip and reveal
# nothing about the per-minute output-token bucket.
BENCH_PROMPT = (
    "Write a detailed technical explanation, about 700 words, of how a modern "
    "CPU executes an instruction from fetch through retirement — cover "
    "pipelining, out-of-order execution, branch prediction, and caches. Use "
    "flowing paragraphs, not lists. Aim for at least 700 words."
)

BenchRunner = Callable[[str, "Path | None"], tuple[int, bool]]


class BenchResult(NamedTuple):
    label: str
    runs: int
    ok: int
    out_tokens: int
    wall_s: float

    @property
    def tps(self) -> float:
        return self.out_tokens / self.wall_s if self.wall_s > 0 else 0.0


# The api-key vars (first three): set, any makes Claude Code take the api-key
# path and 401 against subscription credentials (the OAuth gate flips off) — a
# bench launched from inside a Claude Code session, where ANTHROPIC_API_KEY is
# exported, would otherwise report every call as a failure. CLAUDE_EFFORT: an
# interactive session may export a high effort (e.g. xhigh) that balloons each
# bench call's thinking tokens, cost, and latency; drop it so the bench runs at
# the account's own default and measures comparable output throughput.
BENCH_ENV_DROP = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_EFFORT",
)


def bench_env(config_dir: Path | None, base: Mapping[str, str]) -> dict:
    """Environment for a bench claude call: drop the api-key vars so the call
    uses subscription OAuth, and pin CLAUDE_CONFIG_DIR to config_dir (a
    distinct account) or leave it ambient (the pool) when None."""
    env = {k: v for k, v in base.items() if k not in BENCH_ENV_DROP}
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def claude_run(prompt: str, config_dir: Path | None) -> tuple[int, bool]:
    """One real `claude --print --output-format json` call -> (output_tokens,
    ok). config_dir pins CLAUDE_CONFIG_DIR (a distinct account); None uses the
    ambient pool. Never raises; any failure reads as (0, False)."""
    env = bench_env(config_dir, os.environ)
    try:
        r = subprocess.run(
            ["claude", "--print", "--output-format", "json", prompt],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0, False
    if r.returncode != 0:
        return 0, False
    try:
        doc = json.loads(r.stdout)
    except ValueError:
        return 0, False
    usage = doc.get("usage") if isinstance(doc, dict) else None
    tok = (usage or {}).get("output_tokens")
    return (int(tok) if isinstance(tok, (int, float)) else 0), True


def run_bench_batch(
    label: str,
    prompt: str,
    config_dirs: list[Path | None],
    runner: BenchRunner,
    clock: Callable[[], float] = time.time,
) -> BenchResult:
    """Run len(config_dirs) calls CONCURRENTLY, timing the whole batch (so tps
    is aggregate goodput, not a per-call rate). runner/clock are injected so
    the aggregation is testable without spending tokens."""
    start = clock()
    out_tokens = ok = 0
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(len(config_dirs), 1)
    ) as ex:
        for tok, good in ex.map(lambda d: runner(prompt, d), config_dirs):
            out_tokens += tok
            ok += 1 if good else 0
    return BenchResult(label, len(config_dirs), ok, out_tokens, clock() - start)


def cmd_bench(
    cfg: Config,
    prompt: str,
    n: int,
    account_names: list[str],
    runner: BenchRunner = claude_run,
    clock: Callable[[], float] = time.time,
    out=print,
) -> int:
    accounts = discover_accounts(cfg)
    out(
        "agent-balance: benchmark makes REAL claude calls and spends "
        "subscription tokens / quota."
    )

    def show(r: BenchResult) -> None:
        miss = "" if r.ok == r.runs else f" ({r.runs - r.ok} failed)"
        out(
            f"  {r.label:<18} {r.out_tokens:>7} out tok  "
            f"{r.wall_s:6.1f}s  {r.tps:7.1f} tok/s{miss}"
        )

    base = run_bench_batch("1x (baseline)", prompt, [None], runner, clock)
    show(base)
    same = run_bench_batch(f"{n}x same acct", prompt, [None] * n, runner, clock)
    show(same)

    dirs: list[Path | None] = []
    for nm in account_names:
        a = by_name(accounts, nm)
        if a is None:
            out(f"  (unknown account {nm!r}, skipping)")
        else:
            dirs.append(a.home)
    dist = (
        run_bench_batch(f"{len(dirs)}x distinct", prompt, dirs, runner, clock)
        if dirs
        else None
    )
    if dist is not None:
        show(dist)

    if base.tps > 0:
        out(
            f"  same-account scaling: {same.tps / base.tps:.2f}x "
            f"(near 1x => one shared per-minute bucket; near {n}x => not "
            "bucket-bound)"
        )
        if dist is not None:
            out(
                f"  distinct-account scaling: {dist.tps / base.tps:.2f}x "
                f"across {dist.runs} accounts (near {dist.runs}x => sharding "
                "instances multiplies throughput from this IP)"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-balance",
        description="Swap Claude accounts beneath running sessions before "
        "they hit the 5-hour limit.",
    )
    parser.add_argument(
        "--version", action="version", version=f"agent-balance {VERSION}"
    )
    sub = parser.add_subparsers(dest="command")
    status_p = sub.add_parser("status", help="accounts, installed creds, timer health")
    status_p.add_argument("--json", action="store_true", dest="as_json")
    status_p.add_argument("--refresh", action="store_true")
    sub.add_parser("tick", help="one idempotent balance pass")
    sub.add_parser("watch", help="foreground tick loop")
    sub.add_parser("install", help="enable the systemd user timer")
    sub.add_parser("uninstall", help="remove the systemd user timer")
    launch = sub.add_parser("launch", help="tick, then exec claude on the pool")
    launch.add_argument(
        "--shard",
        action="store_true",
        help="pin this instance to the best account (auto-spread), not the pool",
    )
    launch.add_argument("claude_args", nargs=argparse.REMAINDER)
    bench_p = sub.add_parser(
        "bench", help="measure 1-vs-N tokens/sec (spends real tokens)"
    )
    bench_p.add_argument("--prompt", default=BENCH_PROMPT)
    bench_p.add_argument("--n", type=int, default=4)
    bench_p.add_argument(
        "--accounts",
        default="",
        help="comma-separated account names for the distinct-account run",
    )

    ns = parser.parse_args(argv)
    cfg = make_config()
    if sys.platform == "darwin" and ns.command in (
        "tick",
        "watch",
        "install",
        "launch",
    ):
        print(
            "agent-balance: macOS stores Claude credentials in the "
            "Keychain; hot-swapping is Linux-only for now",
            file=sys.stderr,
        )
        return 1

    if ns.command in (None, "status"):
        return cmd_status(
            cfg,
            as_json=getattr(ns, "as_json", False),
            refresh=getattr(ns, "refresh", False),
        )
    if ns.command == "tick":
        return tick(cfg)
    if ns.command == "watch":
        return cmd_watch(cfg)
    if ns.command == "install":
        return cmd_install(cfg)
    if ns.command == "uninstall":
        return cmd_uninstall(cfg)
    if ns.command == "launch":
        return cmd_launch(cfg, ns.claude_args, shard=getattr(ns, "shard", False))
    if ns.command == "bench":
        names = [s.strip() for s in ns.accounts.split(",") if s.strip()]
        return cmd_bench(cfg, ns.prompt, ns.n, names)
    parser.error(f"unknown command {ns.command!r}")


if __name__ == "__main__":
    sys.exit(main())
