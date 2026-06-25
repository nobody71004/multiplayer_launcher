#!/usr/bin/env python3
"""tools/probe_greeter.py — black-box 0-RTT greeter probe.

Locates a target process (default ``prelauncher.exe``) via ``psutil``,
enumerates its listening TCP/UDP ports from the OS socket table, and
opens a fresh socket to each one. The probe then *passively* drains
the socket for a configurable window in case the server emits a
banner / handshake / startup frame on connect (the "0-RTT" greeting).
If nothing arrives within that window, a single safe structural
nudge byte is sent so the peer has something to react to — a
length-prefix protocol will reject a 0-length header, a stateful
greeting protocol will emit an error frame, etc.

All received bytes are echoed to stdout in the canonical
``Offset | Hex | ASCII`` hex-dump format (xxd-/``hexdump -C``-style,
literal-pipe column separators) so the exact wire bytes can be
copied verbatim into the protocol documentation schema.

Target use case: ``prelauncher.exe`` is a heavily packed Windows
binary that dynamically maps ``ws2_32.dll`` at runtime, so we cannot
trust static port extraction or assume any particular greeting shape.
This script is a black-box tool for empirical discovery.

Usage examples:

    # Defaults: discover prelauncher.exe, 5s greet, 4-byte zero-header nudge
    python tools/probe_greeter.py

    # Single 0x00 byte nudge, longer greet window
    python tools/probe_greeter.py --greet-timeout 10 --nudge 00

    # Pure passive (no nudge at all)
    python tools/probe_greeter.py --no-nudge

    # Pin to one specific port instead of probing every listener
    python tools/probe_greeter.py --port 54321

    # Override process-name lookup with an explicit PID
    python tools/probe_greeter.py --pid 4242

Exit codes:
    0  Probe completed; targets responded (or silence-with-clean-RST).
    1  Probe ran but no listening sockets matched the filters / refused.
    2  Target process not found (or not accessible) — operator error.

Dependencies: Python 3.9+ and ``psutil`` (no third-party deps beyond that).
On Windows you may need an *elevated* shell to read another process's
socket table — see the AccessDenied warning emitted at runtime.
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import psutil

# Probe-telemetry helpers (hexdump + parse_hex_bytes) live in
# tools/probe_common.py and are shared with tools/greet_server.py so
# the wire traces the two ends of a captured connection emit are
# byte-for-byte aligned.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_common import hexdump, parse_hex_bytes  # noqa: E402


# Defaults — chosen to match the spec's intent: "a configurable timeout
# (e.g., 5 seconds)" and "a 4-byte zero-length header" as the
# structural nudge exemplar.
DEFAULT_PROCESS = "prelauncher.exe"
DEFAULT_GREET_TIMEOUT_S = 5.0
DEFAULT_NUDGE_TIMEOUT_S = 5.0
DEFAULT_CONNECT_TIMEOUT_S = 2.0
DEFAULT_NUDGE_HEX = "00000000"  # 4-byte zero-length header


# Discovered-target tuple: (bind_ip, port, kind)
ListeningSocket = Tuple[str, int, str]


# ---------------------------------------------------------------------------
# Process & socket-table discovery
# ---------------------------------------------------------------------------

def find_process(name: str) -> Optional[psutil.Process]:
    """Return first running process whose name matches ``name`` case-insensitively.

    On Windows, process names preserve case at the API level but are matched
    case-insensitively by the kernel; ``psutil.Process.name()`` returns the
    OS-supplied name. Lower-casing both sides pulls in ``PreLauncher.EXE``,
    ``prelauncher.exe``, etc., without surprises.
    """
    needle = name.lower()
    for proc in psutil.process_iter(attrs=["name"]):
        try:
            pname = proc.info["name"] or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if pname.lower() == needle:
            return proc
    return None


def list_listeners(proc: psutil.Process) -> List[ListeningSocket]:
    """Enumerate ``proc``'s listening TCP/UDP IPv4 + IPv6 sockets.

    Uses ``Process.net_connections(kind="inet")`` rather than scanning the
    whole OS socket table so we only probe sockets OWNED by the target
    process.

    Filtering rules:
      * TCP: ``status == psutil.CONN_LISTEN`` and ``laddr`` set.
      * UDP: ``type == SOCK_DGRAM``, ``laddr`` set, and ``raddr`` empty
        (so connected/sender-pinned UDP sockets don't masquerade as
        listeners).

    On Windows, querying another process's socket table requires
    Administrator privileges; the resulting ``AccessDenied`` is logged
    loudly rather than swallowed — silent failure here would mislead the
    operator into thinking the binary failed to bind ports.
    """
    seen = set()
    listeners: List[ListeningSocket] = []
    try:
        conns = proc.net_connections(kind="inet")
    except psutil.NoSuchProcess:
        return listeners
    except psutil.AccessDenied as e:
        print(
            f"[probe] WARNING: cannot read {proc.name()!r} (pid={proc.pid}) "
            f"socket table — AccessDenied ({e!r}). This typically means "
            f"the probe is not running in an elevated shell on Windows. "
            f"Re-run as Administrator to enumerate listeners.",
            file=sys.stderr,
        )
        return listeners
    except OSError as e:
        print(
            f"[probe] WARNING: net_connections() failed for "
            f"{proc.name()!r} (pid={proc.pid}): {type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return listeners

    for c in conns:
        if not c.laddr:
            continue
        ip, port = c.laddr

        if c.type == socket.SOCK_DGRAM:
            # UDP is connectionless — ``psutil`` reports bound UDP sockets
            # with ``status=NONE`` (not CONN_LISTEN). A UDP socket with
            # no remote (``raddr`` empty / None) is a server-side listener;
            # one with a remote is a connected/peer-pinned send socket.
            if c.raddr:
                continue
            kind = "udp"
        else:
            # TCP: must actually be in LISTEN state.
            if c.status != psutil.CONN_LISTEN:
                continue
            kind = "tcp"

        key = (ip, port, kind)
        if key in seen:
            continue
        seen.add(key)
        listeners.append(key)

    # Stable order so output is reproducible across runs.
    listeners.sort(key=lambda x: (x[2], x[1], x[0]))
    return listeners


# Hex-dump telemetry and argparse helpers are imported from probe_common
# at the top of this file.


# ---------------------------------------------------------------------------
# Wildcard rewrite for probe-target selection
# ---------------------------------------------------------------------------

def _local_probe_addr(ip: str) -> str:
    """Rewrite wildcard binds to a concrete loopback for ``socket.connect``.

    A server can bind ``0.0.0.0``/``::`` to accept from any interface,
    but feeding those back to ``connect()`` is *not* a valid destination:
    Windows returns ``WSAEADDRNOTAVAIL`` (10049) for ``0.0.0.0``, and the
    IPv6 / IPv4-mapped-IPv6 wildcards have their own quirks. Since we
    know prelauncher.exe is local, we just point at loopback.

    Non-wildcard bind addresses are returned untouched — if the binary
    bound a specific LAN IP, we should connect to it as discoverable.
    """
    if ip == "0.0.0.0":
        return "127.0.0.1"
    if ip == "::":
        return "::1"
    low = ip.lower()
    if low.startswith("::ffff:0.0.0.0") or low.startswith("::ffff:0."):
        # IPv4-mapped IPv6 wildcard — downgrade entirely to IPv4 so we
        # don't tangle with dual-stack connect semantics.
        return "127.0.0.1"
    return ip


def _format_endpoint(ip: str, port: int, kind: str) -> str:
    return f"{ip}:{port}/{kind}"


# ---------------------------------------------------------------------------
# Socket drain (bounded by an absolute monotonic deadline)
# ---------------------------------------------------------------------------

def _drain(sock: socket.socket, total_timeout_s: float) -> bytes:
    """Recv whatever's available, bounded by a total absolute deadline.

    Uses ``time.monotonic()`` rather than re-arming ``SO_RCVTIMEO``
    after each recv — otherwise a noisy peer's second arrival would
    silently extend the probe window beyond ``total_timeout_s``.

    Termination conditions:
      * deadline reached (returns whatever bytes were accumulated)
      * ``recv()`` returned ``b""`` → TCP FIN / clean peer half-close;
        break immediately, do not re-arm.
      * ``ConnectionResetError`` / ``ConnectionAbortedError`` /
        ``OSError`` → peer RST / abort; the caller decides what to do
        with the partial bytes (we just return what's accumulated).

    Never raises — drain termination is always treated as
    data-exhaustion so the caller can branch uniformly.
    """
    deadline = time.monotonic() + max(total_timeout_s, 0.001)
    chunks: List[bytes] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            buf = sock.recv(65535)
        except socket.timeout:
            break
        except (ConnectionResetError, ConnectionAbortedError):
            # RST / abort mid-drain: bytes accumulated so far are
            # still valid wire-artefact evidence; bail out so the
            # caller can render a 'peer RST' note alongside the
            # hex-dump rather than waiting for an RST that won't
            # come.
            break
        except OSError:
            # Any other OS-level fault (e.g. shutdown race) — bail
            # cleanly and return whatever we accumulated.
            break
        if not buf:
            # TCP FIN from peer — clean half-close. Stop draining
            # immediately; do not re-arm.
            break
        chunks.append(buf)
    return b"".join(chunks)


def _drain_udp(sock: socket.socket, total_timeout_s: float) -> bytes:
    """Drain a connected UDP socket, same deadline semantics as ``_drain``."""
    deadline = time.monotonic() + max(total_timeout_s, 0.001)
    chunks: List[bytes] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        sock.settimeout(remaining)
        try:
            buf, _ = sock.recvfrom(65535)
        except socket.timeout:
            break
        except OSError:
            break
        if not buf:
            break
        chunks.append(buf)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Probe a single TCP listener
# ---------------------------------------------------------------------------

def probe_tcp(
    target_ip: str,
    target_port: int,
    *,
    connect_timeout_s: float,
    greet_timeout_s: float,
    nudge_bytes: Optional[bytes],
    nudge_timeout_s: float,
) -> Tuple[bytes, str, Optional[str]]:
    """Open a fresh TCP socket, drain the greeter window, optionally nudge.

    Returns ``(bytes_received, tag, note)`` where:
      * ``tag`` ∈ {``greet``, ``silent``, ``silent-nudged``, ``refused``,
        ``connect-failed``, ``send-failed``}
      * ``note`` is a short operator-facing string if a wire-artefact
        event occurred mid-drain (RST, BrokenPipe on nudge write, …) so
        the caller can render it alongside the hex-dump.

    Per spec interpretation: if ANY bytes arrived during the greet
    window, the probe has proven the protocol is "server-speaks-first"
    and we skip nudging — pushing bytes onto a stateful partial-banner
    state machine would muddy baseline documentation. The nudge fires
    only when the greet window truly expires with zero bytes.
    """
    probe_ip = _local_probe_addr(target_ip)
    family = socket.AF_INET6 if ":" in probe_ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM, 0)
    sock.settimeout(connect_timeout_s)
    note: Optional[str] = None
    try:
        if probe_ip != target_ip:
            print(
                f"[probe]   wildcard rewrite: bind {target_ip} → "
                f"connect {probe_ip}",
                file=sys.stderr,
            )
        try:
            sock.connect((probe_ip, target_port))
        except ConnectionRefusedError:
            return b"", "refused", None
        except (socket.timeout, OSError) as e:
            print(
                f"[probe]   connect to {_format_endpoint(target_ip, target_port, 'tcp')} "
                f"failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return b"", "connect-failed", None

        # Greet window — passive drain for any banner/handshake bytes.
        greet = _drain(sock, greet_timeout_s)
        if greet and nudge_bytes:
            # Spec: nudge only when the greet window expires with
            # zero bytes. Don't push bytes onto an already-started
            # server state machine.
            return greet, "greet", None

        if not greet and nudge_bytes:
            # Send the structural nudge, then drain the post-nudge window.
            try:
                sock.sendall(nudge_bytes)
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                note = (
                    f"nudge write failed mid-send: "
                    f"{type(e).__name__}: {e} (peer hung up before "
                    f"nudge fully accepted — treat as wire rejection)"
                )
                return b"", "send-failed", note
            post = _drain(sock, nudge_timeout_s)
            # Detect post-nudge RST vs. genuine silence by ticking
            # once more at non-blocking recv: if recv raises RESET
            # without returning bytes, that's an RST-as-response and
            # we surface it as a wire-artefact note. Order matters
            # here — ConnectionResetError is an OSError subclass, so
            # a bare ``except OSError`` above would silently swallow
            # the RST signal before the more specific clause gets a
            # chance to run.
            try:
                sock.settimeout(0.0)
                tail = sock.recv(65535)
                post += tail
            except (socket.timeout, BlockingIOError):
                # No data, no RST — connection simply went silent.
                pass
            except (ConnectionResetError, ConnectionAbortedError) as e:
                note = (
                    f"peer RST after nudge: {type(e).__name__}: {e} "
                    f"(treat RST as wire rejection — protocol "
                    f"understood transport but hard-rejected framing)"
                )
            return post, ("post-nudge" if post else "silent-nudged"), note

        # No greet bytes and nudge disabled.
        return greet, "silent", None
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


# ---------------------------------------------------------------------------
# Probe a single UDP listener
# ---------------------------------------------------------------------------

def probe_udp(
    target_ip: str,
    target_port: int,
    *,
    greet_timeout_s: float,
    nudge_bytes: Optional[bytes],
    nudge_timeout_s: float,
) -> Tuple[bytes, str, Optional[str]]:
    """Open a connected UDP socket, peek for a datagram, optionally nudge.

    UDP has no "0-RTT" greeting in the TCP sense, but well-behaved
    game servers sometimes emit bootstrapping datagrams. Any datagram
    received within the greet window is treated as a "greeting" for
    documentation purposes; if none arrives, a single-shot nudge
    datagram is sent (length-prefix protocols typically interpret
    ``\\x00\\x00\\x00\\x00`` as "empty payload" and reply with an error
    frame or echo back).
    """
    probe_ip = _local_probe_addr(target_ip)
    family = socket.AF_INET6 if ":" in probe_ip else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM, 0)
    sock.settimeout(greet_timeout_s)
    note: Optional[str] = None
    try:
        if probe_ip != target_ip:
            print(
                f"[probe]   wildcard rewrite: bind {target_ip} → "
                f"connect {probe_ip}",
                file=sys.stderr,
            )
        try:
            sock.connect((probe_ip, target_port))
        except OSError as e:
            print(
                f"[probe]   udp connect to "
                f"{_format_endpoint(target_ip, target_port, 'udp')} "
                f"failed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return b"", "connect-failed", None

        greet = _drain_udp(sock, greet_timeout_s)
        # Per spec: if ANY bytes arrived during the greet window, the
        # protocol is "server-speaks-first" — do not nudge. We surface
        # whatever came in as the documented greeting and stop.
        if greet:
            return greet, "greet", None

        if not nudge_bytes:
            return b"", "silent", None

        try:
            sock.send(nudge_bytes)
        except OSError as e:
            note = f"nudge sendto failed: {type(e).__name__}: {e}"
            return b"", "send-failed", note
        post = _drain_udp(sock, nudge_timeout_s)
        if post:
            return post, "post-nudge", note
        return b"", "silent-nudged", note
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Orchestration / CLI entry point
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Black-box 0-RTT greeter probe. Discovers a process's "
            "listening TCP/UDP ports via psutil, opens a fresh "
            "socket to each, passively drains a greet window, and "
            "optionally sends a structural nudge. All bytes received "
            "are emitted in canonical 'Offset | Hex | ASCII' hex-dump "
            "format suitable for pasting into documentation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python tools/probe_greeter.py\n"
            "  python tools/probe_greeter.py --greet-timeout 8 --nudge 00\n"
            "  python tools/probe_greeter.py --no-nudge\n"
            "  python tools/probe_greeter.py --port 54321\n"
            "  python tools/probe_greeter.py --pid 4242 --skip-udp\n"
        ),
    )
    parser.add_argument(
        "--process",
        default=DEFAULT_PROCESS,
        help=(
            "Process name to look up via psutil (case-insensitive). "
            f"Default: {DEFAULT_PROCESS!r}."
        ),
    )
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help=(
            "Explicit PID to probe. Overrides --process lookup. Useful "
            "when the binary's process image name is randomized."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Probe only the single port matching this number (across "
            "all discovered listeners). Default: probe every listener."
        ),
    )
    parser.add_argument(
        "--greet-timeout",
        type=float,
        default=DEFAULT_GREET_TIMEOUT_S,
        help=(
            "Seconds to passively drain after connect, looking for a "
            f"server-side greeting. Default: {DEFAULT_GREET_TIMEOUT_S}."
        ),
    )
    parser.add_argument(
        "--nudge-timeout",
        type=float,
        default=DEFAULT_NUDGE_TIMEOUT_S,
        help=(
            "Seconds to drain after sending the nudge byte(s). "
            f"Default: {DEFAULT_NUDGE_TIMEOUT_S}."
        ),
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT_S,
        help=(
            "Seconds to allow for the initial socket connect. "
            f"Default: {DEFAULT_CONNECT_TIMEOUT_S}."
        ),
    )
    parser.add_argument(
        "--nudge",
        type=parse_hex_bytes,
        default=DEFAULT_NUDGE_HEX,
        help=(
            "Hex bytes to send as a structural nudge if the greet "
            f"window expires with zero bytes. Default: "
            f"{DEFAULT_NUDGE_HEX!r} (= 4-byte zero-length header). "
            f"Try '00' for a single 0x00 byte, '0d0a' for CRLF, "
            f"etc. Whitespace and '0x'/'\\\\x' prefixes are stripped."
        ),
    )
    parser.add_argument(
        "--no-nudge",
        action="store_true",
        help="Skip the structural nudge entirely (pure passive listening).",
    )
    parser.add_argument(
        "--skip-udp",
        action="store_true",
        help="Skip UDP listeners even if discovered.",
    )
    args = parser.parse_args(argv)

    nudge_bytes: Optional[bytes] = None if args.no_nudge else args.nudge

    proc: Optional[psutil.Process] = None
    if args.pid is not None:
        try:
            proc = psutil.Process(args.pid)
        except psutil.NoSuchProcess:
            print(
                f"[probe] --pid {args.pid} is not a running process.",
                file=sys.stderr,
            )
            return 2
        except psutil.AccessDenied as e:
            print(
                f"[probe] --pid {args.pid} not accessible: {e!r}",
                file=sys.stderr,
            )
            return 2
    else:
        proc = find_process(args.process)
        if proc is None:
            print(
                f"[probe] no running process named {args.process!r} "
                f"found. Is it running? Try --pid to specify explicitly "
                f"or --process with a different name.",
                file=sys.stderr,
            )
            return 2

    listeners = list_listeners(proc)
    if args.skip_udp:
        listeners = [l for l in listeners if l[2] != "udp"]
    if args.port is not None:
        listeners = [l for l in listeners if l[1] == args.port]
    if not listeners:
        print(
            f"[probe] no listening sockets matched (process="
            f"{proc.name()!r}, pid={proc.pid}, port-filter={args.port}, "
            f"skip-udp={args.skip_udp}).",
            file=sys.stderr,
        )
        return 1

    nudge_repr = (
        "off"
        if nudge_bytes is None
        else (f"{nudge_bytes.hex()} ({len(nudge_bytes)} byte"
              f"{'s' if len(nudge_bytes) != 1 else ''})")
    )
    print(
        f"[probe] target={proc.name()!r} pid={proc.pid} "
        f"greet-timeout={args.greet_timeout}s "
        f"nudge-timeout={args.nudge_timeout}s "
        f"connect-timeout={args.connect_timeout}s "
        f"nudge={nudge_repr}"
    )
    print(
        f"[probe] discovered {len(listeners)} listening socket(s):"
    )
    for ip, port, kind in listeners:
        print(f"[probe]   - {_format_endpoint(ip, port, kind)}")

    encountered_refused = False
    for ip, port, kind in listeners:
        endpoint = _format_endpoint(ip, port, kind)
        print(f"[probe] === probe {endpoint} ===")
        if kind == "tcp":
            data, tag, note = probe_tcp(
                ip, port,
                connect_timeout_s=args.connect_timeout,
                greet_timeout_s=args.greet_timeout,
                nudge_bytes=nudge_bytes,
                nudge_timeout_s=args.nudge_timeout,
            )
        else:
            data, tag, note = probe_udp(
                ip, port,
                greet_timeout_s=args.greet_timeout,
                nudge_bytes=nudge_bytes,
                nudge_timeout_s=args.nudge_timeout,
            )

        print(f"[probe] tag={tag} bytes={len(data)}")
        if data:
            for line in hexdump(data).splitlines():
                print(f"[probe]   {line}")
        else:
            print("[probe]   (no bytes received)")
        if note:
            print(f"[probe]   note: {note}")
        if tag in {"refused", "connect-failed"}:
            encountered_refused = True

    print("[probe] done.")
    return 1 if encountered_refused else 0


if __name__ == "__main__":
    sys.exit(main())
