#!/usr/bin/env python3
"""tools/greet_server.py — generic asyncio TCP server with hex-dump telemetry.

The complement of ``tools/probe_greeter.py``: while ``probe_greeter``
opens outgoing sockets to discover what bytes a peer sends on connect,
this script listens for incoming TCP connections and logs the bytes
that clients send *to* it. The captured bytes are emitted in canonical
``Offset | Hex | ASCII`` hex-dump format so wire traces can be pasted
verbatim into protocol documentation side-by-side with the
client-side probe output.

Per-connection lifecycle::

    accept   →   read handshake (idle-bounded)   →   validate (configurable)
                                                          │
                                                          └→  send optional --response payload
                                                          └→  close

Generic handshake validator
----------------------------
``parse_handshake()`` is wired to a *generic* high-performance
protocol spec you can configure from the command line. Each validator
runs independently and ALL configured validators must pass for the
handshake to be accepted:

  * ``--validator-magic <hex>`` — magic-byte prefix the payload must
    begin with (variable length). E.g. ``aa55`` = 2 bytes.
  * ``--validator-prefix-len-bytes <N>`` — the next ``N`` bytes after
    magic form a little-endian uint32 length-prefix; declared length
    must not exceed remaining payload length.
  * ``--validator-version <N>`` — version byte (immediately after
    magic + length-prefix) must equal ``N``. Overrides min/max.
  * ``--validator-version-min/--validator-version-max <N>`` — version
    byte in [min, max]. Mutually exclusive with ``--validator-version``.
  * ``--validator-token-secret <str>`` — substring the payload must
    contain (e.g. an auth token).

If **no** validator flag is supplied, ``parse_handshake`` rejects every
payload — the safe-default. This preserves the prior behavior so an
operator must explicitly opt in to validation.

Use cases:
    * Black-box protocol development: drop this server in front of
      your custom client to observe what it sends, byte-for-byte.
    * Phase-N emulator scaffolds: with ``--validator-*`` flags this
      becomes a high-performance protocol server with magic-byte +
      length-prefix + version-byte verification wired in.
    * Smoke testing: feed it a known byte stream and confirm your
      response bytes round-trip cleanly.

Dependencies: Python 3.9+, stdlib only. No psutil — this server has
no need to enumerate other processes.

Exit codes:
    0  Graceful shutdown (KeyboardInterrupt, EOF on stdin, etc.).
    1  Spec failure (e.g. ``--port`` already in use at startup).

Usage:

    # Pure passive listening (safe default — rejects everything).
    python tools/greet_server.py --port 54321

    # Validator enabled: magic-byte + length-prefix + version-byte.
    python tools/greet_server.py --port 54321 \\
        --validator-magic aa55 \\
        --validator-prefix-len-bytes 4 \\
        --validator-version 2

    # Range version check via min/max instead of pinning.
    python tools/greet_server.py --port 54321 \\
        --validator-version-min 1 --validator-version-max 3

    # Full validator stack + response byte sequence.
    python tools/greet_server.py --port 54321 \\
        --validator-magic aa55 \\
        --validator-prefix-len-bytes 4 \\
        --validator-version 2 \\
        --validator-token-secret 's3cr3t' \\
        --response 4f4b0d0a
"""
from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from pathlib import Path
from typing import Optional, Tuple

# Probe-telemetry helpers (hexdump + parse_hex_bytes) live in
# tools/probe_common.py and are shared with tools/probe_greeter.py so
# the wire traces the two ends of a captured connection emit are
# byte-for-byte aligned.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_common import hexdump, parse_hex_bytes  # noqa: E402


# ---------------------------------------------------------------------------
# Handshake validator — generic high-performance protocol spec
# ---------------------------------------------------------------------------

def _validator_cfg_has_any(cfg: dict) -> bool:
    """Return True if at least one validator option is configured.

    With no validators configured, ``parse_handshake`` falls back to
    the safe-default rejection path so the server is never accidentally
    permissive when an operator forgets the CLI flags.
    """
    return any(
        [
            cfg.get("magic") is not None,
            (cfg.get("prefix_len_bytes") or 0) > 0,
            cfg.get("version") is not None,
            cfg.get("version_min") is not None,
            cfg.get("version_max") is not None,
            cfg.get("token_secret") is not None,
        ]
    )


def _validate_handshake(payload: bytes, cfg: dict) -> Tuple[bool, str]:
    """Walk the configured validators and return ``(ok, reason)``.

    Each validator runs in sequence against a moving cursor into
    ``payload``. The first failure returns ``(False, "<reason>")``
    with an operator-facing diagnostic. All-pass returns
    ``(True, "<stage>")`` so the caller can log which stages ran.
    """
    cursor = 0

    magic = cfg.get("magic")
    if magic is not None:
        n = len(magic)
        if len(payload) < n:
            return False, f"short payload ({len(payload)} < magic {n})"
        if payload[:n] != magic:
            return (
                False,
                f"magic mismatch (got {payload[:n].hex()}, "
                f"want {magic.hex()})",
            )
        cursor += n

    prefix_len_bytes = cfg.get("prefix_len_bytes") or 0
    if prefix_len_bytes > 0:
        if len(payload) - cursor < prefix_len_bytes:
            return (
                False,
                f"short payload for length-prefix "
                f"({len(payload) - cursor} < {prefix_len_bytes})",
            )
        prefix_bytes = payload[cursor : cursor + prefix_len_bytes]
        try:
            declared_len = int.from_bytes(
                prefix_bytes, "little", signed=False
            )
        except (OverflowError, ValueError):
            return False, "length-prefix decode failed"
        cursor += prefix_len_bytes
        remaining = len(payload) - cursor
        if declared_len > remaining:
            return (
                False,
                f"declared length {declared_len} exceeds remaining "
                f"payload ({remaining})",
            )

    version = cfg.get("version")
    version_min = cfg.get("version_min")
    version_max = cfg.get("version_max")
    if (
        version is not None
        or version_min is not None
        or version_max is not None
    ):
        if len(payload) - cursor < 1:
            return False, "no version byte present"
        v = payload[cursor]
        cursor += 1
        if version is not None and v != version:
            return False, f"version mismatch (got {v}, want {version})"
        if version_min is not None and v < version_min:
            return (
                False,
                f"version {v} below min {version_min}",
            )
        if version_max is not None and v > version_max:
            return (
                False,
                f"version {v} above max {version_max}",
            )

    token_secret = cfg.get("token_secret")
    if token_secret is not None:
        if token_secret.encode("utf-8") not in payload:
            return False, "token_secret not found in payload"

    return True, "ok"


def parse_handshake(payload: bytes, *, cfg: Optional[dict] = None) -> bool:
    """Return True if ``payload`` matches the configured handshake spec.

    Default behaviour (no validators configured): REJECT every payload
    with the canonical ``parse_handshake: REJECTED (no validator
    configured)`` log line. Customize by passing a non-empty ``cfg``
    populated with the CLI's ``--validator-*`` flags.

    Wire-format invariant: every rejection path emits a single line
    that contains the literal substring ``parse_handshake: REJECTED``
    so log scrapers (e.g. ``tools/_validator_smoke.py``) can parse
    the parity decision uniformly.
    """
    cfg = cfg or {}
    if not payload:
        print(
            "[server]   parse_handshake: REJECTED (empty payload)",
            file=sys.stderr,
        )
        return False
    if not _validator_cfg_has_any(cfg):
        print(
            "[server]   parse_handshake: REJECTED (no validator "
            "configured). Customize parse_handshake() in "
            "greet_server.py (or pass --validator-* flags on CLI) "
            "to enforce handshake validation. Dropping connection.",
            file=sys.stderr,
        )
        return False
    ok, reason = _validate_handshake(payload, cfg)
    if not ok:
        print(
            f"[server]   parse_handshake: REJECTED ({reason})",
            file=sys.stderr,
        )
        return False
    print(
        f"[server]   parse_handshake: ACCEPTED ({reason})",
        file=sys.stderr,
    )
    return True


# ---------------------------------------------------------------------------
# Per-connection handlers
# ---------------------------------------------------------------------------

async def read_handshake(
    reader: asyncio.StreamReader,
    *,
    max_bytes: int,
    idle_timeout_s: float,
) -> bytes:
    """Read bytes until ``idle_timeout_s`` of silence (handshake-end) OR
    ``max_bytes`` is reached OR the peer half-closes / RSTs.

    The idle-window pattern is the most generic "where does the
    handshake end?" detector: it makes no protocol-specific assumptions
    about delimiters, length-prefix decoding, or frame boundaries.
    Tune ``idle_timeout_s`` upward on slow networks.
    """
    handshake = bytearray()
    while len(handshake) < max_bytes:
        chunk_size = min(4096, max_bytes - len(handshake))
        try:
            chunk = await asyncio.wait_for(
                reader.read(chunk_size),
                timeout=idle_timeout_s,
            )
        except asyncio.TimeoutError:
            break  # idle window elapsed — handshake complete
        if not chunk:
            break  # EOF / TCP FIN — clean half-close
        handshake.extend(chunk)
    return bytes(handshake)


async def send_response(
    writer: asyncio.StreamWriter,
    response: bytes,
    *,
    peer: str,
) -> None:
    """Write ``response`` to ``writer`` and ``await drain()``.

    Intentionally does NOT call ``writer.wait_closed()`` — keeping the
    socket alive after the response byte stream is the caller's choice.
    Many protocols want to write a reply and continue receiving further
    frames on the same connection.
    """
    print(f"[server]   -> peer {peer} ({len(response)} bytes):", file=sys.stderr)
    if response:
        for line in hexdump(response).splitlines():
            print(f"[server]     {line}", file=sys.stderr)
    else:
        print("[server]     (no response bytes configured)", file=sys.stderr)
    writer.write(response)
    try:
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        # Peer RST'd in the middle of our drain — surface as a clean
        # log line rather than letting it tank the per-connection task.
        print(
            f"[server]   drain to peer {peer} failed: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        raise


async def handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    max_handshake_bytes: int,
    handshake_idle_s: float,
    response_bytes: Optional[bytes],
    validator_cfg: Optional[dict] = None,
) -> None:
    """Handle one client connection through the full lifecycle.

    Exceptions raised here never propagate up to ``serve_forever``;
    asyncio isolates per-connection coroutines, and the broad
    ``except OSError`` below catches every TCP wire-level break
    (``ConnectionResetError``, ``BrokenPipeError``, ``ECONNRESET``,
    ``EPIPE``, etc.) so the event loop survives any client misbehaviour.
    """
    peer_info = writer.get_extra_info("peername")
    peer = f"{peer_info[0]}:{peer_info[1]}" if peer_info else "<unknown>"
    print(f"[server] +++ client connected: {peer}", file=sys.stderr)

    try:
        payload = await read_handshake(
            reader,
            max_bytes=max_handshake_bytes,
            idle_timeout_s=handshake_idle_s,
        )
        print(
            f"[server]   received {len(payload)} bytes from {peer}:",
            file=sys.stderr,
        )
        if payload:
            for line in hexdump(payload).splitlines():
                print(f"[server]     {line}", file=sys.stderr)
        else:
            print("[server]     (no bytes received)", file=sys.stderr)

        if not parse_handshake(payload, cfg=validator_cfg):
            print(
                f"[server] -- handshake rejected for {peer}, closing",
                file=sys.stderr,
            )
            return

        if response_bytes is not None:
            await send_response(writer, response_bytes, peer=peer)
            print(
                f"[server] -- response sent to {peer}, leaving "
                f"connection open for further traffic",
                file=sys.stderr,
            )
        else:
            print(
                f"[server] -- no --response configured for {peer}, "
                f"closing cleanly",
                file=sys.stderr,
            )

    except OSError as e:
        # Wire-level break: peer RST, broken pipe, half-close race, etc.
        # We catch the broad OSError umbrella so a single ornery client
        # can't disrupt siblings on the same event loop.
        print(
            f"[server]   peer {peer} wire break: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
    except asyncio.CancelledError:
        # Server is shutting down — propagate so the loop can finish
        # cancelling pending tasks cleanly.
        print(f"[server]   handle cancelled for {peer}", file=sys.stderr)
        raise
    except Exception as e:
        # Last-resort catch-all so unexpected protocol errors don't
        # propagate up and seed an event-loop crash loop.
        print(
            f"[server]   unexpected error handling {peer}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass
        print(f"[server] --- connection closed: {peer}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Server orchestration
# ---------------------------------------------------------------------------

def _build_validator_cfg(args: argparse.Namespace) -> dict:
    """Assemble the validator config dict from CLI flags.

    ``--validator-version`` (exact pin) takes precedence over the
    ``--validator-version-min/--validator-version-max`` range flags —
    pin a version with ``--validator-version N`` and the range flags
    are silently ignored.
    """
    return {
        "magic": args.validator_magic,
        "prefix_len_bytes": args.validator_prefix_len_bytes,
        "version": args.validator_version,
        "version_min": (
            args.validator_version_min
            if args.validator_version is None
            else None
        ),
        "version_max": (
            args.validator_version_max
            if args.validator_version is None
            else None
        ),
        "token_secret": args.validator_token_secret,
    }


async def run_server(args: argparse.Namespace) -> int:
    """Build and run the asyncio TCP server until cancelled."""
    validator_cfg = _build_validator_cfg(args)

    server = await asyncio.start_server(
        lambda r, w: handle_client(
            r,
            w,
            max_handshake_bytes=args.max_handshake_bytes,
            handshake_idle_s=args.handshake_idle,
            response_bytes=args.response_bytes,
            validator_cfg=validator_cfg,
        ),
        host=args.bind,
        port=args.port,
        reuse_address=True,
    )

    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    response_repr = (
        "<none>"
        if args.response_bytes is None
        else f"{args.response_bytes.hex()} ({len(args.response_bytes)} byte"
             f"{'s' if len(args.response_bytes) != 1 else ''})"
    )
    validator_repr = (
        "<none (safe-default — rejects every payload)>"
        if not _validator_cfg_has_any(validator_cfg)
        else ", ".join(
            f"{k}={v.hex() if isinstance(v, bytes) else v!r}"
            for k, v in validator_cfg.items()
            if ((v is not None) and (v != 0) and (v != b""))
        )
    )
    print(
        f"[server] listening on {addrs} "
        f"(max-handshake-bytes={args.max_handshake_bytes}, "
        f"handshake-idle={args.handshake_idle}s, "
        f"response={response_repr})",
        file=sys.stderr,
    )
    print(f"[server] validator: {validator_repr}", file=sys.stderr)

    async with server:
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            print("[server] await serve_forever cancelled", file=sys.stderr)
            raise
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="greet_server",
        description=(
            "Generic asyncio TCP server with canonical 'Offset | Hex | "
            "ASCII' hex-dump telemetry on every read and write. "
            "Configure --validator-* flags to enforce a magic-byte / "
            "length-prefix / version-byte / token-substring protocol "
            "spec on incoming handshakes. Without any validator "
            "flags, the server still rejects every payload (safe "
            "default)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python tools/greet_server.py --port 54321\n"
            "  python tools/greet_server.py --port 54321 --response 4f4b0d0a\n"
            "  python tools/greet_server.py --port 54321 \\\n"
            "      --validator-magic aa55 \\\n"
            "      --validator-prefix-len-bytes 4 \\\n"
            "      --validator-version 2\n"
            "  python tools/greet_server.py --port 54321 \\\n"
            "      --validator-version-min 1 --validator-version-max 3 \\\n"
            "      --validator-token-secret 's3cr3t'\n"
        ),
    )
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help=(
            "Interface to bind. Default: 127.0.0.1 (loopback only). "
            "Use 0.0.0.0 to listen on all interfaces."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="TCP port to listen on (required).",
    )
    parser.add_argument(
        "--max-handshake-bytes",
        type=int,
        default=4096,
        help=(
            "Cap on handshake payload size. Once this many bytes have "
            "been read, the handshake is forcibly closed. Default: 4096."
        ),
    )
    parser.add_argument(
        "--handshake-idle",
        type=float,
        default=0.5,
        help=(
            "Seconds of peer silence that demarcates end-of-handshake. "
            "Tune upward for slow networks. Default: 0.5."
        ),
    )
    parser.add_argument(
        "--response",
        type=parse_hex_bytes,
        default=None,
        help=(
            "Optional hex bytes to send back after a successful handshake. "
            "Default: none (validate-only mode). Try '4f4b0d0a' for 'OK\\r\\n'."
        ),
    )
    parser.add_argument(
        "--validator-magic",
        type=parse_hex_bytes,
        default=None,
        help=(
            "Magic-byte prefix the handshake payload must begin with. "
            "Hex string of variable length. E.g. 'aa55' = bytes 0xaa 0x55."
        ),
    )
    parser.add_argument(
        "--validator-prefix-len-bytes",
        type=int,
        default=0,
        help=(
            "Bytes immediately after --validator-magic that form a "
            "little-endian uint32 length-prefix. Default: 0 (no length "
            "check). E.g. 4 yields a 4-byte LE uint32 prefix."
        ),
    )
    parser.add_argument(
        "--validator-version",
        type=int,
        default=None,
        help=(
            "Exact version byte required (the byte immediately after "
            "magic + length-prefix). When set, this OVERRIDES "
            "--validator-version-min / --validator-version-max."
        ),
    )
    parser.add_argument(
        "--validator-version-min",
        type=int,
        default=None,
        help=(
            "Inclusive minimum for the version byte. Only consulted if "
            "--validator-version is NOT set."
        ),
    )
    parser.add_argument(
        "--validator-version-max",
        type=int,
        default=None,
        help=(
            "Inclusive maximum for the version byte. Only consulted if "
            "--validator-version is NOT set."
        ),
    )
    parser.add_argument(
        "--validator-token-secret",
        type=str,
        default=None,
        help=(
            "Substring (UTF-8) that the handshake payload must contain. "
            "Used to anchor the validator on a known token between "
            "client and server."
        ),
    )
    args = parser.parse_args(argv)
    args.response_bytes = args.response

    try:
        return asyncio.run(run_server(args))
    except KeyboardInterrupt:
        # asyncio.run() handles the cancellation fan-out itself; we just
        # swallow the noisy traceback here.
        print("\n[server] interrupted — shutting down", file=sys.stderr)
        return 0
    except OSError as e:
        print(f"[server] startup failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
