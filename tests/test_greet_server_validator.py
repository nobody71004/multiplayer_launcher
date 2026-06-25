"""Pytest wrapping of tools/_validator_smoke.DEFAULT_SCENARIOS.

Each scenario in the catalogue becomes its own pytest test so a
regression names the exact validator combination that broke. The
harness itself (process management, port readiness, parity-line
capture) lives in tools/_validator_smoke.py and is reused by the
``tools-smoke`` CI job -- so testing it inline here is purely a
convenience for `pytest tests/ -v` runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

# Importing the harness appends nothing to a fixture graph; the
# scenario catalogue is a pure-data module-level list.
from _validator_smoke import DEFAULT_SCENARIOS, run_scenario  # noqa: E402


@pytest.mark.parametrize(
    "scenario",
    DEFAULT_SCENARIOS,
    ids=[s["name"] for s in DEFAULT_SCENARIOS],
)
def test_validator_scenario(scenario: dict) -> None:
    ok, msg = run_scenario(scenario, verbose=False)
    assert ok, msg
