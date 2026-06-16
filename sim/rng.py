# sim/rng.py — seeded randomness with named, independent streams.
#
# Determinism is a hard requirement (CI runs py3.11–3.14 and the byte-stability
# test pins an event trace). Every draw in the sim goes through a SeedStreams
# instance: NEVER the global random module, NEVER the wall clock. Common random
# numbers (CRN) across policies means each policy replays the SAME demand /
# hazard draws — so a policy comparison measures the policy, not luck. CRN is
# achieved by deriving every stream's seed from (base_seed, name) alone, so a
# given (seed, replicate, stream) yields the same sequence regardless of which
# policy is running.

from __future__ import annotations

import hashlib
import random


def _derive(base: int, name: str) -> int:
    """A stable 63-bit seed from (base, name) — independent of import order,
    dict iteration, or which policy is consuming the stream. SHA-256 over the
    text so renaming a stream reshuffles only that stream, not the rest."""
    h = hashlib.sha256(f"{base}:{name}".encode()).digest()
    return int.from_bytes(h[:8], "big") & ((1 << 63) - 1)


class SeedStreams:
    """A family of independent random.Random generators keyed by name.

    stream("demand") and stream("hazard") never interfere, and both are
    reproducible from the base seed alone. Pass a SeedStreams (or its named
    sub-streams) explicitly into every component — there is no global state."""

    def __init__(self, seed: int) -> None:
        self.seed = int(seed)
        self._streams: dict[str, random.Random] = {}

    def stream(self, name: str) -> random.Random:
        """The Random for `name`, created once and cached. Same name ->
        same object -> a continuing sequence within one run."""
        rng = self._streams.get(name)
        if rng is None:
            rng = random.Random(_derive(self.seed, name))
            self._streams[name] = rng
        return rng

    def derive(self, name: str) -> SeedStreams:
        """A child SeedStreams whose base seed is derived from this one and
        `name` — used to give each replicate its own independent family while
        keeping the whole experiment reproducible from one top seed."""
        return SeedStreams(_derive(self.seed, name))
