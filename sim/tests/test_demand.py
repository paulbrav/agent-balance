# The demand fitter recovers the calibration bands and reproduces the starved
# (flat-zero) regime exactly — alt1/alt3 get NO endogenous demand.

from sim.calibrate import read_history
from sim.demand import fit_demand, on_increment
from sim.rng import SeedStreams
from sim.tests.conftest import FIXTURES


def test_flat_zero_is_starved():
    hist = read_history(FIXTURES / "alt1.history")
    fit = fit_demand(hist)
    assert fit.starved
    assert fit.p_busy == 0.0
    assert fit.on_mean == 0.0


def test_active_account_fits_in_bands():
    hist = read_history(FIXTURES / "main.history")
    fit = fit_demand(hist)
    assert not fit.starved
    assert 0.30 <= fit.p_busy <= 0.40  # P(busy) band
    assert 0.20 <= fit.on_mean <= 0.65  # ON increment mean band
    assert 0.43 <= fit.on_cv <= 0.65  # CV band


def test_dwell_realizes_p_busy():
    fit = fit_demand(read_history(FIXTURES / "main.history"))
    realized = fit.mean_on / (fit.mean_on + fit.mean_off)
    assert abs(realized - fit.p_busy) < 1e-9


def test_starved_increment_is_zero():
    fit = fit_demand(read_history(FIXTURES / "alt3.history"))
    rng = SeedStreams(0).stream("burn")
    assert on_increment(rng, fit) == 0.0


def test_increment_is_deterministic():
    fit = fit_demand(read_history(FIXTURES / "main.history"))
    a = [on_increment(SeedStreams(3).stream("burn"), fit) for _ in range(1)]
    b = [on_increment(SeedStreams(3).stream("burn"), fit) for _ in range(1)]
    assert a == b
