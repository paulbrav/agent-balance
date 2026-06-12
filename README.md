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
there is unimplemented). Python 3.10+, stdlib only.

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
  it picks the best other feasible account (agent-pick's weekly-pace policy,
  capacity-weighted) and atomically copies its credentials into the pool.
  Every running pool session follows on its next turn.
- **Burn-rate projection** makes the high threshold safe: the tick tracks
  the slope of recent probes, and if `current% + rate × two ticks` crosses
  100%, it swaps early regardless of the threshold. Slow burn rides the
  window to 99%; an 8-wide workflow burning several %/minute rotates with
  exactly the margin it needs.
- **Deadline pull:** weekly allowance is use-it-or-lose-it, so every ~10
  minutes the tick also checks whether another account's weekly window
  expires soon (default within 24h) with substantially more allowance left
  than the installed account's pace (default 20 weighted points) — and
  rotates toward it even though the installed account is fine. Sessions
  don't notice; the expiring allowance gets used instead of wasted.
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
| `AGENT_BALANCE_PULL_HOURS` | `24` | deadline pull: rotate toward a weekly window expiring within this many hours (`0` disables) |
| `AGENT_BALANCE_PULL_MARGIN` | `20` | ...only when its weighted weekly pace beats the installed account's by at least this many points |
| `AGENT_PICK_ROOT` | `~/.claude-accounts` | accounts root, shared with agent-pick |

## Tray indicator (GNOME)

`agent-balance-tray` puts the account table in your system tray: the icon
label shows the installed account and its 5h usage, and the dropdown renders
every account's 5h/7d windows as colored bars (the agent-pick `--list` look),
plus "Tick now". It reads the balancer's own probe cache, so it adds no API
load.

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
  request/minute (installed account only; the fleet is probed only when a
  swap is actually due).
- Rotating accounts to extend usage limits is not an endorsed pattern —
  this automates what `/login` does by hand, on your own paid accounts. Use
  your own judgment (same caveat as agent-pick).

## Verified mechanics

The design rests on behavior verified against Claude Code v2.1.175 (Linux)
with controlled two-turn sessions — swapping the credentials file between
turns for garbage (next turn fails `401`) and for a second valid account
(next turn succeeds on the other account). Details, the tick algorithm, and
troubleshooting live in [docs/balancing.md](docs/balancing.md).

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
