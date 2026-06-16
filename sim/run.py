# sim/run.py — the dev-only CLI: `python -m sim.run gate|experiment ...`.
#
# `gate` replays the real swaps + 429 + history timelines through the
# face-validity checks and prints a CALIBRATED / DIRECTIONAL verdict. `experiment`
# runs the CRN replicate sweep over the (k_a, intensity, fanout) grid and prints
# the Pareto surface. Both default to the real cache/projects trees but accept
# explicit paths so tests stay hermetic. Nothing here writes to the cache; the
# sim is read-only against the shipped tool's state.

from __future__ import annotations

import argparse
from pathlib import Path

from sim import facevalidity, report
from sim.calibrate import load_real
from sim.experiment import run_experiment


def _parse_k_grid(spec: str) -> list[float]:
    """Parse '6,8,12,16,inf' into [6.0, 8.0, ..., inf]. 'inf' is a first-class
    point (no hazard) — k_a is a swept axis, never assumed."""
    out: list[float] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        out.append(float("inf") if tok.lower() == "inf" else float(tok))
    return out


def _parse_floats(spec: str) -> list[float]:
    return [float(t) for t in spec.split(",") if t.strip()]


def cmd_gate(args: argparse.Namespace) -> int:
    cal = load_real(Path(args.cache).expanduser(), Path(args.projects).expanduser())
    res = facevalidity.evaluate(cal)
    print("agent-balance sim — face-validity gate on REAL logs")
    print(f"  accounts loaded     : {', '.join(sorted(cal.accounts))}")
    print(f"  swaps in ledger     : {len(cal.swaps)}")
    print(f"  unique 429 incidents: {res.n_429}")
    inst_ok = res.installed_share >= facevalidity.INSTALLED_SHARE_MIN
    print(
        f"  (i)   managed installed: {res.installed_share * 100:5.1f}% "
        f"(need >= {facevalidity.INSTALLED_SHARE_MIN * 100:.0f}%) "
        f"-> {'PASS' if inst_ok else 'FAIL'}"
    )
    cover_floor = max(1, int(facevalidity.COVERAGE_MIN_FRAC * res.n_429))
    band_ok = (
        res.n_band_covered >= cover_floor
        and res.n_band_covered > 0
        and res.decoupled_share >= facevalidity.DECOUPLED_SHARE_MIN
    )
    print(
        f"  (ii)  decoupled (<{facevalidity.WALL:.0f}% 5h): "
        f"{res.decoupled_share * 100:5.1f}% of {res.n_band_covered}/{res.n_429} "
        f"covered (need >= {facevalidity.DECOUPLED_SHARE_MIN * 100:.0f}% and "
        f">= {facevalidity.COVERAGE_MIN_FRAC:.0%} coverage; "
        f"{res.band_share * 100:.0f}% in tight 8-23% band) "
        f"-> {'PASS' if band_ok else 'FAIL'}"
    )
    print(
        f"  (iii) 5h sawtooth     : {res.n_resets} resets seen "
        f"-> {'PASS' if res.saw_reset else 'FAIL'}"
    )
    for note in res.notes:
        print(f"  note: {note}")
    print(f"  VERDICT: {res.verdict}")
    if res.passed:
        print(
            "  Calibrated regime confirmed: the experiment may compare policies "
            "inside finite-k_a cells; k_a=inf stays DIRECTIONAL (no hazard)."
        )
    else:
        print(
            "  Model does not fully reproduce the logs -> all experiment results "
            "are DIRECTIONAL ONLY (watermark every Pareto claim)."
        )
    return 0


def cmd_experiment(args: argparse.Namespace) -> int:
    cal = load_real(Path(args.cache).expanduser(), Path(args.projects).expanduser())
    k_grid = _parse_k_grid(args.k)
    intensities = _parse_floats(args.intensity)
    fanouts = _parse_floats(args.fanout)
    gate = facevalidity.evaluate(cal)
    region = facevalidity.calibrated_region(cal, k_grid, fanouts)
    cells = run_experiment(
        cal,
        k_grid=k_grid,
        intensities=intensities,
        fanouts=fanouts,
        replicates=args.replicates,
        seed=args.seed,
    )
    print("agent-balance sim — Pareto experiment on REAL logs")
    print(
        f"  grid: k_a={args.k}  intensity={args.intensity}  fanout={args.fanout}  "
        f"replicates={args.replicates}  seed={args.seed}"
    )
    print(f"  face-validity gate : {gate.verdict}")
    if region:
        labels = ", ".join(f"(k={k:g},f={f:g})" for k, f in region)
        print(f"  calibrated region  : {labels}")
    else:
        print(
            "  calibrated region  : EMPTY -> results DIRECTIONAL ONLY "
            "(no finite-k_a cell reproduced the real 429 storms)"
        )
    print()
    if args.csv:
        out = report.csv_table(cells, seed=args.seed)
    else:
        out = report.ascii_table(cells, seed=args.seed)
    print(out)
    if args.png:
        written = report.maybe_plot_png(cells, args.png, seed=args.seed)
        if written:
            print(f"\n  wrote PNG: {written}")
        else:
            print("\n  matplotlib not installed (pip install 'agent-balance[sim]')")
    print(
        "\n  No single winner is declared: the Pareto surface above IS the "
        "result. perished_weekly is [SHAPE-ONLY] (no weekly time series in the "
        "cache) and never breaks the frontier."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sim.run",
        description="dev-only allocation-policy simulation/backtest on real logs",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("gate", help="face-validity verdict on the real logs")
    g.add_argument("--cache", default="~/.cache/agent-balance")
    g.add_argument("--projects", default="~/.claude/projects")
    g.set_defaults(func=cmd_gate)

    e = sub.add_parser("experiment", help="CRN Pareto sweep on the real logs")
    e.add_argument("--cache", default="~/.cache/agent-balance")
    e.add_argument("--projects", default="~/.claude/projects")
    e.add_argument("--replicates", type=int, default=20)
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--k", default="6,8,12,16,inf", help="swept k_a knees")
    e.add_argument("--intensity", default="1", help="arrival-intensity multipliers")
    e.add_argument("--fanout", default="1,4", help="subagent fanout multipliers")
    e.add_argument("--csv", action="store_true", help="emit CSV instead of ASCII")
    e.add_argument("--png", default="", help="optional Pareto PNG path ([sim] extra)")
    e.set_defaults(func=cmd_experiment)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
