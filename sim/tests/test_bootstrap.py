# The paired bootstrap is deterministic (seeded) and preserves CRN pairing.

import random

from sim.bootstrap import bootstrap_mean, paired_diff_ci


def test_bootstrap_is_deterministic():
    xs = [1.0, 2.0, 3.0, 4.0, 5.0]
    a = bootstrap_mean(xs, random.Random(0), iters=200)
    b = bootstrap_mean(xs, random.Random(0), iters=200)
    assert a == b
    assert a.point == 3.0
    assert a.lo <= a.point <= a.hi


def test_paired_diff_preserves_pairing():
    # b is always a + 5 -> the paired difference is a constant -5, with a tight
    # CI excluding zero (a real CRN-significant effect).
    a = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    b = [x + 5.0 for x in a]
    ci = paired_diff_ci(a, b, random.Random(1), iters=300)
    assert abs(ci.point - (-5.0)) < 1e-9
    assert ci.excludes_zero
    assert ci.hi < 0


def test_no_effect_includes_zero():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = list(a)  # identical -> diff is zero everywhere
    ci = paired_diff_ci(a, b, random.Random(2), iters=300)
    assert ci.point == 0.0
    assert not ci.excludes_zero


def test_empty_is_safe():
    ci = bootstrap_mean([], random.Random(0))
    assert ci.point == 0.0
