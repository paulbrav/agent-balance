# sim/tests/conftest.py — hermetic fixtures for the sim suite.
#
# Tests NEVER read ~/.cache or ~/.claude; everything loads from the committed
# sim/fixtures tree. NOW mirrors tests/conftest.py's injected-clock convention
# so the determinism guarantees line up with the production suite.

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Redundant under `uv run pytest` (editable install); kept so a bare system
# `pytest` works from a fresh clone — the production conftest convention.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
NOW = 1_700_000_000  # shared with tests/conftest.py — never the wall clock


@pytest.fixture
def fixtures() -> Path:
    """The committed fixtures dir (cache-shaped: snapshots, .history, swaps)."""
    return FIXTURES


@pytest.fixture
def projects() -> Path:
    """The committed projects tree (jsonl with deduped 429 markers)."""
    return FIXTURES / "projects"
