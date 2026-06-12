# How the balancer works — and why it's safe

## Verified facts

Established 2026-06-12 against Claude Code v2.1.175 on Linux (plaintext
credential backend), with controlled two-turn headless sessions
(`--input-format stream-json`) and string analysis of the installed binary:

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
3. **Decide.** Hard need: no installed account, or its token is
   expired/missing. Soft need: 5h utilization at or above the threshold
   (a window whose known reset has passed counts as 0%). Soft swaps respect
   a minimum gap since the last swap (default 300 s) so near-threshold noise
   can't flap; hard need bypasses the gap — a dead token must not be
   hysteresis-limited.
4. **Pick** the best *other* account using agent-pick's policy: feasibility
   first (5h room for a typical session, weekly not spent), then the account
   furthest behind its capacity-weighted weekly pace; in-band ties go to 5h
   headroom. Nothing feasible → report and leave credentials in place.
5. **Install.** Bootstrap the pool if needed (symlink `projects/`+`skills/`
   to the source account, copy `.claude.json`/`settings.json`), atomically
   copy the target's credentials in (mode 600), stamp its `oauthAccount`,
   record state, append to the swap log.

State lives in `$ROOT/.balancer-state.json` (`installed`, `blob_sha256`,
`last_swap_epoch`) — in the accounts root, not the cache, because losing the
installed-account mapping is the one thing that risks a broken refresh chain.
If it's deleted or corrupted, the next harvest re-identifies the pool blob by
email and rebuilds it.

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
