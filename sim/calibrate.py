# sim/calibrate.py — load + fit the REAL logs into a simulatable world.
#
# The fitters read the four on-disk formats the shipped tool writes and turn
# them into AccountStates seeded through the production usage_from_cache, per-
# account DemandFits from the differenced .history series, the swaps install
# timeline, and the deduped 429 timestamps. Everything here is read-only against
# the cache/projects trees; nothing in the sim ever writes there.
#
# Identifiability is honest by construction: history gives the 5h demand shape;
# the snapshot gives ONE instantaneous `seven` (no weekly series -> perished
# allowance is SHAPE-ONLY); the swaps ledger says which account was installed;
# the 429 timestamps are clustered on whichever account was then installed. k_a,
# per-minute rate, arrival intensity and fanout are UNOBSERVED -> swept axes.

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sim import prod
from sim.demand import DemandFit, fit_demand
from sim.state import AccountState

ACCOUNT_NAMES = ("main", "alt1", "alt2", "alt3")


def read_history(path: Path) -> list[tuple[int, float]]:
    """Parse a '<epoch_int> <five_float>' .history series. Malformed lines are
    skipped (matches the production tolerance). Sorted by epoch."""
    out: list[tuple[int, float]] = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            out.append((int(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return sorted(out)


def read_snapshot(path: Path) -> prod.Usage | None:
    """Load a snapshot JSON through the REAL usage_from_cache — the sim never
    re-derives the cache shape."""
    try:
        entry = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(entry, dict):
        return None
    return prod.usage_from_cache(entry)


@dataclass(frozen=True)
class Swap:
    epoch: int
    frm: str
    to: str


def read_swaps(path: Path) -> list[Swap]:
    """Parse the swaps ledger: '<epoch> SWAP <from> <to> <five%> <reason>'.
    Returns the install timeline sorted by epoch."""
    out: list[Swap] = []
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[1] != "SWAP":
            continue
        try:
            out.append(Swap(int(parts[0]), parts[2], parts[3]))
        except ValueError:
            continue
    return sorted(out, key=lambda s: s.epoch)


def installed_at(swaps: list[Swap], epoch: float) -> str:
    """Which account the swaps timeline says was installed at `epoch`. 'unknown'
    before the first swap."""
    cur = "unknown"
    for s in swaps:
        if s.epoch <= epoch:
            cur = s.to
        else:
            break
    return cur


def _parse_iso(ts: str) -> float | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def read_throttle_timestamps(projects: Path, marker: str | None = None) -> list[float]:
    """Deduped 429 epochs from ~/.claude/projects/**/*.jsonl.

    A 429 is a line carrying THROTTLE_MARKER; the same incident appears in the
    parent transcript and again in each subagent copy, so we dedup by exact
    ISO timestamp (the brief's dedup rule). Returns sorted epochs. Read-only,
    fail-soft per file."""
    marker = marker or prod.THROTTLE_MARKER
    seen: set[str] = set()
    try:
        paths = list(projects.rglob("*.jsonl"))
    except OSError:
        return []
    for p in paths:
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        if marker not in text:
            continue
        for line in text.splitlines():
            if marker not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            ts = obj.get("timestamp") if isinstance(obj, dict) else None
            if isinstance(ts, str):
                seen.add(ts)
    epochs = sorted(e for ts in seen if (e := _parse_iso(ts)) is not None)
    return epochs


@dataclass
class Calibration:
    """The fitted, ready-to-simulate world plus the raw timelines the gate
    replays. accounts/fits seed an Engine; swaps + throttles drive face
    validity."""

    accounts: dict[str, AccountState]
    fits: dict[str, DemandFit]
    swaps: list[Swap] = field(default_factory=list)
    throttle_epochs: list[float] = field(default_factory=list)
    history: dict[str, list[tuple[int, float]]] = field(default_factory=dict)


def load_real(cache: Path, projects: Path) -> Calibration:
    """Build a Calibration from the real cache + projects trees. Each account
    gets an AccountState seeded from its snapshot (via usage_from_cache) and a
    DemandFit from its differenced history; a flat-zero history yields a starved
    (zero-demand) fit — the alt1/alt3 regime, reproduced not smoothed."""
    accounts: dict[str, AccountState] = {}
    fits: dict[str, DemandFit] = {}
    history: dict[str, list[tuple[int, float]]] = {}
    learned = prod.read_caps(prod.Config(  # reuse the real caps.json reader
        root=Path("/sim"), cache=cache, threshold=99, min_gap=0, interval=60,
        draw=10, pull_margin=0,
    ))
    for name in ACCOUNT_NAMES:
        hist = read_history(cache / f"{name}.history")
        history[name] = hist
        fit = fit_demand(hist)
        fits[name] = fit
        snap = read_snapshot(cache / name)
        st = AccountState(name=name, capacity=1.0)
        if snap is not None:
            st.five = snap.five
            st.seven = snap.seven
            st.r5 = snap.r5
            st.r7 = snap.r7
        st.p_busy = fit.p_busy
        st.on_mean = fit.on_mean
        st.on_cv = fit.on_cv
        st.mean_on = fit.mean_on
        st.mean_off = fit.mean_off
        if name in learned:
            st.k_a = float(learned[name])
        accounts[name] = st
    return Calibration(
        accounts=accounts,
        fits=fits,
        swaps=read_swaps(cache / "swaps"),
        throttle_epochs=read_throttle_timestamps(projects),
        history=history,
    )


def calibration_start(cal: Calibration) -> float:
    """A deterministic simulation start epoch: the latest snapshot `asof` we
    have, else the last history point, else 0. Anchors the engine on real
    quota positions without touching the wall clock."""
    candidates: list[float] = []
    for hist in cal.history.values():
        if hist:
            candidates.append(float(hist[-1][0]))
    return max(candidates) if candidates else 0.0
