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
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

VERSION = "0.3.0"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_TTL = 45  # seconds a successful usage probe stays cached
RESET_SOON = 900  # a 5h window resetting within this is treated as open
WEEK = 7 * 86400


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

    @property
    def pool(self) -> Path:
        return self.root / ".active"

    @property
    def state_file(self) -> Path:
        return self.root / ".balancer-state.json"

    @property
    def lock_file(self) -> Path:
        return self.root / ".balancer-lock"


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


# ----------------------------------------------------------- usage probe ---


@dataclass
class Usage:
    five: float  # 5h window utilization %
    seven: float  # 7d window utilization %
    r5: int  # 5h reset epoch (0 = unknown)
    r7: int  # 7d reset epoch (0 = unknown)
    asof: float = 0.0  # epoch the numbers were fetched (0 = unknown/fresh)


def parse_reset(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    return 0


def fetch_usage(token: str):
    """One GET against the OAuth usage endpoint -> Usage or a status word
    ('limited' on 429, 'error' otherwise). The endpoint enforces a short
    per-IP rate limit; callers must cache."""
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return "limited" if e.code == 429 else "error"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return "error"
    five = data.get("five_hour") or {}
    seven = data.get("seven_day") or {}
    return Usage(
        five=float(five.get("utilization") or 0),
        seven=float(seven.get("utilization") or 0),
        r5=parse_reset(five.get("resets_at")),
        r7=parse_reset(seven.get("resets_at")),
    )


def cache_get(cfg: Config, name: str, now: float) -> Usage | None:
    entry = read_json(cfg.cache / name)
    try:
        if now - entry["epoch"] < USAGE_TTL:
            return Usage(
                entry["five"],
                entry["seven"],
                entry["r5"],
                entry["r7"],
                asof=float(entry["epoch"]),
            )
    except (KeyError, TypeError):
        pass
    return None


def last_known(cfg: Config, name: str) -> Usage | None:
    """The most recent successful probe regardless of age — the fallback
    when the endpoint is rate-limiting. asof carries its timestamp."""
    entry = read_json(cfg.cache / name)
    try:
        return Usage(
            entry["five"],
            entry["seven"],
            entry["r5"],
            entry["r7"],
            asof=float(entry["epoch"]),
        )
    except (KeyError, TypeError):
        return None


def cache_put(cfg: Config, name: str, usage: Usage, now: float) -> None:
    try:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        (cfg.cache / name).write_text(
            json.dumps(
                {
                    "epoch": now,
                    "five": usage.five,
                    "seven": usage.seven,
                    "r5": usage.r5,
                    "r7": usage.r7,
                }
            )
        )
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
    try:
        wait = PROBE_SPACING - (time.time() - float(marker.read_text()))
        if 0 < wait <= PROBE_SPACING:
            time.sleep(wait)
    except (OSError, ValueError):
        pass
    try:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(time.time()))
    except OSError:
        pass


def probe(account: Account, cfg: Config, now: float, fetcher=fetch_usage):
    """Usage for one account, or a status word: nologin | expired |
    limited | error. Cache-first; staggered real fetches; on a
    rate-limited endpoint, falls back to the last known numbers (asof
    marks their age) for up to STALE_MAX seconds."""
    oauth = read_json(account.creds).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        return "nologin"
    if (oauth.get("expiresAt") or 0) / 1000 <= now:
        return "expired"
    cached = cache_get(cfg, account.name, now)
    if cached is not None:
        return cached

    def stale_or(word: str):
        stale = last_known(cfg, account.name)
        if stale is not None and now - stale.asof <= STALE_MAX:
            return stale
        return word

    cooldown = cfg.cache / f"{account.name}.limited"
    try:
        if now - float(cooldown.read_text()) < LIMITED_COOLDOWN:
            return stale_or("limited")
    except (OSError, ValueError):
        pass

    if fetcher is fetch_usage:  # throttle only the real network path
        throttle_fetch(cfg)
    result = fetcher(token)
    if isinstance(result, Usage):
        result.asof = now
        cache_put(cfg, account.name, result, now)
        record_history(cfg, account.name, result, now)
        try:
            cooldown.unlink()
        except OSError:
            pass
        return result
    if result == "limited":
        try:
            cfg.cache.mkdir(parents=True, exist_ok=True)
            cooldown.write_text(str(now))
        except OSError:
            pass
    return stale_or(result)


def offline_view(account: Account, cfg: Config, now: float):
    """Cache-only view for displays (the tray): Usage — possibly stale,
    asof says how stale — or a status word. Never touches the network;
    the balancer tick is the only steady fetcher on the machine."""
    oauth = read_json(account.creds).get("claudeAiOauth") or {}
    if not oauth.get("accessToken"):
        return "nologin"
    if (oauth.get("expiresAt") or 0) / 1000 <= now:
        return "expired"
    stale = last_known(cfg, account.name)
    return stale if stale is not None else "no data yet"


def record_history(cfg: Config, name: str, usage: Usage, now: float) -> None:
    """Fresh probes only (cache hits would flatten the slope): an
    append-only 'epoch five%' series feeding burn_rate."""
    try:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        path = cfg.cache / f"{name}.history"
        with path.open("a") as f:
            f.write(f"{int(now)} {usage.five}\n")
        lines = path.read_text().splitlines()
        if len(lines) > 200:
            atomic_write(path, ("\n".join(lines[-100:]) + "\n").encode(), mode=0o644)
    except OSError:
        pass


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


def pick_target(
    accounts: list[Account], stats: dict, exclude: str | None, now: float, cfg: Config
) -> tuple[Account | None, bool]:
    """Feasibility first (5h room for a typical session, weekly not
    spent), then the highest-urgency account — the one whose remaining
    week needs the highest sustained burn rate. Ties go to the earlier
    weekly reset (EDF), then 5h headroom.
    Returns (account, feasible) — (None, False) when nothing qualifies."""
    rows = []
    for a in accounts:
        if a.name == exclude or not isinstance(stats.get(a.name), Usage):
            continue
        u = normalized(stats[a.name], now)
        rows.append((a, u, feasible_now(u, cfg, now), urgency(u, a.capacity, now)))
    if not rows:
        return None, False

    cls = any(f for _, _, f, _ in rows)
    pool = [r for r in rows if r[2] == cls]

    def rank(r):
        a, u, _, urg = r
        eff_r7 = u.r7 if u.r7 > now else now + WEEK
        return (urg, -eff_r7, 100 - u.five, a.name)

    best = max(pool, key=rank)
    return best[0], cls


# ------------------------------------------------------ blobs and state ---


def sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def blob_expires(path: Path) -> int:
    """expiresAt (ms) of a credentials file; 0 when unreadable."""
    oauth = read_json(path).get("claudeAiOauth") or {}
    exp = oauth.get("expiresAt")
    return exp if isinstance(exp, int) else 0


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def copy_creds(src: Path, dst: Path) -> str:
    """Atomic credentials copy; returns the blob's sha256."""
    data = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(dst, data)
    return hashlib.sha256(data).hexdigest()


def read_state(cfg: Config, now: float) -> dict:
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
    return {
        "installed": installed,
        "blob_sha256": sha,
        "last_swap_epoch": min(int(epoch), int(now)),
    }


def write_state(cfg: Config, state: dict) -> None:
    cfg.root.mkdir(parents=True, exist_ok=True)
    atomic_write(cfg.state_file, json.dumps(state, indent=2).encode(), mode=0o644)


def log_swap(cfg: Config, now: float, line: str) -> None:
    try:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        path = cfg.cache / "swaps"
        with path.open("a") as f:
            f.write(f"{int(now)} {line}\n")
        lines = path.read_text().splitlines()
        if len(lines) > 2000:
            atomic_write(path, ("\n".join(lines[-1000:]) + "\n").encode(), mode=0o644)
    except OSError:
        pass


# ------------------------------------------------- pool install / harvest ---


def install_creds(cfg: Config, account: Account, out=print) -> str:
    """Copy the account's credentials into the pool and stamp its identity
    into the pool's .claude.json (cosmetic — keeps /status honest)."""
    sha = copy_creds(account.creds, cfg.pool / ".credentials.json")
    oauth_account = read_json(meta_path(account.home)).get("oauthAccount")
    pool_meta_path = cfg.pool / ".claude.json"
    if oauth_account:
        pool_meta = read_json(pool_meta_path)
        pool_meta["oauthAccount"] = oauth_account
        atomic_write(
            pool_meta_path, json.dumps(pool_meta, indent=2).encode(), mode=0o644
        )
    return sha


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
    cfg: Config, accounts: list[Account], state: dict, now: float, out=print
) -> dict:
    """Reconcile the pool with the canonical account dirs before deciding
    anything. Two passes:

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
        pool_exp = blob_expires(pool_creds)
        home_exp = blob_expires(installed.creds)
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
    try:
        if now - int(stamp.read_text().strip()) < PULL_CHECK:
            return False
    except (OSError, ValueError):
        pass
    try:
        cfg.cache.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(int(now)))
    except OSError:
        pass
    return True


def tick(
    cfg: Config,
    now: float | None = None,
    fetcher=fetch_usage,
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

        state = read_state(cfg, now)
        state = harvest(cfg, accounts, state, now, out)
        installed = by_name(accounts, state["installed"])

        need = reason = None
        five_now = 0.0
        est = ""
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
        stats = pulled = None
        if (
            need is None
            and installed is not None
            and isinstance(st, Usage)
            and cfg.pull_margin > 0
            and pull_due(cfg, now)
        ):
            u_inst = urgency(st, installed.capacity, now)
            stats = {a.name: probe(a, cfg, now, fetcher) for a in accounts}
            best = None
            for a in accounts:
                if a.name == installed.name:
                    continue
                sa = stats.get(a.name)
                if not isinstance(sa, Usage):
                    continue
                ua = normalized(sa, now)
                # A pull target must have real 5h headroom — proactive
                # swaps should never move onto an almost-walled account.
                if not feasible_now(ua, cfg, now):
                    continue
                if ua.five + cfg.draw >= cfg.threshold:
                    continue
                u_a = urgency(ua, a.capacity, now)
                if best is None or u_a > best[1]:
                    best = (a, u_a, ua)
            margin = max(cfg.pull_margin, 0.15 * u_inst)
            if best is not None and best[1] >= u_inst + margin:
                pulled, u_a, ua = best
                need = "soft"
                reason = (
                    f"rebalance: {pulled.name} needs {u_a:.0f}%/day to clear "
                    f"its week vs {u_inst:.0f}%/day for {installed.name}"
                )

        if need is None:
            assert installed is not None  # need would be "hard" otherwise
            out(f"agent-balance: {installed.name} at {five_now:.0f}% 5h{est} — ok")
            return 0
        # min_gap damps optimization churn only — a 5h-wall roll ("roll")
        # or a dead token ("hard") must never wait out a hysteresis gap
        # behind a routine rebalance swap.
        if need == "soft" and now - state["last_swap_epoch"] < cfg.min_gap:
            out(
                f"agent-balance: swap due ({reason}) but inside the "
                f"{cfg.min_gap}s gap since the last one"
            )
            return 0

        if stats is None:
            stats = {a.name: probe(a, cfg, now, fetcher) for a in accounts}
        if pulled is not None:
            target, feasible = pulled, True
        else:
            exclude = installed.name if installed is not None else None
            target, feasible = pick_target(accounts, stats, exclude, now, cfg)
        if target is None or not feasible:
            out(
                f"agent-balance: swap due ({reason}) but no other account "
                "is feasible; leaving credentials in place"
            )
            return 0

        if not cfg.pool.is_dir():
            bootstrap_pool(cfg, target, out)
        sha = install_creds(cfg, target, out)
        prev = state["installed"]
        state.update(installed=target.name, blob_sha256=sha, last_swap_epoch=int(now))
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


def cmd_status(cfg: Config) -> int:
    now = time.time()
    accounts = discover_accounts(cfg)
    state = read_state(cfg, now)

    def reset_in(epoch: int) -> str:
        s = epoch - now
        if epoch == 0:
            return "-"
        if s <= 0:
            return "now"
        if s < 3600:
            return f"{int(s // 60) + 1}m"
        if s < 86400:
            return f"{round(s / 3600)}h"
        return f"{round(s / 86400)}d"

    print(f"accounts ({cfg.root}):")
    for a in accounts:
        st = probe(a, cfg, now)
        mark = " <- installed" if a.name == state["installed"] else ""
        if isinstance(st, Usage):
            age = ""
            if st.asof and now - st.asof > 2 * USAGE_TTL:
                age = f" [{(now - st.asof) / 60:.0f}m old]"
            print(
                f"  {a.name:<12} {a.email:<32} "
                f"5h {st.five:3.0f}% ({reset_in(st.r5)})  "
                f"7d {st.seven:3.0f}% ({reset_in(st.r7)}){age}{mark}"
            )
        else:
            print(f"  {a.name:<12} {a.email:<32} {st}{mark}")
    if not accounts:
        print("  (none logged in)")

    print(
        f"\npool: {cfg.pool} "
        f"({'exists' if cfg.pool.is_dir() else 'not bootstrapped yet'})"
    )
    print(f"installed: {state['installed']}", end="")
    if state["last_swap_epoch"]:
        mins = int((now - state["last_swap_epoch"]) / 60)
        print(f" (last swap {mins}m ago)")
    else:
        print()

    timer = "no systemd"
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "agent-balance.timer"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        timer = r.stdout.strip() or "inactive"
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(f"timer: {timer}")

    swaps = cfg.cache / "swaps"
    if swaps.is_file():
        tail = swaps.read_text().splitlines()[-5:]
        if tail:
            print("recent swaps:")
            for line in tail:
                print(f"  {line}")
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
    env_lines = "".join(
        f"Environment={key}={os.environ[key]}\n"
        for key in (
            "AGENT_PICK_ROOT",
            "CLAUDE_ACCOUNTS_ROOT",
            "AGENT_BALANCE_THRESHOLD",
            "AGENT_BALANCE_MIN_GAP",
            "AGENT_BALANCE_DRAW",
            "AGENT_BALANCE_PULL_MARGIN",
        )
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
        return 0
    print(
        f"agent-balance: timer enabled — ticking every {cfg.interval}s. Watch it with:"
    )
    print("  journalctl --user -u agent-balance -f")
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


def cmd_launch(cfg: Config, args: list[str]) -> int:
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
        os.execvpe("claude", ["claude", *args], env)
    except OSError as e:
        print(f"agent-balance: could not exec claude: {e}", file=sys.stderr)
        return 1


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
    sub.add_parser("status", help="accounts, installed creds, timer health")
    sub.add_parser("tick", help="one idempotent balance pass")
    sub.add_parser("watch", help="foreground tick loop")
    sub.add_parser("install", help="enable the systemd user timer")
    sub.add_parser("uninstall", help="remove the systemd user timer")
    launch = sub.add_parser("launch", help="tick, then exec claude on the pool")
    launch.add_argument("claude_args", nargs=argparse.REMAINDER)

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
        return cmd_status(cfg)
    if ns.command == "tick":
        return tick(cfg)
    if ns.command == "watch":
        return cmd_watch(cfg)
    if ns.command == "install":
        return cmd_install(cfg)
    if ns.command == "uninstall":
        return cmd_uninstall(cfg)
    if ns.command == "launch":
        return cmd_launch(cfg, ns.claude_args)
    parser.error(f"unknown command {ns.command!r}")


if __name__ == "__main__":
    sys.exit(main())
