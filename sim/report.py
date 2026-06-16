# sim/report.py — the Pareto surface over (k_a, intensity, fanout).
#
# Two headline objectives, both to MINIMIZE: throttled-instance-seconds (429
# pain) and concentration index (cache-fragmentation / blast-radius risk). A
# policy is Pareto-dominated when another beats it on both, within the same grid
# cell. The table is ASCII by default and CSV on request; perished weekly
# allowance is shown but tagged [SHAPE-ONLY] and NEVER used to break the
# frontier. There is deliberately NO single declared winner — the surface is the
# result. matplotlib is optional behind the `sim` extra and fully guarded.

from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass

from sim.bootstrap import CI, bootstrap_mean
from sim.experiment import CellResults

# Objectives to minimize for the Pareto frontier (perished_weekly excluded —
# SHAPE-ONLY, never decides dominance).
PARETO_METRICS = ("throttled_instance_seconds", "concentration_index")
# Everything the table prints, with display tags.
SHOWN_METRICS = (
    ("throttled_instance_seconds", "throttle_s", ""),
    ("goodput_fraction", "goodput", ""),
    ("concentration_index", "concentr", ""),
    ("wall_stalls", "5h_stalls", ""),
    ("perished_weekly", "perished", "[SHAPE-ONLY]"),
)


@dataclass
class Summary:
    """One policy's per-metric mean + CI within one cell, plus its Pareto rank."""

    policy: str
    cell_label: str
    metrics: dict[str, CI]
    dominated: bool


def summarize_cell(cell: CellResults, seed: int = 0) -> list[Summary]:
    """Bootstrap each policy's metrics in one cell, then flag Pareto-dominated
    policies on the (throttled_seconds, concentration) objectives."""
    rng = random.Random(seed)
    summaries: list[Summary] = []
    points: dict[str, tuple[float, ...]] = {}
    for name, rep in cell.by_policy.items():
        cis: dict[str, CI] = {}
        for metric, _, _ in SHOWN_METRICS:
            cis[metric] = bootstrap_mean(rep.values(metric), rng, iters=500)
        summaries.append(
            Summary(name, cell.cell.label, cis, dominated=False)
        )
        points[name] = tuple(cis[m].point for m in PARETO_METRICS)

    # Pareto domination: A dominates B if A <= B on all objectives and < on one.
    for s in summaries:
        a = points[s.policy]
        for other, b in points.items():
            if other == s.policy:
                continue
            if all(bi <= ai for ai, bi in zip(a, b, strict=True)) and any(
                bi < ai for ai, bi in zip(a, b, strict=True)
            ):
                s.dominated = True
                break
    return summaries


def ascii_table(cells: list[CellResults], seed: int = 0) -> str:
    """A human-readable Pareto table over all cells. '*' marks a Pareto-optimal
    (non-dominated) policy within its cell. No single winner is declared."""
    lines: list[str] = []
    header = (
        f"{'cell':<22} {'policy':<26} "
        + " ".join(f"{lbl:>11}" for _, lbl, _ in SHOWN_METRICS)
        + "  pareto"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for cell in cells:
        for s in summarize_cell(cell, seed):
            cols = []
            for metric, _, _ in SHOWN_METRICS:
                cols.append(f"{s.metrics[metric].point:>11.2f}")
            mark = " " if s.dominated else "*"
            lines.append(
                f"{s.cell_label:<22} {s.policy:<26} "
                + " ".join(cols)
                + f"  {mark}"
            )
        lines.append("")
    lines.append(
        "* = Pareto-optimal in its cell on (throttle_s, concentr). "
        "perished [SHAPE-ONLY]: no weekly series in the cache, never a winner."
    )
    return "\n".join(lines)


def csv_table(cells: list[CellResults], seed: int = 0) -> str:
    """The same surface as CSV (one row per policy x cell)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(
        ["cell", "policy"]
        + [lbl for _, lbl, _ in SHOWN_METRICS]
        + [f"{lbl}_lo" for _, lbl, _ in SHOWN_METRICS]
        + [f"{lbl}_hi" for _, lbl, _ in SHOWN_METRICS]
        + ["pareto_optimal"]
    )
    for cell in cells:
        for s in summarize_cell(cell, seed):
            row = [s.cell_label, s.policy]
            row += [f"{s.metrics[m].point:.4f}" for m, _, _ in SHOWN_METRICS]
            row += [f"{s.metrics[m].lo:.4f}" for m, _, _ in SHOWN_METRICS]
            row += [f"{s.metrics[m].hi:.4f}" for m, _, _ in SHOWN_METRICS]
            row += ["1" if not s.dominated else "0"]
            w.writerow(row)
    return buf.getvalue()


def maybe_plot_png(cells: list[CellResults], path: str, seed: int = 0) -> str | None:
    """Optional matplotlib scatter of the Pareto surface, behind the `sim`
    extra. Returns the written path, or None when matplotlib isn't installed —
    the import is fully guarded so ty/runtime never fail without it."""
    try:
        import matplotlib  # ty: ignore[unresolved-import]

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # ty: ignore[unresolved-import]
    except ImportError:
        return None
    fig, ax = plt.subplots()
    for cell in cells:
        for s in summarize_cell(cell, seed):
            x = s.metrics["concentration_index"].point
            y = s.metrics["throttled_instance_seconds"].point
            ax.scatter(x, y, marker="o" if not s.dominated else "x")
            ax.annotate(s.policy, (x, y), fontsize=6)
    ax.set_xlabel("concentration index")
    ax.set_ylabel("throttled instance-seconds")
    ax.set_title("Pareto surface (lower-left is better)")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path
