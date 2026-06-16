# sim/engine.py — the heapq discrete-event loop on a real-epoch clock.
#
# One run = one policy x one scenario x one replicate. The loop pops Events in
# (time, type, seq) order and evolves the world: instances arrive (LAUNCH) and
# pin to the policy's chosen account for life, toggle ON/OFF (Markov demand),
# burn their account's 5h sawtooth each BUCKET_TICK, draw 429s when account
# demand crosses the knee (THROTTLE), and reset their windows at r5/r7. The
# policy only ever sees a 60s-stale PROBE view, never the live clock — so a
# stale-view penalty is part of every comparison. Determinism: every draw is a
# seeded SeedStreams stream; ties in the heap break by EventType then seq.

from __future__ import annotations

import heapq
import itertools

from sim import demand, prod, tokenbucket
from sim.demand import DemandFit
from sim.events import Event, EventType
from sim.metrics import Metrics
from sim.policies import Policy
from sim.rng import SeedStreams
from sim.state import AccountState, Instance, WorldState

BUCKET_MIN = 1.0  # per-minute accounting cadence
PROBE_STALE = 60.0  # the policy's usage view lags the truth by this (production)
PROBE_EVERY = 60.0  # how often the stale view is refreshed
FIVE_WINDOW = 5 * 3600  # 5h window length (seconds)


class Scenario:
    """The knobs that define one experimental cell. arrivals_per_hour scales by
    `intensity`; `fanout` multiplies each ON instance's bucket demand; k_a is the
    swept per-account knee (applied to every account). horizon is the simulated
    span in seconds."""

    def __init__(
        self,
        *,
        k_a: float,
        intensity: float,
        fanout: float,
        horizon: float,
        arrivals_per_hour: float = 12.0,
        mean_lifetime: float = 1800.0,
    ) -> None:
        self.k_a = k_a
        self.intensity = intensity
        self.fanout = fanout
        self.horizon = horizon
        self.arrivals_per_hour = arrivals_per_hour
        self.mean_lifetime = mean_lifetime


class Engine:
    """A single seeded run. Build with a calibrated set of AccountStates +
    DemandFits, a policy, a scenario, and a SeedStreams; run() returns the
    Metrics for this replicate."""

    def __init__(
        self,
        accounts: dict[str, AccountState],
        fits: dict[str, DemandFit],
        cfg: prod.Config,
        policy: Policy,
        scenario: Scenario,
        seeds: SeedStreams,
        start: float,
    ) -> None:
        self.fits = fits
        self.policy = policy
        self.scn = scenario
        self.seeds = seeds
        self.start = start
        for st in accounts.values():
            st.k_a = scenario.k_a  # the swept knee applies to every account
        self.world = WorldState(accounts=accounts, cfg=cfg, now=start)
        self.metrics = Metrics()
        self.heap: list[Event] = []
        self._seq = itertools.count()
        self._iid = itertools.count(1)
        # The policy's deliberately-stale usage view, refreshed at each PROBE.
        self.stale_stats: dict[str, prod.Usage] = {
            n: st.usage(start - PROBE_STALE) for n, st in accounts.items()
        }
        self.last_tick = start

    def _push(self, time: float, etype: EventType, payload: dict | None = None) -> None:
        heapq.heappush(
            self.heap, Event(time, etype, next(self._seq), payload or {})
        )

    # -- scheduling helpers (all randomness via named streams) --------------

    def _schedule_initial(self) -> None:
        end = self.start + self.scn.horizon
        # Window resets per account.
        for st in self.world.accounts.values():
            if 0 < st.r5 <= end:
                self._push(st.r5, EventType.FIVE_RESET, {"account": st.name})
            if 0 < st.r7 <= end:
                self._push(st.r7, EventType.SEVEN_RESET, {"account": st.name})
        # Per-minute accounting, stale-view refresh, and hazard probe.
        t = self.start
        while t <= end:
            self._push(t, EventType.BUCKET_TICK, {})
            self._push(t, EventType.PROBE, {})
            self._push(t, EventType.THROTTLE, {})
            t += 60.0
        # The arrival stream.
        self._schedule_arrivals(end)

    def _schedule_arrivals(self, end: float) -> None:
        rng = self.seeds.stream("arrivals")
        rate_per_sec = self.scn.arrivals_per_hour * self.scn.intensity / 3600.0
        if rate_per_sec <= 0:
            return
        t = self.start
        while True:
            gap = rng.expovariate(rate_per_sec)
            t += gap
            if t > end:
                break
            self._push(t, EventType.LAUNCH, {})

    def _toggle_time(self, on: bool, fit: DemandFit) -> float:
        """Next Markov sojourn length (seconds) for an instance now `on`."""
        rng = self.seeds.stream("toggle")
        mean = fit.mean_on if on else fit.mean_off
        return rng.expovariate(1.0 / max(mean, 1e-6)) * 60.0

    # -- event handlers -----------------------------------------------------

    def _on_launch(self, ev: Event) -> None:
        load = self.world.load()
        choice = self.policy.choose(self.world, self.stale_stats, load, ev.time)
        rng = self.seeds.stream("lifetime")
        lifetime = rng.expovariate(1.0 / self.scn.mean_lifetime)
        iid = next(self._iid)
        inst = Instance(
            iid=iid,
            account=choice,
            launched=ev.time,
            lifetime=lifetime,
            on=False,
            fanout=self.scn.fanout,
        )
        self.world.add_instance(inst)
        self.metrics.launches += 1
        # Wall-stall instrument: every account already at its 5h wall.
        if all(
            self.world.accounts[a.name].five + self.world.cfg.draw >= 100.0
            for a in self.world.account_list()
        ):
            self.metrics.wall_stalls += 1
        # Schedule its first ON toggle and its EXIT.
        fit = self.fits[choice]
        if not fit.starved:
            self._push(
                ev.time + self._toggle_time(False, fit),
                EventType.TURN_TOGGLE,
                {"iid": iid},
            )
        self._push(ev.time + lifetime, EventType.EXIT, {"iid": iid})

    def _on_toggle(self, ev: Event) -> None:
        iid = ev.payload["iid"]
        inst = self.world.instances.get(iid)
        if inst is None:
            return
        inst.on = not inst.on
        fit = self.fits[inst.account]
        nxt = ev.time + self._toggle_time(inst.on, fit)
        if nxt <= self.start + self.scn.horizon:
            self._push(nxt, EventType.TURN_TOGGLE, {"iid": iid})

    def _on_exit(self, ev: Event) -> None:
        self.world.remove_instance(ev.payload["iid"])

    def _on_bucket(self, ev: Event) -> None:
        dt = ev.time - self.last_tick
        load = self.world.load()
        # Concentration is time-weighted over the elapsed slice.
        if dt > 0:
            self.metrics.fold_slice(dt, load)
        # Burn each account's 5h window from its ON instances; accrue goodput.
        dt_min = max((ev.time - self.last_tick) / 60.0, 0.0) or BUCKET_MIN
        for name, st in self.world.accounts.items():
            fit = self.fits[name]
            for inst in self.world.instances.values():
                if inst.account != name or not inst.on:
                    continue
                inc = demand.on_increment(self.seeds.stream("burn"), fit)
                st.five = min(st.five + inc * dt_min, 100.0)
                # ON instance-seconds are the goodput denominator.
                self.metrics.busy_instance_seconds += dt * 1.0
                self.metrics.useful_goodput += dt * 1.0  # un-throttled by default
        self.last_tick = ev.time

    def _on_throttle(self, ev: Event) -> None:
        # For each account, demand = ON instances x fanout vs the knee k_a.
        # CRN: draw ONE hazard number per account in a load-INDEPENDENT order
        # (sorted names), unconditionally, so the shared 'hazard' stream advances
        # identically across policies — only the p-threshold differs by routing.
        rng = self.seeds.stream("hazard")
        for name in sorted(self.world.accounts):
            st = self.world.accounts[name]
            on_insts = [
                i
                for i in self.world.instances.values()
                if i.account == name and i.on
            ]
            r = rng.random()  # consumed every account, every THROTTLE (CRN)
            if not on_insts:
                continue
            dmd = tokenbucket.account_demand(len(on_insts), self.scn.fanout)
            p = tokenbucket.throttle_prob(dmd, st.k_a, BUCKET_MIN)
            if p > 0 and r < p:
                # A 429 burst: every ON instance on this account stalls for the
                # slice. Charge throttled-seconds and reclaim the goodput that
                # _on_bucket optimistically credited.
                self.metrics.throttle_events += 1
                for _inst in on_insts:
                    self.metrics.throttled_instance_seconds += 60.0
                    self.metrics.useful_goodput = max(
                        self.metrics.useful_goodput - 60.0, 0.0
                    )
                    st.throttled_seconds += 60.0

    def _on_five_reset(self, ev: Event) -> None:
        st = self.world.accounts[ev.payload["account"]]
        st.five = 0.0
        nxt = ev.time + FIVE_WINDOW
        st.r5 = int(nxt)
        if nxt <= self.start + self.scn.horizon:
            self._push(nxt, EventType.FIVE_RESET, {"account": st.name})

    def _on_seven_reset(self, ev: Event) -> None:
        st = self.world.accounts[ev.payload["account"]]
        # The week's unused allowance perished (SHAPE-ONLY — level not trusted).
        perished = max(100.0 - st.seven, 0.0)
        st.perished_weekly += perished
        self.metrics.perished_weekly += perished
        st.seven = 0.0
        nxt = ev.time + prod.WEEK
        st.r7 = int(nxt)
        if nxt <= self.start + self.scn.horizon:
            self._push(nxt, EventType.SEVEN_RESET, {"account": st.name})

    def _on_probe(self, ev: Event) -> None:
        # Refresh the policy's view, but stamped PROBE_STALE in the past so the
        # policy always acts on a 60s-old picture (the production PROBE lag).
        self.stale_stats = {
            n: st.usage(ev.time - PROBE_STALE)
            for n, st in self.world.accounts.items()
        }

    _HANDLERS = {
        EventType.FIVE_RESET: "_on_five_reset",
        EventType.SEVEN_RESET: "_on_seven_reset",
        EventType.BUCKET_TICK: "_on_bucket",
        EventType.TURN_TOGGLE: "_on_toggle",
        EventType.THROTTLE: "_on_throttle",
        EventType.PROBE: "_on_probe",
        EventType.LAUNCH: "_on_launch",
        EventType.EXIT: "_on_exit",
    }

    def run(self) -> Metrics:
        """Drain the heap; return this replicate's Metrics."""
        self._schedule_initial()
        end = self.start + self.scn.horizon
        while self.heap:
            ev = heapq.heappop(self.heap)
            if ev.time > end:
                break
            self.world.now = ev.time
            getattr(self, self._HANDLERS[ev.type])(ev)
        return self.metrics

    def trace(self) -> list[tuple[float, int, str]]:
        """A byte-stable (time, type, key) trace of the schedule for the
        determinism test — drains a fresh heap WITHOUT mutating metrics beyond
        what run() does. Used only by tests."""
        out: list[tuple[float, int, str]] = []
        self._schedule_initial()
        end = self.start + self.scn.horizon
        while self.heap:
            ev = heapq.heappop(self.heap)
            if ev.time > end:
                break
            self.world.now = ev.time
            getattr(self, self._HANDLERS[ev.type])(ev)
            key = str(ev.payload.get("iid", ev.payload.get("account", "")))
            out.append((round(ev.time, 3), int(ev.type), key))
        return out
