"""Python stand-in for the real custom game engine.

Real engine integration: replace this file (or `ENGINE_BIN_REL` in
`engine_contract.py`) with a shim that proxies argv into your binary.

Contract (see game_engine/README.md):
    --server <host>     game-server host (string, IPv4 or DNS)
    --port   <int>      game-server port (integer)
    --token  <jwt>      matchmaker-issued auth token (non-empty required)
    --username <str>    display name in-game (may be empty)
    --ticks  <int>      [stub-only] simulated ticks before exit (default 4)
    --quiet             [stub-only] suppress per-tick output

Exit codes:
    0  clean shutdown
    2  refusal to connect (no token, bad arg)
    1  unexpected error during run
"""

from __future__ import annotations

import argparse
import sys
import time


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Game engine stand-in")
    ap.add_argument("--server", default="127.0.0.1", help="host of the game server")
    ap.add_argument("--port", type=int, default=7777, help="port of the game server")
    ap.add_argument("--token", default="", help="auth token issued by the matchmaker")
    ap.add_argument("--username", default="", help="display name in-game")
    ap.add_argument("--ticks", type=int, default=4, help="simulated ticks (testing)")
    ap.add_argument("--quiet", action="store_true", help="suppress per-tick output")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.token:
        print("[engine] WARN no token supplied — refusing to join", file=sys.stderr)
        return 2
    print(
        f"[engine] connect to {args.server}:{args.port} as '{args.username}' "
        f"(token={len(args.token)} chars)",
        flush=True,
    )
    for i in range(1, args.ticks + 1):
        time.sleep(0.05)
        if not args.quiet:
            print(f"[engine] tick {i}/{args.ticks}", flush=True)
    print("[engine] exit", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
