# The engine is byte-stable under a fixed seed (CI runs py3.11–3.14), and its
# accounting is internally consistent.

import copy
import hashlib

from sim.calibrate import load_real
from sim.engine import Engine, Scenario
from sim.experiment import sim_config
from sim.policies import ProductionWaterfill, PureSpread, PureUrgencyConcentrate
from sim.rng import SeedStreams
from sim.tests.conftest import FIXTURES

# Captured once on Python 3.11 — pins the schedule across py3.11..3.14 so a
# silent cross-version drift in random/heap/float formatting is caught, not
# masked by comparing two runs in the same process. Regenerate ONLY on a
# deliberate engine change (e.g. the hazard-draw CRN fix).
GOLDEN_TRACE_SHA = "1f16dad4c4d99542cc66e5802a29773a7f95f3ff5b1d43230979894c51dbc690"


def _engine(seed=0, k_a=6.0, fanout=1.0, policy=None):
    cal = load_real(FIXTURES, FIXTURES / "projects")
    scn = Scenario(k_a=k_a, intensity=1.0, fanout=fanout, horizon=3 * 3600.0)
    return Engine(
        accounts=copy.deepcopy(cal.accounts),
        fits=cal.fits,
        cfg=sim_config(),
        policy=policy or ProductionWaterfill(),
        scenario=scn,
        seeds=SeedStreams(seed),
        start=1781576180.0,
    )


def test_trace_is_byte_stable():
    t1 = _engine(seed=0).trace()
    t2 = _engine(seed=0).trace()
    assert t1 == t2
    assert len(t1) > 0
    # Pin the literal golden so cross-version drift can't pass by self-comparison.
    got = hashlib.sha256(repr(t1).encode()).hexdigest()
    assert got == GOLDEN_TRACE_SHA


def test_hazard_stream_is_crn_aligned_across_policies():
    # The 'hazard' stream must advance identically regardless of routing, so a
    # paired (CRN) policy comparison measures the policy, not luck. Draw the same
    # number of hazard values per run no matter how concentrated the load is.
    def hazard_draws(policy):
        eng = _engine(seed=0, k_a=2.0, fanout=4.0, policy=policy)
        eng.run()
        # The hazard stream is the sole consumer of stream('hazard'); its draw
        # count is reproducible from the same seed and must match across policies.
        return eng.seeds.stream("hazard")

    spread = hazard_draws(PureSpread())
    concentrate = hazard_draws(PureUrgencyConcentrate())
    # Same seed + same number of draws -> the generators are in the same state.
    assert spread.getstate() == concentrate.getstate()


def test_different_seed_differs():
    assert _engine(seed=0).trace() != _engine(seed=1).trace()


def test_metrics_are_reproducible():
    m1 = _engine(seed=0, k_a=4.0, fanout=4.0).run().as_row()
    m2 = _engine(seed=0, k_a=4.0, fanout=4.0).run().as_row()
    assert m1 == m2


def test_goodput_within_bounds():
    m = _engine(seed=0).run()
    assert 0.0 <= m.goodput_fraction <= 1.0
    assert 0.0 <= m.concentration_index <= 1.0


def test_finite_knee_can_throttle():
    # A tight knee with high fanout must produce some throttling; inf must not.
    tight = _engine(seed=0, k_a=2.0, fanout=4.0).run()
    nohazard = _engine(seed=0, k_a=float("inf"), fanout=4.0).run()
    assert tight.throttled_instance_seconds > 0
    assert nohazard.throttled_instance_seconds == 0.0
