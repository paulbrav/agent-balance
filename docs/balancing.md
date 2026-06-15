# How the balancer works — and why it's safe

## Verified facts

Established 2026-06-12 against Claude Code v2.1.175 on Linux (plaintext
credential backend), with controlled two-turn headless sessions
(`--input-format stream-json`) and string analysis of the installed binary.
That version lives in code as the `VERIFIED_CLAUDE_VERSION` constant — the
single machine-checked source of truth — which a soft, non-fatal `claude
--version` check (surfaced on `install` and in `status`) compares against the
installed major.minor:

1. **Credentials are re-read from disk on every turn.** Replacing
   `$CLAUDE_CONFIG_DIR/.credentials.json` with garbage between two turns made
   the next turn fail with `401 Invalid bearer token`; the session survived
   as a turn-level error rather than crashing.
2. **Cross-account hot-swap works mid-session.** Turn 1 on account A,
   atomic replace with account B's credentials, turn 2 succeeded on B.
   Nothing client-side pins a session to the account recorded in
   `.claude.json` — a stale `oauthAccount` there is cosmetic (`/status`).
3. **Credential writes are atomic temp-file + rename.** A symlinked
   `.credentials.json` would be replaced by a regular file on the first
   token refresh. The balancer therefore always copies, never symlinks.
4. **Token refresh writes back to the live dir.** The CLI's HTTP client
   refreshes on 401 and retries; the refreshed blob lands in whatever config
   dir the session runs against. Refresh-token rotation is unverified
   upstream, so the balancer assumes it rotates and harvests refreshed blobs
   back to the canonical account dir.
5. **Proxies and helpers are dead ends for subscription auth.** Setting
   `ANTHROPIC_BASE_URL` silently drops subscription OAuth (multiple upstream
   issues), and `apiKeyHelper` output is sent as API-key auth. The file swap
   is the only robust mechanism.
6. Every API response carries `anthropic-ratelimit-unified-{5h,7d}-utilization`
   headers, parsed internally by the CLI but not exposed to hooks or the
   statusline — which is why the balancer polls
   `https://api.anthropic.com/api/oauth/usage` instead (the endpoint
   agent-pick already uses, cached for 45 s per account).

## The tick

One idempotent pass, every 60 s, serialized by `flock` on
`$ROOT/.balancer-lock` (a second tick skips; `launch` waits up to 15 s):

1. **Harvest.** If the pool blob's hash differs from the recorded one,
   someone else wrote it. Identify the owner by the email `/login` writes
   into the pool's `.claude.json`: the installed account → token refresh;
   a different known account → manual `/login`, adopted as installed; an
   unknown email → warn once, mark `installed: unknown`, touch nothing.
   Then, regardless: whichever of pool / installed-account-home holds the
   newer `expiresAt` wins, and the other side is overwritten. This both
   harvests refreshed tokens home and replaces a stale pool copy after the
   account was used directly.
2. **Probe the installed account** (cache-first, 45 s TTL).
   `limited`/`error` probes never trigger a swap — the balancer refuses to
   act blind on transient endpoint states.
3. **Decide.** Three trigger classes, deliberately separated:
   - **hard** — no installed account, or its token expired/missing. Always
     acts immediately.
   - **roll** — the 5h wall: utilization at/above the threshold (a window
     whose known reset has passed counts as 0%), or the burn-rate
     projection fires — fresh probes append to a per-account history, and
     when `current% + slope × two tick intervals` crosses 100% the swap
     happens early. That's what makes the default 99% threshold safe: the
     probe cache (45 s) plus tick cadence (60 s) leave a blind spot a
     heavily parallel workflow could burn through, and the projection
     covers exactly that window. Stale numbers (rate-limited endpoint) are
     extrapolated forward by the same slope. Rolls are safety actions and
     **bypass the hysteresis gap** — they must never queue behind a recent
     optimization swap. (Residual risk: a burst starting from a cold
     history inside one blind spot; heavy users can set
     `AGENT_BALANCE_INTERVAL=30`.)
   - **soft** — the **rebalance pull**: at most every 5 minutes the whole
     fleet is probed, and if another feasible account's *urgency* beats
     the installed one's by `max(AGENT_BALANCE_PULL_MARGIN, 15%)`, the
     balancer rotates toward it proactively. Only soft swaps respect the
     minimum gap since the last swap (default 300 s); the margin doubles
     as hysteresis — after the pull no other account clears the bar.
4. **Pick** by *urgency* = weekly allowance remaining ÷ days to its reset,
   capacity-weighted — the required sustained burn rate to avoid wasting
   the week (the critical-ratio / least-laxity index from deadline
   scheduling; weekly allowance is use-it-or-lose-it). Feasibility first
   (5h room for a typical session, weekly not spent); ties go to the
   earlier reset (EDF), then 5h headroom; pull targets additionally need
   real 5h headroom (`5h% + draw < threshold`). Urgency diverges as a
   reset nears, so no special deadline case is needed, and level-loading
   earlier in the week keeps more accounts' 5h windows alive for bursts —
   in 200-seed simulation this index tracked an offline-lookahead oracle
   within tenths of a point on waste and blocked demand, beat the old
   schedule-pace metric everywhere, and an explicit standby-reserve guard
   proved strictly harmful (the index already preserves burst capacity).
   Nothing feasible → report and leave credentials in place.
5. **Install.** Journal the swap (`pending` in the state file), bootstrap
   the pool if needed (symlink `projects/`+`skills/` to the source account,
   copy `.claude.json`/`settings.json`), stamp the target's `oauthAccount`,
   atomically copy its credentials in (mode 600), record final state (which
   clears the journal), append to the swap log. The journal plus the
   meta-before-creds order make a crash at any point recoverable: the next
   harvest either finishes the swap or discards it, and never mistakes a
   half-applied swap for a token refresh.

State lives in `$ROOT/.balancer-state.json` (`installed`, `blob_sha256`,
`last_swap_epoch`, transiently `pending`) — in the accounts root, not the
cache, because losing the installed-account mapping is the one thing that
risks a broken refresh chain. If it's deleted or corrupted, the next harvest
re-identifies the pool blob by email and rebuilds it.

## Tuning the knobs

Four environment variables shape the tick described above. Each one maps onto
a specific part of the urgency / feasibility / projection machinery, so the
right value depends on which part you're trying to bias.

- **`AGENT_BALANCE_THRESHOLD`** (default `99`) — the roll trigger's static
  ceiling (step 3, the **roll** class). The default is deliberately high
  because it is never the only line of defense: the burn-rate projection rolls
  early whenever `current% + slope × two tick intervals` crosses 100%, and
  stale numbers are extrapolated forward by the same slope first. So a slow
  burn legitimately rides the window to 99%, while a heavily parallel workflow
  burning several %/minute is rolled with precisely the lead time its slope
  implies. Raising toward 100 squeezes each account harder; lowering it (e.g.
  `85`) buys a wider static margin for bursty workflows whose probe history is
  too cold for the projection to have a slope yet — the one residual blind
  spot the projection can't cover. (`AGENT_BALANCE_INTERVAL=30` attacks the
  same blind spot from the other side, by shrinking the window itself.)
- **`AGENT_BALANCE_DRAW`** (default `10`) — the 5h points one typical session
  is assumed to consume. It is the slack term in two feasibility tests in the
  **pick** step. `feasible_now` requires `5h% + draw < 100` (or an imminent
  reset), which is what stops the balancer from installing an account a single
  session would immediately wall. The rebalance pull additionally requires
  `5h% + draw < threshold` of any pull target, so a proactive swap never moves
  onto an almost-walled account. Raise `draw` for heavy sessions (more
  conservative — fewer accounts qualify as a swap target); lower it toward 0
  for light sessions, which keeps more accounts feasible and lets the pull use
  fuller accounts that a typical session won't exhaust.
- **`AGENT_BALANCE_PULL_MARGIN`** (default `10`, %/day) — the **soft**
  rebalance pull's both trigger and hysteresis. A target must beat the
  installed account's urgency (required %/day to clear its week) by
  `max(pull_margin, 0.15 × installed urgency)` before the balancer rotates
  toward it. The `0.15 × installed urgency` floor makes the effective margin
  scale up automatically as a reset deadline nears (where urgency diverges),
  so the pull gets *less* twitchy exactly when urgencies are large and noisy.
  Because the same margin is the post-swap hysteresis — after a pull, no other
  account clears the bar against the newly installed one — raising it yields
  fewer, calmer rotations. Setting it to `0` disables the pull and the
  ~5-minute whole-fleet probe (`PULL_CHECK`) altogether, dropping the
  balancer's API traffic to the installed account's ~1 req/min.
- **`AGENT_BALANCE_MIN_GAP`** (default `300`s) — the minimum seconds between
  *soft* swaps, applied in step 3 only to the `need == "soft"` branch. It is
  pure flap damping for the rebalance pull and nothing else: **hard** swaps
  (no account, expired/missing token) and **roll** swaps (the 5h wall, or the
  projection) deliberately bypass it, so a safety action can never queue behind
  a recent optimization swap. Raise it if proactive rotations feel chatty;
  it has no effect on how aggressively the balancer protects you from a wall.

The interaction worth remembering is between `threshold` and `draw`: together
they form the pull's headroom bar (`5h% + draw < threshold`). Raising the
threshold to ride accounts higher, without also raising `draw`, widens that
bar and lets the rebalance pull move onto fuller accounts.

## The refresh-rotation risk, quantified

The one real failure mode: Claude Code refreshes the token in the pool
(possibly rotating the refresh token), and the canonical copy in the account
dir goes stale. The harvest step bounds the stale window to one tick (60 s).
The worst case — a refresh followed by the machine dying inside that window —
costs one forced re-login on one account. Both sync directions use
newest-`expiresAt`-wins, so a stale side can never overwrite a fresh one.

## Troubleshooting

- **`installed: unknown` in status** — the pool holds credentials whose email
  matches no known account dir (e.g. `/login` to a brand-new account inside a
  pool session). The balancer won't write them anywhere; they'll be replaced
  on the next swap. To keep that account, log it into its own dir:
  `CLAUDE_CONFIG_DIR=~/.claude-accounts/<name> claude` → `/login`.
- **Timer not firing** — `systemctl --user is-active agent-balance.timer`;
  `journalctl --user -u agent-balance -n 20`. On headless machines, user
  units need lingering: `loginctl enable-linger $USER` — or skip systemd and
  use `agent-balance watch` / cron.
- **Probe shows `limited`** — the usage endpoint rate-limits per IP for a few
  seconds; it clears on its own. The balancer never swaps on a `limited`
  probe.
- **A pinned session on the same account** (e.g. `agent-pick --use alt2`
  while alt2 is installed in the pool) — both write the same canonical dir
  on refresh; the newest-wins sync handles it. The balancer may swap the
  *pool* away from alt2, but it never touches the pinned session's dir.
- **Roll everything back** — `agent-balance uninstall`, then
  `rm -rf ~/.claude-accounts/.active ~/.claude-accounts/.balancer-state.json
  ~/.claude-accounts/.balancer-lock`. Account dirs are untouched apart from
  harvested (fresher) tokens.
