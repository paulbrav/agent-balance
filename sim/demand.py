# sim/demand.py — the endogenous 5h-burn demand model and its fitter.
#
# Per instance, demand is a Markov-modulated ON/OFF accumulator: the instance
# alternates ON (busy, adding to its account's 5h utilization) and OFF (idle)
# with exponential dwell times, and each ON minute adds a Gamma-distributed
# increment. Fleet-level 5h burn is the sum over the ON instances. The fitter
# recovers (P(busy), ON increment mean/CV) from a differenced .history series.
#
# The demand-STARVED regime is first class: an account whose history is flat
# zero (alt1/alt3 in the real logs) gets ZERO endogenous demand — it only ever
# carries load a policy explicitly routes there. That is the whole point of the
# experiment, so the fitter must reproduce it, not smooth it away.

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DemandFit:
    """The calibrated demand of one account. on_mean/on_cv describe the Gamma
    ON-minute increment (%/min). p_busy is the stationary ON probability,
    realized via mean_on / mean_off exponential dwells. starved is True for a
    flat-zero account (no endogenous demand at all)."""

    p_busy: float
    on_mean: float
    on_cv: float
    mean_on: float
    mean_off: float
    starved: bool

    @property
    def on_shape(self) -> float:
        """Gamma shape k = 1/CV^2; large for a tight increment, ~1 (exponential)
        for CV=1. Floored so a degenerate CV can't explode the shape."""
        cv = max(self.on_cv, 1e-6)
        return 1.0 / (cv * cv)


# Calibration bands from the task brief (P(busy) 0.30–0.40, ON mean 0.2–0.65
# %/min, CV 0.43–0.65). A starved (flat-zero) account bypasses these entirely.
DEFAULT_MEAN_ON = 5.0  # minutes ON per sojourn
DEFAULT_MEAN_OFF = 10.0  # minutes OFF per sojourn — gives ~0.33 stationary busy


def _diffs(series: list[tuple[int, float]]) -> list[tuple[float, float]]:
    """Per-minute positive increments of a (epoch, five%) series: (dt_min, d5).
    A drop (5h reset) is dropped — it is a window rollover, not demand. dt is in
    minutes so the increment is already a %/min rate."""
    out: list[tuple[float, float]] = []
    for (e0, v0), (e1, v1) in zip(series, series[1:], strict=False):
        dt = (e1 - e0) / 60.0
        if dt <= 0:
            continue
        d = v1 - v0
        if d <= 0:  # flat or reset — not an ON-minute of burn
            out.append((dt, 0.0))
        else:
            out.append((dt, d / dt))
    return out


def fit_demand(series: list[tuple[int, float]]) -> DemandFit:
    """Recover a DemandFit from a differenced 5h-utilization history.

    A flat-zero series (every value 0, or fewer than two points) is the
    demand-starved regime -> zero demand, starved=True. Otherwise: P(busy) is
    the fraction of minute-slices with positive burn; the ON increment's mean
    and CV come from the positive slices only. Results are clamped into the
    calibration bands so a thin real series can't produce a degenerate model."""
    series = sorted(series)
    values = [v for _, v in series]
    if len(series) < 2 or all(v == 0.0 for v in values):
        return DemandFit(0.0, 0.0, 0.5, DEFAULT_MEAN_ON, DEFAULT_MEAN_OFF, True)

    rates = _diffs(series)
    if not rates:
        return DemandFit(0.0, 0.0, 0.5, DEFAULT_MEAN_ON, DEFAULT_MEAN_OFF, True)

    positives = [r for _, r in rates if r > 0]
    p_busy = len(positives) / len(rates)
    if not positives:
        return DemandFit(0.0, 0.0, 0.5, DEFAULT_MEAN_ON, DEFAULT_MEAN_OFF, True)

    mean = sum(positives) / len(positives)
    if len(positives) > 1:
        var = sum((r - mean) ** 2 for r in positives) / (len(positives) - 1)
        cv = math.sqrt(var) / mean if mean > 0 else 0.5
    else:
        cv = 0.5

    # Clamp into the brief's bands — keeps a 3-point real series honest.
    p_busy = min(max(p_busy, 0.30), 0.40)
    on_mean = min(max(mean, 0.20), 0.65)
    on_cv = min(max(cv, 0.43), 0.65)
    # Realize p_busy through dwell times: p = mean_on / (mean_on + mean_off).
    mean_on = DEFAULT_MEAN_ON
    mean_off = mean_on * (1.0 - p_busy) / p_busy if p_busy > 0 else DEFAULT_MEAN_OFF
    return DemandFit(p_busy, on_mean, on_cv, mean_on, mean_off, False)


def on_increment(rng, fit: DemandFit) -> float:
    """One ON-minute's 5h-utilization increment (%), Gamma(shape, scale) with
    the fitted mean and CV. A starved account never calls this (it has no ON
    minutes), but guard anyway so it returns 0."""
    if fit.starved or fit.on_mean <= 0:
        return 0.0
    shape = fit.on_shape
    scale = fit.on_mean / shape
    return rng.gammavariate(shape, scale)
