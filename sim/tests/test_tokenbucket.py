# Direct unit tests for the 429 hazard model (the thing the sim exists to
# study). The engine only covers it directionally, so a <= vs < sign flip at the
# knee boundary (tokenbucket.throttle_prob) would slip past test_engine — pin
# the boundary cases here.

import math

from sim.tokenbucket import account_demand, throttle_prob


def test_account_demand_scales_by_fanout():
    assert account_demand(3, 1.0) == 3.0
    assert account_demand(3, 4.0) == 12.0
    assert account_demand(0, 4.0) == 0.0


def test_at_knee_is_no_hazard():
    # demand == k_a is headroom, not overshoot (the <= boundary).
    assert throttle_prob(8.0, 8.0, 1.0) == 0.0


def test_infinite_bucket_never_throttles():
    assert throttle_prob(8.0, float("inf"), 1.0) == 0.0


def test_nonpositive_knee_is_no_hazard():
    assert throttle_prob(1.0, 0.0, 1.0) == 0.0


def test_double_overshoot_one_minute():
    # demand = 2k -> overshoot 1.0 -> rate 1.0 -> 1 - e^-1 over one minute.
    assert throttle_prob(16.0, 8.0, 1.0) == 1.0 - math.exp(-1.0)


def test_longer_slice_raises_probability():
    p1 = throttle_prob(12.0, 8.0, 1.0)
    p2 = throttle_prob(12.0, 8.0, 2.0)
    assert 0.0 < p1 < p2 < 1.0
