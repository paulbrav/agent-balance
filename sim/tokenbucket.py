# sim/tokenbucket.py — the per-minute throughput knee and its 429 hazard.
#
# The production cap k_a is a per-account concurrency knee in instance-
# equivalents: how many simultaneously-busy instances a single account's
# per-minute bucket absorbs before it starts shedding 429s. Here, instantaneous
# demand on an account = (number of ON instances) x fanout multiplier. When that
# demand exceeds k_a, each excess instance draws a 429 with a hazard that grows
# with the overshoot. k_a = inf means an infinite bucket: no hazard, ever — the
# regime that reproduces the PureUrgencyConcentrate storms only because finite
# k_a is what makes concentration cost anything.

from __future__ import annotations

import math


def account_demand(on_instances: int, fanout: float) -> float:
    """Instantaneous demand on one account in instance-equivalents: each ON
    instance contributes `fanout` (its subagents hit the same bucket)."""
    return on_instances * fanout


def throttle_prob(demand: float, k_a: float, dt_min: float) -> float:
    """Probability that at least one 429 fires on this account over a dt-minute
    slice, given current `demand` against the knee `k_a`.

    k_a = inf -> 0 (infinite bucket). At or below the knee -> 0 (headroom). Above
    it, the per-minute hazard rate scales with the fractional overshoot, and the
    slice probability is 1 - exp(-rate * dt) — a memoryless Poisson thinning, so
    a longer slice or a bigger overshoot both raise the chance, deterministically
    given the seeded draw the caller compares it against."""
    if math.isinf(k_a) or k_a <= 0:
        return 0.0
    if demand <= k_a:
        return 0.0
    overshoot = (demand - k_a) / k_a
    rate = overshoot  # per-minute hazard ~ fractional overshoot
    return 1.0 - math.exp(-rate * dt_min)
