#!/usr/bin/env python3
"""tools/fake_prelauncher.py — synthetic TCP client for live-capture testing.

A drop-in stand-in for ``prelauncher.exe`` when you want to verify your
``tools/greet_server.py`` pipeline is wired up correctly *before*
pointing the real binary at it. Same socket shape: opens a fresh TCP
connection, sends whatever bytes you tell it to, optionally closes via
clean FIN / abortive RST / passive idle hold.

Why this exists: when you're reverse-engineering a client whose
``Connect()`` triggers a non-trivial handshake, you don't want to waste
a live capture session on a misconfigured server. Drive ``fake_prelauncher``
against ``greet_server`` first with known bytes, confirm the hex-dump
flow is end-to-end correct, then swap in the real ``prelauncher.exe``
and the server output will be byte-aligned with what you already
established the expected format to be.

Run::

    python tools/fake_prelauncher.py --port 54321 \\
        --greet-bytes 'aabb001202000048454c4c4f'

    python tools/fake_prelauncher.py --port 54321 \\
        --greet-bytes 'aabb00000001' \\
        --post-greet-bytes 'ffff' \\
        --delay-ms 100 --close-mode idle --idle-s 3

    python tools/fake_prelauncher.py --port 54321 \\
        --greet-bytes '00' --close-mode rst

Exit codes:
    0  Bytes written, connection closed (FIN/RST/idle) cleanly.
    1  Could not connect to server.
    2  Server reset the connection mid-send.

Dependencies: stdlib + the shared ``tools/probe_common`` hex-dump
helper for self-telemetry. No third-party deps.
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from pathlib import Path

# Reuse the canonical hex-dump helper so the bytes we send look
# identical to the bytes we receive on the server side when both ends
# of a captured trace are pasted into the documentation schema.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_common import hexdump, parse_hex_bytes  # noqa: E402


def _emit_sent(label: str, data: bytes) -> None:
    """Log a sent byte sequence in the canonical hex-dump format."""
    print(f"[fake]   wrote {len(data)} bytes ({label}):", file=sys.stderr)
    if data:
        for line in hexdump(data).splitlines():
            print(f"[fake]     {line}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fake_prelauncher",
        description=(
            "Synthetic TCP client that mimics prelauncher.exe's "
            "connect-and-send shape. Configure --greet-bytes and "
            "--post-greet-bytes to drive arbitrary wire traces against "
            "tools/greet_server.py."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python tools/fake_prelauncher.py --port 54321 \\\n"
            "      --greet-bytes 'aabb00000001' --close-mode fin\n"
            "  python tools/fake_prelauncher.py --port 54321 \\\n"
            "      --greet-bytes 'aabb00000001' --post-greet-bytes 'ffff' \\\n"
            "      --delay-ms 100 --close-mode idle --idle-s 3\n"
            "  python tools/fake_prelauncher.py --port 54321 \\\n"
            "      --greet-bytes '00' --close-mode rst\n"
        ),
    )
    parser.add_argument(
        "--server",
        default="127.0.0.1",
        help="Server bind address. Default: 127.0.0.1 (loopback only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        required=True,
        help="Server port to connect to (required).",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=2.0,
        help="Seconds to allow for the initial socket connect. Default: 2.0.",
    )
    parser.add_argument(
        "--greet-bytes",
        type=parse_hex_bytes,
        default=None,
        help=(
            "Hex bytes to send immediately on connect (the 'greeting' "
            "frame). Default: send nothing — connect-and-idle, useful "
            "for verifying server-side idle-timeout handshake detection "
            "without producing wire noise."
        ),
    )
    parser.add_argument(
        "--post-greet-bytes",
        type=parse_hex_bytes,
        default=None,
        help=(
            "Optional second frame sent after --delay-ms. Useful for "
            "exercising multi-frame handshake captures."
        ),
    )
    parser.add_argument(
        "--delay-ms",
        type=int,
        default=200,
        help=(
            "Milliseconds between --greet-bytes and --post-greet-bytes. "
            "Default: 200."
        ),
    )
    parser.add_argument(
        "--close-mode",
        choices=("fin", "rst", "idle"),
        default="fin",
        help=(
            "How to terminate the connection. "
            "'fin' = clean shutdown(2)+close(). "
            "'rst' = SO_LINGER(0, 0) + close() so the OS sends an RST "
            "instead of a FIN. "
            "'idle' = hold open for --idle-s seconds then close cleanly. "
            "Default: fin."
        ),
    )
    parser.add_argument(
        "--idle-s",
        type=float,
        default=5.0,
        help="When --close-mode idle, seconds to hold the socket open. Default: 5.0.",
    )
    args = parser.parse_args(argv)

    try:
        sock = socket.create_connection(
            (args.server, args.port),
            timeout=args.connect_timeout,
        )
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        print(
            f"[fake] could not connect to {args.server}:{args.port}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1

    print(
        f"[fake] connected to {args.server}:{args.port}",
        file=sys.stderr,
    )

    try:
        if args.greet_bytes:
            sock.sendall(args.greet_bytes)
            _emit_sent("greet", args.greet_bytes)

            if args.post_greet_bytes:
                time.sleep(max(args.delay_ms, 0) / 1000.0)
                sock.sendall(args.post_greet_bytes)
                _emit_sent("post-greet", args.post_greet_bytes)

        if args.close_mode == "fin":
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            print("[fake] closed via FIN", file=sys.stderr)
        elif args.close_mode == "rst":
            # SO_LINGER on/off + linger_time = 0 → kernel sends RST on close.
            # Note: Windows' Winsock struct linger is {u_short onoff, u_short linger}
            # (4-byte "HH"); POSIX uses {int onoff, int linger} (8-byte "ii").
            # CPython's setsockopt() rejects the wrong-size buffer on Windows,
            # so the pack format must be platform-aware.
            linger_fmt = "HH" if sys.platform == "win32" else "ii"
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_LINGER,
                struct.pack(linger_fmt, 1, 0),
            )
            sock.close()
            print("[fake] closed via RST", file=sys.stderr)
        else:  # idle
            print(
                f"[fake] idle-holding for {args.idle_s}s then closing cleanly",
                file=sys.stderr,
            )
            time.sleep(max(args.idle_s, 0.0))
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
            print("[fake] closed after idle window", file=sys.stderr)

        return 0
    except (ConnectionResetError, BrokenPipeError, OSError) as e:
        # The server can RST us mid-send — log, don't crash.
        print(
            f"[fake] wire break: {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        try:
            sock.close()
        except OSError:
            pass
        return 2


if __name__ == "__main__":
    sys.exit(main())
