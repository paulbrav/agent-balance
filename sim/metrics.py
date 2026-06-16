# sim/metrics.py — per-replicate instruments over one engine run.
#
# Every metric is observational (the sim never steers on these). The headline
# numbers are throttled-instance-seconds and useful goodput; perished weekly
# allowance is computed but ALWAYS tagged [SHAPE-ONLY] — the real cache carries
# only one instantaneous `seven` value, no weekly time series, so its level is
# not identifiable and must never decide a winner. Concentration is the time-
# weighted max account share of live load (1.0 = everything on one account).

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Metrics:
    """Accumulated over one replicate. The engine folds slices in as it ticks;
    finalize() turns the running sums into rates/indices."""

    throttled_instance_seconds: float = 0.0  # time x instances spent under a 429
    useful_goodput: float = 0.0  # instance-seconds of ON work that was NOT throttled
    busy_instance_seconds: float = 0.0  # all ON instance-seconds (the denominator)
    wall_stalls: int = 0  # launches blocked by every account at its 5h wall
    perished_weekly: float = 0.0  # 7d allowance expired unused [SHAPE-ONLY]
    # Concentration: time-weighted mean of (max account share of live load).
    _conc_weighted: float = 0.0
    _conc_time: float = 0.0
    launches: int = 0
    throttle_events: int = 0

    def fold_slice(self, dt: float, load: dict[str, int]) -> None:
        """Accumulate the concentration index over a dt-second slice with the
        given live load. Slices with no live instances contribute share 0 (an
        idle fleet is not 'concentrated')."""
        total = sum(load.values())
        share = (max(load.values()) / total) if total else 0.0
        self._conc_weighted += share * dt
        self._conc_time += dt

    @property
    def concentration_index(self) -> float:
        """Time-weighted max account share of live load over the run."""
        return self._conc_weighted / self._conc_time if self._conc_time else 0.0

    @property
    def goodput_fraction(self) -> float:
        """Fraction of ON work that completed without a 429 — the headline
        efficiency. 1.0 when nothing was throttled."""
        if self.busy_instance_seconds <= 0:
            return 1.0
        return self.useful_goodput / self.busy_instance_seconds

    def as_row(self) -> dict[str, float]:
        """Flat dict for the bootstrap / Pareto table. perished_weekly is
        carried but the report tags it [SHAPE-ONLY]."""
        return {
            "throttled_instance_seconds": self.throttled_instance_seconds,
            "goodput_fraction": self.goodput_fraction,
            "useful_goodput": self.useful_goodput,
            "wall_stalls": float(self.wall_stalls),
            "concentration_index": self.concentration_index,
            "perished_weekly": self.perished_weekly,
            "throttle_events": float(self.throttle_events),
        }


@dataclass
class ReplicateResults:
    """One policy's metric rows across replicates — the bootstrap input."""

    policy: str
    rows: list[dict[str, float]] = field(default_factory=list)

    def values(self, metric: str) -> list[float]:
        return [r[metric] for r in self.rows]
