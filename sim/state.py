# sim/state.py — the mutable world the engine evolves and policies read.
#
# AccountState carries the 5h/7d sawtooth the engine burns and resets; Instance
# is one pinned-for-life session; WorldState ties them together and — crucially
# — keeps a `leases` dict shaped exactly like the production lease registry
# ({iid: {"account": name}}), so a policy can call the REAL account_load against
# it instead of a sim-private re-implementation of n_a.

from __future__ import annotations

from dataclasses import dataclass, field

from sim import prod


@dataclass
class AccountState:
    """One account's evolving quota windows plus its calibrated demand and
    hazard parameters. `five`/`seven` are utilization percentages; r5/r7 are
    the reset epochs (the engine reschedules them on rollover). capacity and
    k_a feed the production urgency / water-fill math unchanged."""

    name: str
    capacity: float = 1.0
    five: float = 0.0  # 5h window utilization %
    seven: float = 0.0  # 7d window utilization %
    r5: int = 0  # next 5h reset epoch
    r7: int = 0  # next 7d reset epoch
    # Calibrated demand (Markov-modulated ON/OFF accumulator, per instance):
    p_busy: float = 0.0  # stationary P(an instance is ON)
    on_mean: float = 0.0  # mean %/min added to 5h while ON (Gamma mean)
    on_cv: float = 0.5  # coefficient of variation of the ON increment
    mean_on: float = 5.0  # mean ON dwell (minutes) — Markov sojourn
    mean_off: float = 10.0  # mean OFF dwell (minutes)
    # Hazard: per-minute throughput knee in instance-equivalents. inf -> the
    # account never 429s (the no-hazard sweep point).
    k_a: float = float("inf")
    # Bookkeeping accumulated over the run (read by metrics).
    perished_weekly: float = 0.0  # 7d allowance that expired unused [SHAPE-ONLY]
    throttled_seconds: float = 0.0  # instance-seconds spent under a 429

    def usage(self, asof: float) -> prod.Usage:
        """A production Usage snapshot of this account's current windows — what
        a probe would have returned at `asof`. Policies consume Usage, never
        AccountState, so the boundary stays the shipped contract."""
        return prod.Usage(self.five, self.seven, self.r5, self.r7, asof=asof)

    def account(self) -> prod.Account:
        """A production Account carrying name + capacity (the only fields the
        allocator reads). home/email are unused by the pick path, so they get
        placeholder values."""
        from pathlib import Path

        return prod.Account(self.name, Path("/sim") / self.name, "", self.capacity)


@dataclass
class Instance:
    """One launched session, pinned to an account at LAUNCH and never
    reassigned (production pins a PID to an account for its life). `on` is the
    Markov ON/OFF demand state; `fanout` is its subagent multiplier."""

    iid: int
    account: str
    launched: float
    lifetime: float  # seconds until its EXIT
    on: bool = False
    fanout: float = 1.0


@dataclass
class WorldState:
    """The whole simulated fleet. `accounts` is the per-account quota+demand
    state; `instances` is the live set; `leases` mirrors the production lease
    registry so policies call the real account_load. `now` is the engine's
    current epoch (set by the loop; policies read a stale PROBE view, not this).
    """

    accounts: dict[str, AccountState]
    cfg: prod.Config
    now: float = 0.0
    instances: dict[int, Instance] = field(default_factory=dict)
    leases: dict[int, dict] = field(default_factory=dict)

    def account_list(self) -> list[prod.Account]:
        """Production Account objects in a stable (sorted) order — the
        allocator iterates this; sorted keeps ties deterministic."""
        return [self.accounts[n].account() for n in sorted(self.accounts)]

    def load(self) -> dict[str, int]:
        """Live instances per account via the REAL account_load over the
        production-shaped lease map — never a sim-private count."""
        return prod.account_load(self.leases)

    def add_instance(self, inst: Instance) -> None:
        self.instances[inst.iid] = inst
        self.leases[inst.iid] = {"account": inst.account, "started": int(inst.launched)}

    def remove_instance(self, iid: int) -> None:
        self.instances.pop(iid, None)
        self.leases.pop(iid, None)
