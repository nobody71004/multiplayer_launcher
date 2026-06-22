"""Shared constants for the custom-engine contract.

These constants are the single point of change if you swap the engine stub:

  ENGINE_BIN_REL  - relative path the launcher spawns
  EXPECTED_ARGV   - ordered arg names the engine MUST accept
  PROTOCOL_VERSION- bump if you change the contract; launcher emits a warning
                   if the engine reports an older version

Real engine integration: replace `game_stub.py` with a shim that proxies
its argv into your engine binary. Everything else (the launcher's
`GAME_STUB_REL`, scripts, tests) still references these constants, so
no caller-side change is required.
"""

from __future__ import annotations

from pathlib import Path

ENGINE_BIN_REL = Path("game_engine") / "game_stub.py"

EXPECTED_ARGV = ("--server", "--port", "--token", "--username")

PROTOCOL_VERSION = 1  # bump when the argv contract changes
