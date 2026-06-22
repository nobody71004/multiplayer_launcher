"""Engine shim with two modes.

Real-engine mode (ENGINE_BIN set in engine_contract.py OR
ENGINE_BIN_OVERRIDE env var): the shim REPLACES its own process image
with the real engine binary via `os.execvp`. This means:
  - The real engine occupies the SAME PID as the shim's Python.
  - The launcher's `subprocess.Popen` reference points at the real engine.
  - `launcher._game_proc.terminate()` cleanly forward-kills the engine.
  - The engine sees --server --port --token --username argv verbatim,
    with ENGINE_ARGV_PREFIX inserted BEFORE --server.

Default mode (no ENGINE_BIN configured): the shim runs its built-in
stub loop -- a simulated tick sequence that prints [engine] tick +
[engine] exit. This preserves existing tests that assert on that
stdout, and is what runs in cold-start dev.

Either way: the launcher-supplied contract is validated before the
shim does anything. An empty --token fails loud (exit 2) rather than
silently reaching the engine layer.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# NOTE: `import engine_contract` (NOT `from engine_contract import ...`).
# We want live attribute lookup so test-mode could in principle mutate
# ENGINE_BIN at runtime -- even though in practice tests prefer the
# ENV_VAR_OVERRIDE + subprocess.env path.
import engine_contract


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    """parse_known_args() allows the launcher (or anyone) to inject extra
    flags without breaking the shim -- unrecognized args are dropped
    from the namespace but preserved in the `extras` list and forwarded
    to the real engine verbatim in proxy mode.
    """
    ap = argparse.ArgumentParser(
        description="Game engine shim -- proxies argv to the configured custom-engine binary",
    )
    ap.add_argument("--server", default="127.0.0.1",
                    help="game-server host (string, IPv4 or DNS)")
    ap.add_argument("--port", type=int, default=7777,
                    help="game-server port (integer)")
    ap.add_argument("--token", default="",
                    help="matchmaker-issued auth token (string; non-empty required)")
    ap.add_argument("--username", default="",
                    help="display name in-game (string; may be empty)")
    # Stub-only knobs (forwarded but only used in default mode).
    ap.add_argument("--ticks", type=int, default=4,
                    help="simulated ticks before exit (default mode only)")
    ap.add_argument("--quiet", action="store_true",
                    help="suppress per-tick stdout (default mode only)")
    return ap.parse_known_args(argv)


def _resolve_engine_config() -> tuple[str | None, tuple[str, ...]]:
    """Resolve (engine_bin, engine_prefix) for this invocation.

    Precedence: env var (testing) > engine_contract constant (canonical).
    Both come back; the caller decides whether to proxy or fall back.
    """
    bin_ = os.environ.get("ENGINE_BIN_OVERRIDE") or engine_contract.ENGINE_BIN
    prefix_env = os.environ.get("ENGINE_ARGV_PREFIX_OVERRIDE", "")
    prefix = tuple(prefix_env.split()) if prefix_env else engine_contract.ENGINE_ARGV_PREFIX
    return bin_, prefix


def _run_stub_loop(args: argparse.Namespace, extras: list[str]) -> int:
    """Default mode: run the simulated tick loop. (Backward-compat for tests.)"""
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


def _proxy_to_engine(args: argparse.Namespace, extras: list[str]) -> int:
    """Real-engine mode: replace the shim's Python process with the engine.

    `os.execvp` does NOT return on success -- the calling process image
    is replaced in place, so subsequent lines are unreachable. We only
    fall through to the return on exec failure (e.g. binary not found).
    """
    bin_, prefix = _resolve_engine_config()
    if bin_ is None:
        # Defensive -- main() guards against this. Should be unreachable.
        return _run_stub_loop(args, extras)
    cmd = [
        bin_, *prefix,
        "--server", args.server,
        "--port", str(args.port),
        "--token", args.token,
        "--username", args.username,
        *extras,
    ]
    # Make sure the diagnostic line is flushed BEFORE execvp hijacks the process.
    print(f"[shim] execvp -> {' '.join(cmd)}", flush=True)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as e:
        print(f"[shim] FATAL execvp failed for {bin_!r}: {e}",
              file=sys.stderr, flush=True)
        return 127  # "command not found" convention


def main(argv: list[str] | None = None) -> int:
    args, extras = parse_args(argv)
    # Validate launcher contract here, regardless of mode. A missing token
    # fails loud at the boundary rather than silently at the engine layer.
    if not args.token:
        print("[shim] WARN no token supplied -- refusing to join",
              file=sys.stderr, flush=True)
        return 2
    bin_, _ = _resolve_engine_config()
    if bin_ is None:
        return _run_stub_loop(args, extras)
    return _proxy_to_engine(args, extras)


if __name__ == "__main__":
    sys.exit(main())
