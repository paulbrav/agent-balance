# sim/ — dev-only allocation-policy backtest harness

A seeded discrete-event simulation that evaluates account-usage **allocation
policies** against the patterns in the **real** agent-balance logs. It exists to
answer one question without shipping a single new line into the tool: *given
what the logs actually show, how much does the sharding allocator's routing
choice matter, and where does it sit on the throttle-vs-concentration frontier?*

**This tree is NOT shipped.** The wheel `only-include`s `agent_balance.py` and
the tray; `sim/` is a developer artifact. The core is stdlib-only (bootstrap
CIs and the ASCII/CSV Pareto table need nothing beyond the standard library);
`matplotlib` is optional behind the `sim` extra and fully import-guarded.

## How to run

```sh
# Face-validity gate: replay the real swaps + 429 + history timelines and print
# a CALIBRATED / DIRECTIONAL verdict.
uv run python -m sim.run gate \
  --cache ~/.cache/agent-balance --projects ~/.claude/projects

# Pareto experiment: CRN replicate sweep over the (k_a, intensity, fanout) grid.
uv run python -m sim.run experiment \
  --replicates 20 --seed 0 --k 6,8,12,16,inf --intensity 1 --fanout 1,4 \
  --cache ~/.cache/agent-balance --projects ~/.claude/projects

# CSV instead of ASCII, and an optional Pareto PNG (needs the [sim] extra):
uv run python -m sim.run experiment --csv ...
uv run --extra sim python -m sim.run experiment --png /tmp/pareto.png ...
```

The single import boundary is `sim/prod.py`: it re-exports the **real** shipped
API read-only (the conftest.py sys.path pattern). The baseline policy
`ProductionWaterfill` IS the shipped `pick_shard_target` (+ the
`choose_shard_account` None-fallback) — never a paraphrase. A drift in
`agent_balance.py` surfaces as an ImportError here, not as a stale copy.

## What the policies are

Six policies run under **common random numbers** (each sees the SAME seeded
demand/hazard draws, so the comparison isolates the routing rule):

- **ProductionWaterfill** — the shipped allocator (urgency water-fill against
  `effective_caps`), the baseline.
- **StaticCapSweep(k)** — water-fill against one flat cap k (the
  `instances_per_account` knob with no per-account `caps.json`).
- **ThrottleAwareEarlySpill** — spill one slot earlier (a more conservative
  `SPILL_ALPHA`), trading cache warmth for fewer 429s.
- **PureSpread** — join-the-shortest-queue; maximally anti-concentration.
- **PureUrgencyConcentrate** — uncapped urgency (cap=inf); reproduces the
  real-log **storms** that piled every instance onto one account.
- **OracleOffline** — an upper bound (sees true live load); not deployable.

## Identifiability table

What the real logs pin down, what they only shape, and what is unobserved and
therefore **swept** (never assumed at a point):

| Quantity | Source | Identifiability |
|---|---|---|
| 5h-utilization demand **shape** (P(busy), ON-increment mean/CV) | `<acct>.history` differenced series | **Fitted** per account from the slope distribution |
| Demand-**starved** regime (alt1, alt3) | flat-zero `.history` | **Reproduced exactly** — zero endogenous demand; load only if a policy routes there |
| 5h reset cadence (sawtooth) | `.history` downward steps + snapshot `r5` | **Confirmed** — visible window rollovers |
| Quota seed (`five`, `seven`, `r5`, `r7`) | `<acct>` snapshot (via `usage_from_cache`) | **Point** — one instantaneous read per account |
| Weekly (`seven`) **time series** | — | **NOT observed** — snapshot carries one instantaneous value → perished weekly allowance is **[SHAPE-ONLY]** and never declares a winner |
| Which account was installed | `swaps` ledger | **Observed timeline** |
| 429 incidents (count, timing, account) | `projects/**/*.jsonl` `THROTTLE_MARKER` | **Observed** (deduped by timestamp); 100% on the installed account |
| Per-minute knee `k_a` | ramp bench lower-bounded it >10 | **UNOBSERVED → SWEPT axis** `{6,8,12,16,24,inf}`, never a point |
| Per-minute rate, arrival **intensity**, subagent **fanout** | — | **UNOBSERVED → SWEPT / assumed-prior** |

## The face-validity gate

Before any policy comparison may claim a winner, three falsifiable checks must
hold against the real timelines (`sim/facevalidity.py`):

1. **>= 80%** of 429s landed on whatever account was **installed** at the time.
   (Real logs: **100%**.)
2. 429s are **decoupled from the 5h quota wall** — they cluster low-to-mid
   utilization, not at the 100% limit (evidence of a *throughput knee*, not a
   usage limit). Reported alongside the share inside the tight **8–23%** band.
3. The `.history` **sawtooth resets** at `r5` (visible downward steps).

When all three hold the regime is **CALIBRATED** and the experiment compares
inside the finite-`k_a` cells. `k_a = inf` is always **DIRECTIONAL ONLY**: an
infinite bucket can never 429, so it cannot reproduce the storms.

## Caveats (read before trusting a number)

- **perished weekly allowance is [SHAPE-ONLY].** The cache holds one
  instantaneous `seven` per account, no weekly series, so the *level* of
  perished allowance is not identifiable. The metric is computed and shown but
  **never breaks the Pareto frontier** and never names a winner.
- **`k_a` is a swept axis, not a point.** The ramp bench only lower-bounded the
  knee (>10 on one account). Every result is read across the `k_a` sweep; a
  conclusion that flips with `k_a` is reported as such.
- **No single winner is declared.** The Pareto surface over
  `(k_a, intensity, fanout)` IS the result; `*` marks the non-dominated policies
  per cell on the (throttled-seconds, concentration) objectives.
- **History coverage is short.** Only the recent tail of the 429 stream has
  `.history` coverage; the gate checks the band on the covered subset and says
  so explicitly.

## Determinism

Every draw goes through a seeded `SeedStreams` (named, independent streams) —
never the global `random`, never the wall clock; `now`/seeds are injected
(mirroring `tests/conftest.py`: `NOW = 1_700_000_000`). The engine's event
schedule is byte-stable under a fixed seed (a test pins it), so CI on
py3.11–3.14 reproduces identical traces and metrics.

## Module map

- `prod.py` — the single read-only import boundary into the shipped tool.
- `rng.py` — `SeedStreams` (seeded named streams; CRN).
- `state.py` — `AccountState` / `Instance` / `WorldState` (keeps a production-
  shaped `leases` dict so policies call the real `account_load`).
- `events.py` — `Event(order=True)` + `EventType` IntEnum; heap key
  `(time, type, seq)`.
- `demand.py` — Markov-modulated ON/OFF accumulator + the history fitter.
- `tokenbucket.py` — per-account knee and the 429 hazard.
- `engine.py` — the heapq discrete-event loop on a real-epoch clock.
- `policies.py` — the six policies (Policy Protocol).
- `calibrate.py` — load the real logs; fit demand; parse swaps + 429s.
- `metrics.py` — per-replicate instruments.
- `bootstrap.py` — stdlib paired bootstrap CIs.
- `facevalidity.py` — the gate.
- `report.py` — ASCII/CSV Pareto surface (optional PNG behind `[sim]`).
- `experiment.py` — the CRN replicate loop over the grid.
- `run.py` — the `gate` / `experiment` CLI.
