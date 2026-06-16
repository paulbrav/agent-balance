# The engine is byte-stable under a fixed seed (CI runs py3.11–3.14), and its
# accounting is internally consistent.

import copy

from sim.calibrate import load_real
from sim.engine import Engine, Scenario
from sim.experiment import sim_config
from sim.policies import ProductionWaterfill
from sim.rng import SeedStreams
from sim.tests.conftest import FIXTURES


def _engine(seed=0, k_a=6.0, fanout=1.0):
    cal = load_real(FIXTURES, FIXTURES / "projects")
    scn = Scenario(k_a=k_a, intensity=1.0, fanout=fanout, horizon=3 * 3600.0)
    return Engine(
        accounts=copy.deepcopy(cal.accounts),
        fits=cal.fits,
        cfg=sim_config(),
        policy=ProductionWaterfill(),
        scenario=scn,
        seeds=SeedStreams(seed),
        start=1781576180.0,
    )


def test_trace_is_byte_stable():
    t1 = _engine(seed=0).trace()
    t2 = _engine(seed=0).trace()
    assert t1 == t2
    assert len(t1) > 0


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
