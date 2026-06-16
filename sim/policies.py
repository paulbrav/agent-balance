# sim/policies.py — the six allocation policies under common random numbers.
#
# A Policy decides which account a LAUNCH pins to, given a 60s-stale usage view
# (PROBE) plus the live load. All six see the SAME demand/hazard draws (CRN), so
# the comparison isolates the routing rule. The headline baseline,
# ProductionWaterfill, does NOT reimplement anything: it calls the REAL
# pick_shard_target with effective_caps/cfg and replicates choose_shard_account's
# least-loaded None-fallback verbatim.

from __future__ import annotations

from typing import Protocol

from sim import prod, tokenbucket
from sim.state import WorldState


class Policy(Protocol):
    """Pin a launching instance to an account name.

    `stats` is the 60s-stale {name: Usage} view (what offline_view would have
    returned a probe-interval ago); `load` is the live {name: n_a} from the real
    account_load. Must return a name present in world.accounts."""

    name: str

    def choose(
        self,
        world: WorldState,
        stats: dict[str, prod.Usage],
        load: dict[str, int],
        now: float,
    ) -> str: ...


def _least_loaded(world: WorldState, load: dict[str, int]) -> str:
    """choose_shard_account's exact fallback: the least-loaded account, ties by
    name. The one place the production blind pick is mirrored."""
    accts = world.account_list()
    return min(accts, key=lambda a: (load.get(a.name, 0), a.name)).name


class ProductionWaterfill:
    """THE BASELINE — the shipped allocator. Wraps the real pick_shard_target
    against effective_caps(cfg, accounts) and replicates choose_shard_account's
    None-fallback to the least-loaded account. No paraphrase: this is what the
    tool does today."""

    name = "ProductionWaterfill"

    def choose(self, world, stats, load, now):
        accounts = world.account_list()
        caps = prod.effective_caps(world.cfg, accounts)
        target = prod.pick_shard_target(accounts, stats, load, caps, now, world.cfg)
        if target is None:
            return _least_loaded(world, load)
        return target.name


class StaticCapSweep:
    """Water-fill against a single flat cap k for every account (the
    instances_per_account knob with no per-account caps.json). Reuses the real
    allocator with a uniform cap map — the natural ablation of effective_caps's
    per-account learning."""

    name = "StaticCapSweep"

    def __init__(self, cap: int):
        self.cap = cap
        self.name = f"StaticCapSweep(k={cap})"

    def choose(self, world, stats, load, now):
        accounts = world.account_list()
        caps = {a.name: self.cap for a in accounts}
        target = prod.pick_shard_target(accounts, stats, load, caps, now, world.cfg)
        if target is None:
            return _least_loaded(world, load)
        return target.name


class ThrottleAwareEarlySpill:
    """Water-fill, but spill one instance earlier than the cap — reserve a slot
    of headroom against the per-minute knee. Models a more conservative
    SPILL_ALPHA: feasible + most-urgent among accounts below cap-1, else the
    real allocator's choice. Trades a little cache warmth for fewer 429s."""

    name = "ThrottleAwareEarlySpill"

    def choose(self, world, stats, load, now):
        accounts = world.account_list()
        caps = prod.effective_caps(world.cfg, accounts)
        tight = {n: max(1, c - 1) for n, c in caps.items()}
        target = prod.pick_shard_target(accounts, stats, load, tight, now, world.cfg)
        if target is None:
            return _least_loaded(world, load)
        return target.name


class PureSpread:
    """Join-the-shortest-queue: always the least-loaded feasible account,
    ignoring urgency. Maximally anti-concentration — spreads load thin to dodge
    the knee at the cost of fragmenting prompt caches and ignoring weekly
    waste."""

    name = "PureSpread"

    def choose(self, world, stats, load, now):
        return _least_loaded(world, load)


class PureUrgencyConcentrate:
    """Always the single highest-urgency feasible account (cap = inf), never
    spilling — exactly what an uncapped urgency allocator does. Reproduces the
    real-log storms: it piles every instance onto one account, so under a finite
    k_a it draws 429s in bursts. The cautionary baseline the caps were added to
    fix."""

    name = "PureUrgencyConcentrate"

    def choose(self, world, stats, load, now):
        accounts = world.account_list()
        big = {a.name: 10**9 for a in accounts}  # effectively uncapped
        target = prod.pick_shard_target(accounts, stats, big, big, now, world.cfg)
        if target is None:
            return _least_loaded(world, load)
        return target.name


class OracleOffline:
    """An upper-bound reference, NOT a deployable policy: it sees the TRUE live
    load and routes to the least-loaded account that still has knee headroom
    (k_a), falling back to least-loaded. Headroom is tested against the SAME 429
    hazard the engine uses — account_demand(on, fanout) vs k_a — so with fanout
    > 1 it admits only while ON*fanout stays under the bucket. With perfect
    concurrency information it never knowingly oversubscribes a finite bucket —
    the best a load-only allocator could do, bounding how much the real policies
    leave on the table."""

    name = "OracleOffline"

    def choose(self, world, stats, load, now):
        accounts = world.account_list()
        # True ON-demand per account right now (oracle: not the stale view).
        under = []
        for a in accounts:
            st = world.accounts[a.name]
            on_insts = [
                i for i in world.instances.values()
                if i.account == a.name and i.on
            ]
            # Derive fanout from an ON instance (engine stamps scn.fanout on it);
            # default 1.0 when this account has none ON yet.
            fanout = on_insts[0].fanout if on_insts else 1.0
            # Admit only if one MORE ON instance would still not exceed the knee
            # under the engine's demand = ON*fanout hazard (k_a may be inf).
            if tokenbucket.account_demand(len(on_insts) + 1, fanout) <= st.k_a:
                under.append(a)
        pool = under or accounts
        return min(pool, key=lambda a: (load.get(a.name, 0), a.name)).name


def default_policies(k_sweep: list[int]) -> list[Policy]:
    """The six-policy roster for an experiment. StaticCapSweep is instantiated
    once per swept k so the Pareto table shows the flat-cap frontier."""
    roster: list[Policy] = [
        ProductionWaterfill(),
        ThrottleAwareEarlySpill(),
        PureSpread(),
        PureUrgencyConcentrate(),
        OracleOffline(),
    ]
    roster.extend(StaticCapSweep(k) for k in k_sweep)
    return roster
