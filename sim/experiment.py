# sim/experiment.py — the CRN replicate loop over the (k_a, intensity, fanout)
# grid.
#
# For each grid cell and each replicate, every policy is run on the SAME seeded
# draws (common random numbers): the replicate's SeedStreams is derived from
# (top seed, cell, replicate) ALONE, never from the policy, so policy A and
# policy B at replicate i share an identical demand/hazard realization. That
# pairing is what bootstrap.paired_diff_ci consumes. The grid sweeps k_a as a
# first-class axis (never a point) because the per-minute knee is UNOBSERVED.

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from sim import prod
from sim.calibrate import Calibration, calibration_start
from sim.engine import Engine, Scenario
from sim.metrics import ReplicateResults
from sim.policies import Policy, default_policies
from sim.rng import SeedStreams

DEFAULT_HORIZON = 12 * 3600.0  # 12h simulated span per replicate


def sim_config() -> prod.Config:
    """A production Config for the sim: threshold 99 / draw 10 (the shipped
    defaults), min_gap 0 (the engine has no swap-gap), rooted at a placeholder
    path the pick path never touches on disk."""
    return prod.Config(
        root=Path("/sim"),
        cache=Path("/sim"),
        threshold=99,
        min_gap=0,
        interval=60,
        draw=10,
        pull_margin=0,
    )


@dataclass(frozen=True)
class Cell:
    """One grid cell: a knee, an arrival-intensity multiplier, a fanout."""

    k_a: float
    intensity: float
    fanout: float

    @property
    def label(self) -> str:
        k = "inf" if self.k_a == float("inf") else f"{self.k_a:g}"
        return f"k={k},i={self.intensity:g},f={self.fanout:g}"


@dataclass
class CellResults:
    """All policies' replicate metrics for one cell, keyed by policy name."""

    cell: Cell
    by_policy: dict[str, ReplicateResults] = field(default_factory=dict)


def build_cells(
    k_grid: list[float], intensities: list[float], fanouts: list[float]
) -> list[Cell]:
    return [
        Cell(k, i, f)
        for k in k_grid
        for i in intensities
        for f in fanouts
    ]


def run_cell(
    cal: Calibration,
    cell: Cell,
    policies: list[Policy],
    *,
    replicates: int,
    seed: int,
    horizon: float = DEFAULT_HORIZON,
    cfg: prod.Config | None = None,
) -> CellResults:
    """Run every policy over `replicates` CRN replicates of one cell. Each
    replicate's seed family is fixed by (seed, cell, replicate); a deep copy of
    the calibrated accounts gives every (policy, replicate) a fresh quota
    state."""
    if cfg is None:
        cfg = sim_config()
    start = calibration_start(cal)
    top = SeedStreams(seed).derive(f"cell:{cell.label}")
    results = CellResults(cell=cell)
    for pol in policies:
        results.by_policy[pol.name] = ReplicateResults(policy=pol.name)
    for r in range(replicates):
        rep_seeds = top.derive(f"rep:{r}")
        scn = Scenario(
            k_a=cell.k_a,
            intensity=cell.intensity,
            fanout=cell.fanout,
            horizon=horizon,
        )
        for pol in policies:
            # Fresh world per (policy, replicate); SAME seeds across policies.
            accounts = copy.deepcopy(cal.accounts)
            eng = Engine(
                accounts=accounts,
                fits=cal.fits,
                cfg=cfg,
                policy=pol,
                scenario=scn,
                seeds=rep_seeds,
                start=start,
            )
            metrics = eng.run()
            results.by_policy[pol.name].rows.append(metrics.as_row())
    return results


def run_experiment(
    cal: Calibration,
    *,
    k_grid: list[float],
    intensities: list[float],
    fanouts: list[float],
    replicates: int,
    seed: int,
    horizon: float = DEFAULT_HORIZON,
) -> list[CellResults]:
    """The full grid x replicate sweep. StaticCapSweep is instantiated over the
    FINITE swept knees so the flat-cap frontier shows up in the table."""
    finite_k = [int(k) for k in k_grid if k != float("inf")]
    policies = default_policies(finite_k)
    cells = build_cells(k_grid, intensities, fanouts)
    return [
        run_cell(
            cal, cell, policies, replicates=replicates, seed=seed, horizon=horizon
        )
        for cell in cells
    ]
