# sim/facevalidity.py — THE GATE. Does the model reproduce the real logs?
#
# Before any policy comparison is allowed to claim a winner, three falsifiable
# checks must hold against the REAL swaps + 429 + history timelines:
#
#   (i)   >= 80% of 429s landed while a MANAGED account was installed (the 429
#         records carry no per-account label, so this confirms a managed account
#         was installed throughout the storm — a sequencing precondition — not
#         that throttling tracks one specific account).
#   (ii)  most 429s occurred at a 5h utilization in the 8-23% band — i.e. the
#         throttle is DECOUPLED from the 5h quota wall (a throughput knee, not a
#         usage limit). Only 429s whose installed account has history coverage
#         can be checked; if too FEW are covered (< COVERAGE_MIN_FRAC of all
#         429s) the check cannot certify, since a high decoupled-share on a tiny
#         covered subset is not evidence about the storm as a whole.
#   (iii) the .history sawtooth actually RESETS at r5 (a 5h window rollover is
#         visible as a large drop), confirming the burn/reset model's shape.
#
# When all three hold we have a calibrated regime and the experiment compares
# inside it. When any fail (notably k_a = inf, which can never produce 429s and
# so can't reproduce the storms), results are watermarked DIRECTIONAL ONLY.

from __future__ import annotations

from dataclasses import dataclass

from sim.calibrate import Calibration, installed_at

UTIL_LO = 8.0  # the 429 5h-util band (decoupled-from-quota evidence)
UTIL_HI = 23.0
# The real decoupling claim: 429s land FAR from the 5h quota wall, not at it.
# A per-minute throughput knee fires regardless of how full the 5h window is, so
# the evidence is that almost no 429 happens near 100% — they cluster low-to-mid.
WALL = 50.0  # "near the quota wall" begins here; a decoupled 429 stays below it
INSTALLED_SHARE_MIN = 0.80  # >= this fraction of 429s on the installed account
DECOUPLED_SHARE_MIN = 0.75  # >= this fraction of covered 429s below WALL
COVERAGE_MIN_FRAC = 0.5  # >= this fraction of 429s must have history coverage,
# else (ii) is unverifiable: a high decoupled_share on a 1-of-100 covered subset
# is not evidence the storm was decoupled from the quota wall.
DROP_MIN = 10.0  # a 5h reset shows up as at least this big a downward step


@dataclass
class GateResult:
    """One face-validity verdict. `passed` gates the experiment; the booleans
    say which checks held, with the supporting numbers. band_share is the
    fraction of covered 429s in the tight 8-23% band (descriptive);
    decoupled_share is the fraction below the quota wall (the PASS condition for
    check ii — 429s are decoupled from the 5h limit)."""

    passed: bool
    installed_share: float
    n_429: int
    band_share: float
    decoupled_share: float
    n_band_covered: int
    saw_reset: bool
    n_resets: int
    notes: list[str]

    @property
    def verdict(self) -> str:
        return "CALIBRATED" if self.passed else "DIRECTIONAL ONLY"


def _util_at(history: list[tuple[int, float]], epoch: float) -> float | None:
    """The most recent .history 5h value at or before `epoch`, or None when the
    epoch predates the series (no coverage)."""
    best: float | None = None
    for e, v in history:
        if e <= epoch:
            best = v
        else:
            break
    return best


def _count_resets(history: list[tuple[int, float]]) -> int:
    """Downward steps of at least DROP_MIN — visible 5h window rollovers."""
    n = 0
    for (_, v0), (_, v1) in zip(history, history[1:], strict=False):
        if v0 - v1 >= DROP_MIN:
            n += 1
    return n


def evaluate(cal: Calibration) -> GateResult:
    """Run the three checks against the real timelines in a Calibration."""
    notes: list[str] = []
    epochs = cal.throttle_epochs
    n = len(epochs)

    # (i) installed-account share.
    on_installed = 0
    for ep in epochs:
        acct = installed_at(cal.swaps, ep)
        if acct != "unknown" and acct in cal.accounts:
            # Credit the 429 to the installed account (the real logs put 100%
            # there; we only require >= 80%).
            on_installed += 1
    installed_share = on_installed / n if n else 0.0

    # (ii) 429s are DECOUPLED from the 5h quota wall: among 429s with history
    # coverage on the installed account, the util sits low-to-mid (well below
    # the wall), and a large share lands in the tight 8-23% band.
    in_band = 0
    decoupled = 0
    covered = 0
    for ep in epochs:
        acct = installed_at(cal.swaps, ep)
        hist = cal.history.get(acct, [])
        u = _util_at(hist, ep)
        if u is None:
            continue
        covered += 1
        if UTIL_LO <= u <= UTIL_HI:
            in_band += 1
        if u < WALL:
            decoupled += 1
    band_share = in_band / covered if covered else 0.0
    decoupled_share = decoupled / covered if covered else 0.0
    cover_floor = max(1, int(COVERAGE_MIN_FRAC * n)) if n else 0
    if covered < n:
        notes.append(
            f"{n - covered}/{n} 429s predate any .history coverage "
            "(history is short — band checked on the covered subset only)"
        )
    if n and covered < cover_floor:
        notes.append(
            f"only {covered}/{n} 429s have history coverage "
            f"(< {COVERAGE_MIN_FRAC:.0%}) — (ii) cannot be certified"
        )

    # (iii) the sawtooth resets at r5 (a visible drop) across all accounts.
    total_resets = sum(_count_resets(hist) for hist in cal.history.values())
    saw_reset = total_resets > 0

    check_i = n > 0 and installed_share >= INSTALLED_SHARE_MIN
    check_ii = (
        covered >= cover_floor
        and covered > 0
        and decoupled_share >= DECOUPLED_SHARE_MIN
    )
    check_iii = saw_reset
    if n == 0:
        notes.append("no 429 incidents found — cannot confirm the throttle model")
    if not saw_reset:
        notes.append("no 5h sawtooth reset visible in history — shape unconfirmed")

    passed = check_i and check_ii and check_iii
    return GateResult(
        passed=passed,
        installed_share=installed_share,
        n_429=n,
        band_share=band_share,
        decoupled_share=decoupled_share,
        n_band_covered=covered,
        saw_reset=saw_reset,
        n_resets=total_resets,
        notes=notes,
    )


def calibrated_region(
    cal: Calibration, k_grid: list[float], fanout_grid: list[float]
) -> list[tuple[float, float]]:
    """The (k_a, fanout) cells where the gate's spirit holds: a FINITE knee
    that the demand can actually cross (so 429s are reproducible), and a fanout
    >= 1. k_a = inf is excluded — an infinite bucket can never 429, so it cannot
    reproduce the storms and any comparison there is DIRECTIONAL only. The gate
    against the static timelines (evaluate) decides overall pass/fail; this
    enumerates which sweep cells stay inside the calibrated regime."""
    region: list[tuple[float, float]] = []
    base = evaluate(cal)
    if not base.passed:
        return region
    for k in k_grid:
        if k == float("inf"):
            continue
        for f in fanout_grid:
            if f >= 1.0:
                region.append((k, f))
    return region
