# ProductionWaterfill is NOT a paraphrase: its choice equals a direct call to
# the shipped pick_shard_target (with the same None-fallback). The other
# policies route as advertised.

from sim import prod
from sim.experiment import sim_config
from sim.policies import (
    OracleOffline,
    ProductionWaterfill,
    PureSpread,
    PureUrgencyConcentrate,
)
from sim.state import AccountState, Instance, WorldState
from sim.tests.conftest import NOW


def _world():
    cfg = sim_config()
    r5 = NOW + 7200
    accounts = {
        "main": AccountState("main", five=10.0, seven=10.0, r5=r5, r7=NOW + 6 * 86400),
        "alt2": AccountState("alt2", five=40.0, seven=50.0, r5=r5, r7=NOW + 2 * 86400),
        "alt1": AccountState("alt1", five=0.0, seven=0.0, r5=r5, r7=NOW + 5 * 86400),
    }
    return WorldState(accounts=accounts, cfg=cfg, now=NOW)


def _stats(world, now):
    return {n: st.usage(now) for n, st in world.accounts.items()}


def test_production_waterfill_matches_pick_shard_target():
    world = _world()
    # Put some live instances on accounts so load is non-trivial.
    for iid, acct in [(1, "main"), (2, "main"), (3, "alt2")]:
        world.add_instance(Instance(iid, acct, NOW, 100.0))
    stats = _stats(world, NOW)
    load = world.load()
    accounts = world.account_list()
    caps = prod.effective_caps(world.cfg, accounts)
    direct = prod.pick_shard_target(accounts, stats, load, caps, NOW, world.cfg)
    expected = (
        direct.name
        if direct is not None
        else min(accounts, key=lambda a: (load.get(a.name, 0), a.name)).name
    )
    assert ProductionWaterfill().choose(world, stats, load, NOW) == expected


def test_production_waterfill_none_fallback_is_least_loaded():
    # Force every account infeasible (5h walled, no imminent reset) so
    # pick_shard_target returns None and the policy falls back to least-loaded.
    world = _world()
    for st in world.accounts.values():
        st.five = 100.0
        st.r5 = 0  # no imminent reset -> not feasible
    world.add_instance(Instance(1, "main", NOW, 100.0))  # main most loaded
    stats = _stats(world, NOW)
    load = world.load()
    direct = prod.pick_shard_target(
        world.account_list(), stats, load,
        prod.effective_caps(world.cfg, world.account_list()), NOW, world.cfg,
    )
    assert direct is None  # precondition: production also returns None here
    # least-loaded, ties by name -> alt1 (load 0, alphabetically first of the 0s)
    assert ProductionWaterfill().choose(world, stats, load, NOW) == "alt1"


def test_pure_spread_picks_least_loaded():
    world = _world()
    for iid, acct in [(1, "main"), (2, "alt2")]:
        world.add_instance(Instance(iid, acct, NOW, 100.0))
    stats = _stats(world, NOW)
    load = world.load()
    assert PureSpread().choose(world, stats, load, NOW) == "alt1"  # load 0


def test_pure_urgency_concentrates():
    world = _world()
    stats = _stats(world, NOW)
    load = world.load()
    # alt2 has the highest urgency (most weekly used, nearest weekly reset).
    assert PureUrgencyConcentrate().choose(world, stats, load, NOW) == "alt2"


def test_oracle_routes_to_headroom():
    world = _world()
    # Saturate main with ON instances against a finite knee; oracle avoids it.
    for st in world.accounts.values():
        st.k_a = 2.0
    for iid in range(1, 4):
        inst = Instance(iid, "main", NOW, 100.0, on=True)
        world.add_instance(inst)
    stats = _stats(world, NOW)
    load = world.load()
    choice = OracleOffline().choose(world, stats, load, NOW)
    assert choice != "main"  # main is over its knee; oracle picks headroom
