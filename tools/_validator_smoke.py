"""tools/_validator_smoke.py — validator end-to-end harness for greet_server.py.

Boots greet_server.py as a subprocess with a chosen set of
``--validator-*`` flags, drives a known byte payload via raw socket,
then asserts the server-side stderr contains the expected
``parse_handshake: ACCEPTED`` or ``parse_handshake: REJECTED``
(parity) line.

This harness is reused by both:
  * ``tests/test_greet_server_validator.py`` (pytest import)
  * ``.github/workflows/ci.yml`` ``tools-smoke`` job (CLI exit code)

Each scenario is independent: a fresh port, a fresh server, fresh
teardown. Failures are reported per-scenario so a CI run tells the
operator exactly which validator combination regressed.

Scenario shape::

    {
        "name": "<human label>",
        "cli": [<str>...],          # extra args beyond --port
        "payload_hex": "<ascii hex of bytes>",
        "expect": "ACCEPTED" | "REJECTED",
        "expect_substring": "<opt: required substring of the reason field>",
        "handshake_idle": 0.4,      # opt override (default 0.4s)
    }

Exit code: 0 if all scenarios pass, 1 if any fails.
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

# ---------------------------------------------------------------------------
# Default scenario catalogue
# ---------------------------------------------------------------------------
#
# Each scenario is a self-contained assertion that a specific
# CLI-flag combination drives the validator through a specific
# accept/reject decision. The catalogue is the contract this file
# exports; tests and CI both consume it identically.

DEFAULT_SCENARIOS: List[dict] = [
    # ---- safe default (no validator flags): reject everything ---------
    {
        "name": "no-validator-rejects-everything",
        "cli": [],
        "payload_hex": "aa55",
        "expect": "REJECTED",
    },
    # ---- magic-only: accept when prefix matches, reject otherwise -----
    {
        "name": "magic-only-accepts-match",
        "cli": ["--validator-magic", "aa55"],
        "payload_hex": "aa55ff01",
        "expect": "ACCEPTED",
    },
    {
        "name": "magic-only-rejects-mismatch",
        "cli": ["--validator-magic", "aa55"],
        "payload_hex": "bb55ff01",
        "expect": "REJECTED",
    },
    # ---- magic + exact version ---------------------------------------
    {
        "name": "magic-and-version-accepts-match",
        "cli": [
            "--validator-magic", "aa55",
            "--validator-version", "2",
        ],
        "payload_hex": "aa5502",         # magic + version 2
        "expect": "ACCEPTED",
    },
    {
        "name": "magic-and-version-rejects-wrong-version",
        "cli": [
            "--validator-magic", "aa55",
            "--validator-version", "2",
        ],
        "payload_hex": "aa5503",         # magic + version 3 (mismatch)
        "expect": "REJECTED",
    },
    # ---- magic + version-range -------------------------------------
    {
        "name": "magic-and-version-range-accepts-mid",
        "cli": [
            "--validator-magic", "aa55",
            "--validator-version-min", "1",
            "--validator-version-max", "3",
        ],
        "payload_hex": "aa5502",
        "expect": "ACCEPTED",
    },
    {
        "name": "magic-and-version-range-rejects-above-max",
        "cli": [
            "--validator-magic", "aa55",
            "--validator-version-min", "1",
            "--validator-version-max", "3",
        ],
        "payload_hex": "aa5504",
        "expect": "REJECTED",
    },
    # ---- magic + length-prefix + version --------------------------
    # payload: aa55 + 4-byte LE uint32 (declared_len) + version byte
    # declared_len=1 means 1 byte follows version (1 hex byte)
    {
        "name": "magic-prefix-version-accepts",
        "cli": [
            "--validator-magic", "aa55",
            "--validator-prefix-len-bytes", "4",
            "--validator-version", "7",
        ],
        "payload_hex": "aa55" + "01000000" + "07" + "ee",
        "expect": "ACCEPTED",
    },
    {
        "name": "magic-prefix-version-rejects-declared-len-too-big",
        "cli": [
            "--validator-magic", "aa55",
            "--validator-prefix-len-bytes", "4",
            "--validator-version", "7",
        ],
        # declared_len=999 way exceeds 1 remaining byte after version
        "payload_hex": "aa55" + "e7030000" + "07",
        "expect": "REJECTED",
    },
    # ---- token-secret match -------------------------------------
    {
        "name": "token-secret-accepts-substring-present",
        "cli": [
            "--validator-token-secret", "s3cr3t",
        ],
        "payload_hex": "deadbeef" + "733363723374" + "00",
        "expect": "ACCEPTED",
    },
    {
        "name": "token-secret-rejects-substring-absent",
        "cli": [
            "--validator-token-secret", "s3cr3t",
        ],
        "payload_hex": "deadbeefcafebabe",
        "expect": "REJECTED",
    },
]


# ---------------------------------------------------------------------------
# Single-scenario driver
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind to port 0 ephemeral; release and return the OS-chosen port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _bytes_from_hex(s: str) -> bytes:
    s = s.replace("0x", "").replace(" ", "").replace("_", "")
    if len(s) % 2:
        raise ValueError(f"hex string has odd length: {s!r}")
    return bytes.fromhex(s)


def _check_listening_on(proc: subprocess.Popen, deadline_s: float = 5.0) -> bool:
    """Wait for the server-side ``[server] listening on`` banner in stdout.

    We deliberately do NOT TCP-probe the port here. A TCP probe would
    open a stray connection that the server's accept loop processes
    as a real client -- its per-connection coroutine then runs to
    completion (empty payload -> REJECTED (empty payload)) and emits
    a parity line BEFORE the real scenario's parity line lands in the
    captured buffer. That race made ACCEPT-scenarios fail: the
    harness's parity grep breaks on the FIRST parity line it sees,
    and the probe's line was always first.

    Reading the banner line from proc.stdout is a clean readiness
    signal -- no stray connection, no race, single-source-of-truth
    on "is the server actually listening yet?".
    """
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if proc.poll() is not None:
            return False
        line = proc.stdout.readline()  # type: ignore[union-attr]
        # readline() can block past the deadline if the server
        # silently hangs at boot without ever emitting a byte --
        # re-check the deadline AFTER the read so we exit even when
        # the OS pipe read itself is the blocking primitive.
        if time.monotonic() >= end:
            return False
        if not line:
            time.sleep(0.02)
            continue
        if "[server] listening on" in line:
            return True
    return False


def run_scenario(scenario: dict, *, verbose: bool = False) -> tuple[bool, str]:
    """Run a single scenario; return (passed, message)."""
    name = scenario["name"]
    extra_cli = list(scenario.get("cli", []))
    payload = _bytes_from_hex(scenario["payload_hex"])
    expect = scenario["expect"].upper()
    expect_sub = scenario.get("expect_substring")
    idle = float(scenario.get("handshake_idle", 0.4))

    port = _free_port()

    cmd = [
        sys.executable,
        str(TOOLS / "greet_server.py"),
        "--port", str(port),
        "--handshake-idle", str(idle),
        *extra_cli,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    try:
        if not _check_listening_on(proc):
            return False, f"[{name}] server never logged 'listening on' banner"
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                s.sendall(payload)
                # Half-close so the server-side read_unblock returns EOF.
                try:
                    s.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                # Read whatever the server wants to send so the local
                # kernel buffer drains (the connection can half-close
                # without us reading).
                try:
                    s.settimeout(0.5)
                    while True:
                        chunk = s.recv(4096)
                        if not chunk:
                            break
                except (socket.timeout, OSError):
                    pass
        except OSError as e:
            return False, f"[{name}] client socket I/O failed: {e}"

        # Give the server up to 2s to flush its parity log line.
        deadline = time.monotonic() + 2.0
        captured = ""
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                line = proc.stdout.readline()  # type: ignore[union-attr]
            except (ValueError, OSError):
                break
            if not line:
                time.sleep(0.05)
                continue
            captured += line
            if "parse_handshake" in line and (
                "ACCEPTED" in line or "REJECTED" in line
            ):
                break  # caught the parity line

        parity = f"parse_handshake: {expect}"
        if parity not in captured:
            return False, (
                f"[{name}] expected {parity!r} in server stderr, "
                f"captured=\n{captured!r}"
            )
        if expect_sub and expect_sub not in captured:
            return False, (
                f"[{name}] expected substring {expect_sub!r} in server "
                f"stderr, captured=\n{captured!r}"
            )
        if verbose:
            print(f"[{name}] PASS -- {expect}", flush=True)
        return True, f"[{name}] PASS"
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass


def run_all(
    scenarios: Optional[Iterable[dict]] = None,
    *,
    verbose: bool = False,
) -> int:
    scs = list(scenarios) if scenarios is not None else DEFAULT_SCENARIOS
    if not scs:
        print("[validator-smoke] no scenarios to run", flush=True)
        return 0
    print(
        f"[validator-smoke] running {len(scs)} scenarios "
        f"({sum(1 for s in scs if s['expect'] == 'ACCEPTED')} ACCEPTED, "
        f"{sum(1 for s in scs if s['expect'] == 'REJECTED')} REJECTED)",
        flush=True,
    )
    fails: List[str] = []
    for s in scs:
        ok, msg = run_scenario(s, verbose=verbose)
        print(f"[validator-smoke] {msg}", flush=True)
        if not ok:
            fails.append(msg)
    if fails:
        print(
            f"[validator-smoke] FAILED ({len(fails)}/{len(scs)} failed)",
            flush=True,
        )
        return 1
    print(f"[validator-smoke] all {len(scs)} scenarios PASS", flush=True)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="validator-smoke",
        description=(
            "End-to-end harness for greet_server.py's --validator-* "
            "flags. Boots a fresh server per scenario, drives bytes "
            "via raw socket, asserts the parity line in stderr."
        ),
    )
    ap.add_argument(
        "-v", "--verbose", action="store_true",
        help="echo every scenario's PASS line.",
    )
    args = ap.parse_args(argv)
    return run_all(verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
