# sim/bootstrap.py — seeded, stdlib-only paired bootstrap CIs.
#
# Policies run under common random numbers, so replicate i of policy A and
# replicate i of policy B saw the SAME demand/hazard draws — they are paired.
# A paired bootstrap resamples replicate INDICES (not the two policies
# independently), preserving that pairing, and reports a CI on the per-metric
# difference. All resampling goes through a seeded random.Random; no numpy.

from __future__ import annotations

from dataclasses import dataclass


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


@dataclass(frozen=True)
class CI:
    """A point estimate with a percentile confidence interval."""

    point: float
    lo: float
    hi: float

    @property
    def excludes_zero(self) -> bool:
        """True when the whole interval is one side of 0 — a CRN-significant
        difference at this level."""
        return (self.lo > 0) or (self.hi < 0)


def _percentile(sorted_xs: list[float], q: float) -> float:
    """Linear-interpolated percentile of an already-sorted list, q in [0,1]."""
    if not sorted_xs:
        return 0.0
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    idx = q * (len(sorted_xs) - 1)
    lo = int(idx)
    frac = idx - lo
    if lo + 1 >= len(sorted_xs):
        return sorted_xs[-1]
    return sorted_xs[lo] * (1 - frac) + sorted_xs[lo + 1] * frac


def bootstrap_mean(
    xs: list[float], rng, *, iters: int = 2000, alpha: float = 0.05
) -> CI:
    """Percentile-bootstrap CI for the mean of xs. Deterministic given rng."""
    n = len(xs)
    if n == 0:
        return CI(0.0, 0.0, 0.0)
    means = []
    for _ in range(iters):
        sample = [xs[rng.randrange(n)] for _ in range(n)]
        means.append(mean(sample))
    means.sort()
    return CI(
        mean(xs),
        _percentile(means, alpha / 2),
        _percentile(means, 1 - alpha / 2),
    )


def paired_diff_ci(
    a: list[float], b: list[float], rng, *, iters: int = 2000, alpha: float = 0.05
) -> CI:
    """Paired-bootstrap CI on mean(a - b), resampling the shared replicate
    INDEX so the CRN pairing is preserved. a[i] and b[i] must be the same
    replicate of two policies."""
    n = min(len(a), len(b))
    diffs = [a[i] - b[i] for i in range(n)]
    return bootstrap_mean(diffs, rng, iters=iters, alpha=alpha)
