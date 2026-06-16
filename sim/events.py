# sim/events.py — the discrete-event clock's event type and ordering.
#
# The engine runs a heapq of Events on a real-epoch clock. Ordering must be
# total and deterministic: ties on time break by event TYPE (a lower IntEnum
# fires first), then by an insertion sequence number, so two events scheduled
# for the same instant always fire in the same order across runs and Pythons.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class EventType(IntEnum):
    """Lower value fires first on a time tie. The ordering encodes the
    intended causality at an instant: window resets land before the bucket
    tick that reads utilization, which lands before launches/exits that
    change load, which land before the throttle probe that observes it."""

    FIVE_RESET = 0  # a 5h window rolled over -> util back to 0
    SEVEN_RESET = 1  # a 7d window rolled over -> weekly allowance refreshed
    BUCKET_TICK = 2  # per-minute accounting: accrue demand, burn 5h, accrue waste
    TURN_TOGGLE = 3  # an instance's Markov ON/OFF state flips
    THROTTLE = 4  # evaluate the 429 hazard against current demand vs k_a
    PROBE = 5  # refresh the policy's (deliberately stale) usage view
    LAUNCH = 6  # a new instance arrives; the policy pins it to an account
    EXIT = 7  # an instance finishes; its load is released


@dataclass(order=True)
class Event:
    """Heap entry. Only (time, type, seq) participate in ordering — `payload`
    is compare=False so two events never tie on the dict identity. seq is a
    monotonic insertion counter assigned by the engine, the final tiebreaker
    that makes the whole schedule a total order."""

    time: float
    type: EventType
    seq: int
    payload: dict = field(default_factory=dict, compare=False)
