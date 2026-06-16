# The gate PASSES in a constructed box (the committed fixtures reproduce the
# real-log shape) and the calibrated region drops k_a=inf to DIRECTIONAL.

from sim.calibrate import Calibration, Swap, load_real
from sim.demand import fit_demand
from sim.facevalidity import calibrated_region, evaluate
from sim.state import AccountState
from sim.tests.conftest import FIXTURES


def test_gate_passes_in_constructed_box():
    cal = load_real(FIXTURES, FIXTURES / "projects")
    res = evaluate(cal)
    assert res.passed
    assert res.verdict == "CALIBRATED"
    assert res.installed_share == 1.0  # all fixture 429s on the installed main
    assert res.decoupled_share == 1.0  # all below the quota wall
    assert res.saw_reset  # the main.history fixture has a sawtooth reset


def test_calibrated_region_excludes_inf():
    cal = load_real(FIXTURES, FIXTURES / "projects")
    region = calibrated_region(cal, [6.0, float("inf")], [1.0, 4.0])
    assert (6.0, 1.0) in region
    assert (6.0, 4.0) in region
    assert all(k != float("inf") for k, _ in region)  # inf can never 429


def test_no_429s_goes_directional():
    # A box with no throttle incidents cannot confirm the model.
    accounts = {"main": AccountState("main")}
    fits = {"main": fit_demand([(0, 0.0), (60, 0.0)])}
    cal = Calibration(accounts=accounts, fits=fits, swaps=[], throttle_epochs=[])
    res = evaluate(cal)
    assert not res.passed
    assert res.verdict == "DIRECTIONAL ONLY"


def test_429s_at_wall_go_directional():
    # 429s that only ever happen near the 5h quota wall are NOT decoupled ->
    # the throughput-knee model is not confirmed -> DIRECTIONAL.
    hist = [(0, 95.0), (60, 96.0), (120, 97.0), (180, 50.0)]  # has a reset
    accounts = {"main": AccountState("main")}
    fits = {"main": fit_demand(hist)}
    cal = Calibration(
        accounts=accounts,
        fits=fits,
        swaps=[Swap(0, "unknown", "main")],
        throttle_epochs=[60.0, 120.0],  # both at ~96% util — at the wall
        history={"main": hist},
    )
    res = evaluate(cal)
    assert res.installed_share == 1.0
    assert res.decoupled_share == 0.0  # none below the wall
    assert not res.passed
