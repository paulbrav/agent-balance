"""reset_in — the reset-countdown format shared by cmd_status and the tray."""

import pytest
from conftest import NOW

import agent_balance as ab


@pytest.mark.parametrize(
    ("epoch", "label"),
    [
        (0, "-"),  # unknown reset beats "now"
        (NOW - 5, "now"),
        (NOW + 59, "1m"),  # minutes are a ceiling...
        (NOW + 3599, "60m"),  # ...up to the full hour
        (NOW + 3600, "1h"),
        (NOW + 5400, "2h"),  # hours and days round, not floor
        (NOW + 86399, "24h"),
        (NOW + 2 * 86400, "2d"),
    ],
)
def test_reset_in(epoch, label):
    assert ab.reset_in(epoch, NOW) == label
