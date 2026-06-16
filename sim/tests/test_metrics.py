# Direct unit tests for the per-replicate instruments. test_engine only checks
# these are in-bounds; pin the boundary semantics (empty/single-account
# concentration, the no-busy goodput default) so a regression is caught here.

from sim.metrics import Metrics


def test_empty_load_is_not_concentrated():
    m = Metrics()
    m.fold_slice(60.0, {})  # idle fleet -> share 0
    assert m.concentration_index == 0.0


def test_single_account_is_fully_concentrated():
    m = Metrics()
    m.fold_slice(60.0, {"main": 5})
    assert m.concentration_index == 1.0


def test_even_split_is_half_concentrated():
    m = Metrics()
    m.fold_slice(60.0, {"main": 2, "alt": 2})
    assert m.concentration_index == 0.5


def test_goodput_is_one_when_nothing_busy():
    m = Metrics()
    assert m.busy_instance_seconds == 0.0
    assert m.goodput_fraction == 1.0


def test_goodput_fraction_is_useful_over_busy():
    m = Metrics()
    m.busy_instance_seconds = 100.0
    m.useful_goodput = 75.0
    assert m.goodput_fraction == 0.75


def test_concentration_is_time_weighted():
    m = Metrics()
    m.fold_slice(10.0, {"main": 1})  # share 1.0 for 10s
    m.fold_slice(30.0, {"main": 1, "alt": 1})  # share 0.5 for 30s
    assert m.concentration_index == (1.0 * 10.0 + 0.5 * 30.0) / 40.0
