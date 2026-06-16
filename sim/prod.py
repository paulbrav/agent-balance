# sim/prod.py — the single import boundary into the shipped tool.
#
# The baseline policy IS the shipped code, never a paraphrase: every name the
# sim leans on is re-exported here read-only, so a drift in agent_balance.py
# surfaces as an ImportError at the boundary rather than as a silently stale
# copy of pick_shard_target buried in the harness. Mirrors tests/conftest.py:
# prepend the repo root to sys.path, then import the single-file module.

from __future__ import annotations

import sys
from pathlib import Path

# Repo root is sim/'s parent. Prepend (not append) so a system-installed
# agent_balance can't shadow the working tree — the conftest.py convention.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import agent_balance as ab  # noqa: E402

# Types / dataclasses (frozen contracts the sim seeds and reads).
Config = ab.Config
Usage = ab.Usage
Account = ab.Account
Candidate = ab.Candidate

# Pure policy functions — the baseline allocator and its building blocks.
normalized = ab.normalized
urgency = ab.urgency
feasible_now = ab.feasible_now
candidates = ab.candidates
rank = ab.rank
pick_shard_target = ab.pick_shard_target
account_load = ab.account_load

# Cache / caps helpers (calibrate seeds AccountState through usage_from_cache).
usage_from_cache = ab.usage_from_cache
effective_caps = ab.effective_caps
read_caps = ab.read_caps

# Constants the sim must honor (the production None-fallback uses these).
RESET_SOON = ab.RESET_SOON
WEEK = ab.WEEK
INSTANCES_PER_ACCOUNT = ab.INSTANCES_PER_ACCOUNT
SPILL_ALPHA = ab.SPILL_ALPHA
THROTTLE_MARKER = ab.THROTTLE_MARKER

__all__ = [
    "Account",
    "Candidate",
    "Config",
    "INSTANCES_PER_ACCOUNT",
    "RESET_SOON",
    "SPILL_ALPHA",
    "THROTTLE_MARKER",
    "Usage",
    "WEEK",
    "ab",
    "account_load",
    "candidates",
    "effective_caps",
    "feasible_now",
    "normalized",
    "pick_shard_target",
    "rank",
    "read_caps",
    "urgency",
    "usage_from_cache",
]
