# agent-balance

Swap Claude accounts **beneath running sessions** — before the 5-hour limit
kills your workflow.

A running Claude Code instance re-reads `$CLAUDE_CONFIG_DIR/.credentials.json`
from disk on **every turn** (verified empirically against v2.1.175). Atomically
replacing that file mid-session reroutes the session to another account on its
next turn — no `/login`, no restart, no lost workflow. agent-balance automates
exactly that: a 60-second tick watches the active account's usage windows and
installs the best other account's credentials before the wall is hit, so a
100-subagent workflow doesn't die at subagent #94.

Sister project to [agent-pick](https://github.com/paulbrav/agent-pick) (the
launch-time account picker); shares its account layout and selection policy.

```
$ agent-balance status
accounts (/home/you/.claude-accounts):
  main         you@example.com                  5h  62% (2h)   7d  71% (3d)
  alt1         you+1@example.com                5h  12% (4h)   7d  34% (5d) <- installed
  alt2         you+2@example.com                5h   0% (-)    7d  18% (2d)

pool: /home/you/.claude-accounts/.active (exists)
installed: alt1 (last swap 41m ago)
timer: active

$ journalctl --user -u agent-balance -f
agent-balance: alt1 at 84% 5h — ok
agent-balance: swapped alt1 -> alt2 (you+2@example.com) — 5h 0%, 7d 18% [5h at 86% >= 85]
```

Linux only (macOS keeps Claude credentials in the Keychain; hot-swapping
there is unimplemented). Python 3.11+, stdlib only.

## How it works

```
~/.claude                        the "main" account        (canonical creds)
~/.claude-accounts/<name>/       one Anthropic login each  (canonical creds)
~/.claude-accounts/.active/      the POOL — sessions run here; holds a COPY
                                 of whichever account the balancer installed
```

- Sessions launch with `CLAUDE_CONFIG_DIR=~/.claude-accounts/.active`
  (`agent-balance launch`, or export the variable yourself). The pool's
  `projects/` and `skills/` are symlinked to the source account, so history
  and skills are shared.
- Every 60s a tick polls the installed account's 5h/7d windows (the same
  OAuth usage endpoint agent-pick reads, cached 45s). When the 5-hour window
  crosses the threshold (default 99%) — or the installed token is expired —
  it rolls to the most urgent other feasible account and atomically copies
  its credentials into the pool. Every running pool session follows on its
  next turn. These safety rolls are exempt from swap hysteresis.
- **Burn-rate projection** makes the high threshold safe: the tick tracks
  the slope of recent probes, and if `current% + rate × two ticks` crosses
  100%, it swaps early regardless of the threshold. Slow burn rides the
  window to 99%; an 8-wide workflow burning several %/minute rotates with
  exactly the margin it needs.
- **Urgency-driven account choice.** Accounts are ranked by *required burn
  rate*: weekly allowance remaining ÷ days until its reset (capacity
  weighted), ties to the earlier reset. Weekly allowance is
  use-it-or-lose-it, so the account that needs the highest sustained rate
  to clear its week is served first — urgency diverges as a reset
  approaches (no special "deadline" case needed) while level-loading
  earlier in the week, which naturally keeps more accounts' 5h windows
  alive for bursts. In simulation this index matched an offline-lookahead
  oracle within tenths of a percent on both waste and blocked demand, and
  beat the previous schedule-pace metric on every scenario.
- **Rebalance pull:** every ~5 minutes the tick probes the whole fleet
  and proactively swaps when another account's urgency beats the installed
  one's by a margin (default 10%/day, scaled up for high urgencies — the
  margin doubles as flap hysteresis). Sessions don't notice; expiring
  allowance gets used instead of wasted.
- **Write-back sync:** Claude Code refreshes OAuth tokens in the live dir,
  and refresh tokens must be assumed to rotate. Each tick reconciles the pool
  with the installed account's home dir — whichever side holds the newer
  token wins — so refreshed tokens are harvested back home and a stale pool
  copy is replaced. A manual `/login` inside a pool session is recognized by
  email and adopted.

Two rules keep this safe: credentials are **always copied, never symlinked**
(Claude Code rewrites the file atomically and would replace a symlink), and
the balancer **never installs an expired or infeasible account's blob** — if
nothing else is feasible it leaves the current credentials in place and says
so.

## Install

```bash
git clone https://github.com/paulbrav/agent-balance.git
cd agent-balance
./install.sh                 # -> ~/.local/bin/agent-balance
agent-balance install        # write + enable the 60s systemd user timer
```

No systemd user session? `agent-balance install` falls back to printing a
crontab line, or run `agent-balance watch` in a tmux pane.

**Then route `claude` through the pool (recommended).** Bare `claude` reads
`~/.claude` — your unbalanced main account — until you point it at the pool.
Add this to `~/.bashrc` (or `~/.zshrc`) and open a new terminal:

```bash
export CLAUDE_CONFIG_DIR="$HOME/.claude-accounts/.active"
```

From then on every `claude` runs on whichever account the balancer has
installed, and gets hot-swapped beneath you before limits hit. Sessions that
were already running on `~/.claude` keep their old account until relaunched.
`agent-pick --use <name>` still pins a real account dir — agent-pick sets the
variable itself, overriding the shell default.

## Use

```bash
agent-balance status         # accounts, installed creds, timer health, recent swaps
agent-balance launch         # tick, then exec claude on the pool (args pass through)
agent-balance tick           # one manual balance pass
agent-balance watch          # foreground loop (no systemd needed)
agent-balance uninstall      # remove the timer; pool dir is left in place
```

With the `CLAUDE_CONFIG_DIR` export from the Install section in place, bare
`claude` is already balanced — `agent-balance launch` remains useful for
one-off runs without the export. agent-pick keeps working unchanged:
`agent-pick --use alt2` still pins a real account dir and bypasses the pool
entirely.

Tuning (env vars, also honored by the systemd unit if set at install time):

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_BALANCE_THRESHOLD` | `99` | swap when the installed account's 5h window reaches this % (the burn-rate projection can swap earlier) |
| `AGENT_BALANCE_MIN_GAP` | `300` | minimum seconds between threshold-driven swaps (expired tokens bypass this) |
| `AGENT_BALANCE_INTERVAL` | `60` | tick cadence for `watch` and the timer |
| `AGENT_BALANCE_DRAW` | `10` | 5h points a typical session is assumed to need (feasibility gate) |
| `AGENT_BALANCE_PULL_MARGIN` | `10` | rebalance pull: proactively swap when another account's urgency (required %/day) beats the installed one's by this margin (`0` disables) |
| `AGENT_PICK_ROOT` | `~/.claude-accounts` | accounts root, shared with agent-pick |

### Tuning the knobs

The table is the reference; this is how to think about changing the four
behavioral knobs. They divide cleanly: **threshold** and **draw** govern when
a *single* account is too full to ride or to move onto, **pull_margin** and
**min_gap** govern the *proactive* rebalance between accounts.

- **`AGENT_BALANCE_THRESHOLD`** (default `99`) — how high to ride the 5h
  window before a forced roll. It's safe at 99 because the burn-rate
  projection swaps early whenever the recent slope would cross 100% within
  two ticks, so a slow burn rides the window to the wall while a fast,
  parallel burst rotates with exactly the margin it needs. Raise toward 100
  to squeeze every last point out of each account; lower it (e.g. `85`) for a
  wider safety margin on bursty multi-agent workflows, where the 2-tick
  lookahead can under-cover a burst that starts from a cold probe history.
- **`AGENT_BALANCE_DRAW`** (default `10`) — the 5h points a typical session is
  assumed to consume. It feeds two gates: feasibility (`5h% + draw < 100`, so
  the balancer won't install an account a single session would immediately
  wall) and the rebalance pull's headroom check (`5h% + draw < threshold`, so
  a proactive swap never lands on an almost-full account). Raise it for heavy
  sessions; lower it toward 0 for light ones, which keeps more accounts
  feasible.
- **`AGENT_BALANCE_PULL_MARGIN`** (default `10`, %/day) — the rebalance pull's
  trigger and its hysteresis in one number. Setting it to `0` disables
  proactive rebalancing entirely (and with it the ~5-minute whole-fleet
  probe, so the balancer sends no extra API traffic). The effective margin is
  `max(pull_margin, 0.15 × installed urgency)`, so it auto-scales up near a
  reset deadline; raise it for fewer, calmer rotations.
- **`AGENT_BALANCE_MIN_GAP`** (default `300`s) — minimum seconds between
  *soft* (rebalance) swaps; pure flap damping. Hard rolls (a 5h wall) and
  expired-token swaps bypass it, so safety actions never wait out a gap. Raise
  it if proactive rotations feel chatty.

Note the coupling: **threshold and draw together set the pull's headroom bar**
(`5h% + draw < threshold`), so raising `threshold` without also raising `draw`
lets the pull move onto fuller accounts.

## Tray indicator (GNOME)

`agent-balance-tray` puts the account table in your system tray: the icon
label shows the installed account and its 5h usage, and the dropdown renders
every account's 5h/7d windows as colored bars (the agent-pick `--list` look),
plus "Rebalance now". It reads the balancer's own probe cache, so it adds no
API load; the "Refresh" item forces one staggered fleet probe on demand.

```bash
agent-balance-tray                       # foreground
agent-balance-tray --install-autostart   # start at every login
```

Needs the system Python's GObject bindings and an AppIndicator-capable shell
(stock Ubuntu GNOME qualifies): `apt install python3-gi
gir1.2-ayatanaappindicator3-0.1` if missing.

## Caveats

- **This tool writes credentials.** The pool dir is fully balancer-owned, and
  the write-back sync copies refreshed tokens into the matching account dir's
  `.credentials.json`. That's the whole point — but know it before running it.
- Refresh-token rotation desync is bounded by the tick cadence: worst case
  (a token refreshed and the machine dying inside the same minute) is one
  forced re-login on one account.
- The pool's `.claude.json`/`settings.json` (MCP servers, model prefs) are a
  bootstrap-time snapshot of the source account. `rm -rf
  ~/.claude-accounts/.active` rebuilds it on the next tick.
- The usage endpoint is observed CLI behavior, not a documented public API,
  and enforces a short per-IP rate limit. Steady-state the balancer sends ~1
  request/minute for the installed account, plus a staggered whole-fleet
  sweep every ~5 minutes for the rebalance pull (~0.6 req/min extra with 4
  accounts; none with `AGENT_BALANCE_PULL_MARGIN=0`) — under the endpoint's
  measured ~2-2.5 req/min per-IP allowance.
- Rotating accounts to extend usage limits is not an endorsed pattern —
  this automates what `/login` does by hand, on your own paid accounts. Use
  your own judgment (same caveat as agent-pick).

## Verified mechanics

The design rests on behavior verified against Claude Code v2.1.175 (Linux)
with controlled two-turn sessions — swapping the credentials file between
turns for garbage (next turn fails `401`) and for a second valid account
(next turn succeeds on the other account). That verified version is encoded as
the `VERIFIED_CLAUDE_VERSION` constant in `agent_balance.py` (the single
machine-checked source of truth); `status` and `install` run a soft, non-fatal
`claude --version` check and warn when the installed major.minor differs.
Details, the tick algorithm, and troubleshooting live in
[docs/balancing.md](docs/balancing.md).

## Development

Tooling is managed with [uv](https://docs.astral.sh/uv/) — `pyproject.toml`
carries the metadata and the dev toolchain (pytest, ruff, ty). The *runtime*
stays stdlib-only on purpose, so `install.sh` keeps working on any box with a
system python, no venv required.

```bash
uv sync                         # .venv with the dev group
uv run pytest                   # unit tests, no network, no real accounts
uv run ruff check . && uv run ruff format --check .
uv run ty check                 # type check (tray excluded: gi has no stubs)

AGENT_BALANCE_LIVE=1 uv run pytest tests/test_hotswap_live.py -v
# ^ end-to-end rehearsal: starts a real two-turn haiku session on a scratch
#   pool and swaps accounts beneath it between the turns
```

## License

MIT
