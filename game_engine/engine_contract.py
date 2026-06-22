"""Shared constants for the custom-engine contract.

This module is the SINGLE EDIT-POINT for swapping the engine. To wire a
real custom-engine binary:

  1. Set `ENGINE_BIN` to the binary's path (absolute, or relative to the
     matchmaker's working directory). If unset (None), the shim at
     `game_engine/game_stub.py` falls back to its built-in stub-loop
     behavior -- which is what tests assert + what runs in cold-start dev.
  2. Optionally set `ENGINE_ARGV_PREFIX` to a tuple of args to inject
     BEFORE the launcher's `--server --port --token --username` flags.
     Example: ENGINE_ARGV_PREFIX = ("--join",) for an engine that
     expects a verb flag before the connection target.

After editing, save and run normally. The shim auto-detects the change.

Testing-only env-var overrides (NOT the canonical config):
  - ENGINE_BIN_OVERRIDE        - absolute path to engine binary
  - ENGINE_ARGV_PREFIX_OVERRIDE - space-separated args to prepend

These only apply when subprocess env is set; engine_contract.ENGINE_BIN
remains the canonical (committed) config.
"""

from __future__ import annotations

from pathlib import Path

ENGINE_BIN_REL = Path("game_engine") / "game_stub.py"

# Real custom-engine binary. None means "fall back to stub loop".
# Set this to /path/to/your/engine.exe (or .sh, .bat on Windows + Linux
# alike) to wire a real custom engine in production.
ENGINE_BIN: str | None = None

# Optional argv prefix to inject BEFORE --server --port --token --username.
# Use this when your engine expects its own flags before ours. Example:
#   ENGINE_ARGV_PREFIX = ("--join", "--mode=ranked")
ENGINE_ARGV_PREFIX: tuple = ()

# Canonical argv contract the engine MUST accept.
EXPECTED_ARGV = ("--server", "--port", "--token", "--username")

PROTOCOL_VERSION = 1  # bump if argv contract changes
