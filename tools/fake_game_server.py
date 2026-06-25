#!/usr/bin/env python3
"""tools/fake_game_server.py — heartbeat stand-in for the missing real engine.

Background::

    engine_contract.ENGINE_BIN = None

This project ships without an actual game-server binary wired in. The
launcher's ``Servers`` Treeview is populated entirely from
``POST /api/heartbeat`` callers on the matchmaker, so to make the
Tkinter UI useful for a local click-around demo you need a long-running
process that's doing the heartbeating on behalf of "the game server".

This script fills that role:

  * Registers + logs in against the matchmaker (gets a token).
  * Binds a UDP socket on ``(--bind-host, --bind-port)`` so the
    launcher-spawned engine (``game_stub.py`` in stub mode, or any
    real engine wired into ``ENGINE_BIN``) has a port to point at.
  * Heartbeats every ``--heartbeat-s`` seconds (default 25s, comfortably
    under the matchmaker's ``SERVER_TTL_SEC=60.0``). Each heartbeat
    posts the configured ``server_id``/``host``/``port``/``players``
    so ``GET /api/servers`` returns this entry until shutdown.
  * Responds to ``SIGINT`` and ``SIGTERM`` cleanly so it works as a
    foreground process in a terminal tab — Ctrl-C shuts it down without
    leaving the matchmaker in a "stale server" state beyond TTL expiry.

Once this is running, ``python multiplayer_launcher.py`` on the same
machine (or anywhere that can reach ``--matchmaker-url``) will see the
demo server entry on Refresh and can click **Launch** to spawn the
engine against it. (``game_stub.py`` in stub mode doesn't actually send
wire bytes, but the UDP port is held so a real engine wired into
``ENGINE_BIN`` will have somewhere to receive.)

Dependencies: stdlib + ``requests`` (already pinned in ``requirements.txt``).
No third-party deps beyond that. No psutil — this process has no need
to enumerate other processes.

Usage::

    # Default: matchmaker on 127.0.0.1:5000, UDP on 127.0.0.1:7777
    python tools/fake_game_server.py

    # Custom matchmaker URL + game port
    python tools/fake_game_server.py \\
        --matchmaker-url http://127.0.0.1:5001 --bind-port 7780

    # Env-var form for docker / systemd wrappers
    MATCHMAKER_URL=http://matchmaker:5000 python tools/fake_game_server.py

Exit codes:
    0  Graceful shutdown (Ctrl-C, SIGTERM, end of stdin).
    1  Matchmaker unreachable after --wait-timeout seconds.
    2  Could not bind the configured UDP game port.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from typing import Optional

import requests


# -----------------------------------------------------------------------
# Defaults — picked to match the launcher's auto-injected game port
# (multiplayer_launcher.py picks whichever row the user selects from
# the Servers treeview; the row's port comes from the heartbeat body).
# -----------------------------------------------------------------------

_USERNAME_DEFAULT = "gamesrv-demo"
_PASSWORD_DEFAULT = "demo-server-pw-9xs82"
_SERVER_ID_DEFAULT = "fake-srv-01"
_SERVER_NAME_DEFAULT = "Demo Setup Server"
_BIND_HOST_DEFAULT = "127.0.0.1"
_BIND_PORT_DEFAULT = 7777
_HEARTBEAT_S_DEFAULT = 25.0
_PLAYERS_DEFAULT = 1
_MAX_PLAYERS_DEFAULT = 16
_MATCHMAKER_URL_DEFAULT = "http://127.0.0.1:5000"
_WAIT_TIMEOUT_DEFAULT = 30.0
_SLEEP_TICK_S = 0.5  # granularity of the heartbeat loop's sleep window


# -----------------------------------------------------------------------
# Matchmaker primitives — kept thin so they fit beside MatchClient
# in launcher_core.py without duplicating its surface area. The launcher
# only consumes /api/health, /api/register, /api/login, /api/servers; a
# *server-as-client* additionally calls /api/heartbeat, which is what
# this script owns.
# -----------------------------------------------------------------------

def _wait_for_health(base_url: str, *, timeout_s: float) -> bool:
    """Poll ``/api/health`` until 200 or ``timeout_s`` elapsed."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{base_url}/api/health", timeout=1.0)
            if r.ok:
                return True
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return False


def _register_then_login(
    base_url: str, username: str, password: str
) -> str:
    """Register (idempotent on the 409-already-exists path) + login.

    Returns the auth token. Surfaces non-409 register failures so the
    caller can render the cause (e.g. password too short).
    """
    reg = requests.post(
        f"{base_url}/api/register",
        json={"username": username, "password": password},
        timeout=5.0,
    )
    if reg.status_code >= 400 and reg.status_code != 409:
        # Raise with the response body so the operator sees the cause;
        # 409 just means "already registered" which is fine on re-run.
        raise requests.HTTPError(
            f"register failed: {reg.status_code} {reg.text}",
            response=reg,
        )
    r = requests.post(
        f"{base_url}/api/login",
        json={"username": username, "password": password},
        timeout=5.0,
    )
    r.raise_for_status()
    return r.json()["token"]


def _heartbeat_once(
    base_url: str, token: str, server_id: str, name: str,
    host: str, port: int, players: int, max_players: int,
) -> None:
    """POST a single heartbeat. Raises requests.RequestException on failure."""
    r = requests.post(
        f"{base_url}/api/heartbeat",
        json={
            "token": token,
            "server_id": server_id,
            "name": name,
            "host": host,
            "port": port,
            "players": players,
            "max_players": max_players,
        },
        timeout=5.0,
    )
    r.raise_for_status()


# -----------------------------------------------------------------------
# Main loop
# -----------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fake_game_server",
        description=(
            "Heartbeat stand-in for a real game server. Registers/logs in "
            "against the matchmaker, binds a UDP socket on the configured "
            "game port, and posts /api/heartbeat every --heartbeat-s so "
            "the launcher's /api/servers list keeps showing this entry "
            "until shutdown."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python tools/fake_game_server.py\n"
            "  python tools/fake_game_server.py --bind-port 7780\n"
            "  MATCHMAKER_URL=http://127.0.0.1:5001 \\\n"
            "      python tools/fake_game_server.py --matchmaker-url http://127.0.0.1:5001\n"
        ),
    )
    parser.add_argument(
        "--matchmaker-url",
        default=os.environ.get(
            "MATCHMAKER_URL", _MATCHMAKER_URL_DEFAULT
        ),
        help=(
            "Matchmaker base URL. "
            "Env: MATCHMAKER_URL. "
            f"Default: {_MATCHMAKER_URL_DEFAULT!r}."
        ),
    )
    parser.add_argument(
        "--username",
        default=_USERNAME_DEFAULT,
        help=f"Server-account username. Default: {_USERNAME_DEFAULT!r}.",
    )
    parser.add_argument(
        "--password",
        default=_PASSWORD_DEFAULT,
        help="Server-account password (must be ≥4 chars per matchmaker policy).",
    )
    parser.add_argument(
        "--server-id",
        default=_SERVER_ID_DEFAULT,
        help=(
            "Stable id emitted in heartbeats. Pick something unique "
            "per machine so two fake servers on the LAN don't collide. "
            f"Default: {_SERVER_ID_DEFAULT!r}."
        ),
    )
    parser.add_argument(
        "--server-name",
        default=_SERVER_NAME_DEFAULT,
        help=(
            "Human-readable name shown in the launcher's Servers treeview. "
            f"Default: {_SERVER_NAME_DEFAULT!r}."
        ),
    )
    parser.add_argument(
        "--bind-host",
        default=_BIND_HOST_DEFAULT,
        help=f"UDP address to bind. Default: {_BIND_HOST_DEFAULT!r}.",
    )
    parser.add_argument(
        "--bind-port",
        type=int,
        default=_BIND_PORT_DEFAULT,
        help=(
            "UDP port to bind. Must match what the launcher points the "
            f"engine at. Default: {_BIND_PORT_DEFAULT}."
        ),
    )
    parser.add_argument(
        "--heartbeat-s",
        type=float,
        default=_HEARTBEAT_S_DEFAULT,
        help=(
            f"Seconds between heartbeats. Default: {_HEARTBEAT_S_DEFAULT}. "
            "Keep well under the matchmaker's SERVER_TTL_SEC=60 so the "
            "Servers list doesn't blank out between beats."
        ),
    )
    parser.add_argument(
        "--players",
        type=int,
        default=_PLAYERS_DEFAULT,
        help=f"Reported player count. Default: {_PLAYERS_DEFAULT}.",
    )
    parser.add_argument(
        "--max-players",
        type=int,
        default=_MAX_PLAYERS_DEFAULT,
        help=f"Reported max-player count. Default: {_MAX_PLAYERS_DEFAULT}.",
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=_WAIT_TIMEOUT_DEFAULT,
        help=(
            "Seconds to wait for the matchmaker to come up before "
            f"giving up. Default: {_WAIT_TIMEOUT_DEFAULT}."
        ),
    )
    args = parser.parse_args(argv)

    # --- 1) wait for matchmaker -----------------------------------------
    print(
        f"[fake-srv] waiting for matchmaker health at {args.matchmaker_url}",
        flush=True,
    )
    if not _wait_for_health(
        args.matchmaker_url, timeout_s=args.wait_timeout
    ):
        print(
            f"[fake-srv] matchmaker at {args.matchmaker_url} did not come "
            f"up within {args.wait_timeout:.0f}s. Is `python "
            f"matchmaking_server.py` running on that URL?",
            file=sys.stderr,
            flush=True,
        )
        return 1

    # --- 2) auth --------------------------------------------------------
    try:
        token = _register_then_login(
            args.matchmaker_url, args.username, args.password
        )
    except requests.RequestException as e:
        print(
            f"[fake-srv] registering/logging in as {args.username!r} failed: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return 1
    print(f"[fake-srv] authed as {args.username!r}", flush=True)

    # --- 3) bind UDP for engine handoff ---------------------------------
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((args.bind_host, args.bind_port))
    except OSError as e:
        print(
            f"[fake-srv] could not bind UDP {args.bind_host}:{args.bind_port}: "
            f"{type(e).__name__}: {e}. "
            f"Is another fake-server / real-engine already on that port?",
            file=sys.stderr,
        )
        if sock is not None:
            sock.close()
        return 2
    print(
        f"[fake-srv] UDP bound on {args.bind_host}:{args.bind_port}; "
        f"id={args.server_id!r} name={args.server_name!r} "
        f"heartbeat-s={args.heartbeat_s}",
        flush=True,
    )

    # --- 4) signal handling ---------------------------------------------
    stop = {"flag": False}

    def _handle_signal(signum, _frame):
        stop["flag"] = True
        print(
            f"[fake-srv] received signal {signum}; shutting down",
            flush=True,
        )
    try:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, AttributeError):
        # SIGTERM is not always installable on Windows for non-Python
        # signals, but Ctrl-C still hits the default KeyboardInterrupt
        # path via the asyncio main loop. Skip gracefully.
        pass

    # --- 5) heartbeat loop ----------------------------------------------
    sent_count = 0
    try:
        while not stop["flag"]:
            try:
                _heartbeat_once(
                    args.matchmaker_url, token,
                    args.server_id, args.server_name,
                    args.bind_host, args.bind_port,
                    args.players, args.max_players,
                )
                sent_count += 1
                print(
                    f"[fake-srv] heartbeat #{sent_count} sent "
                    f"(ts={time.time():.0f})",
                    flush=True,
                )
            except requests.RequestException as e:
                # Matchmaker bounced — log and keep trying. We do NOT
                # want one transient 5xx to take the demo down.
                print(
                    f"[fake-srv] heartbeat #{sent_count + 1} failed: "
                    f"{type(e).__name__}: {e} (will retry next tick)",
                    file=sys.stderr,
                    flush=True,
                )
            # Sleep in short ticks so SIGINT/SIGTERM responds promptly.
            end = time.monotonic() + max(args.heartbeat_s, _SLEEP_TICK_S)
            while not stop["flag"] and time.monotonic() < end:
                time.sleep(min(_SLEEP_TICK_S, end - time.monotonic()))
    except KeyboardInterrupt:
        print("[fake-srv] Ctrl-C; shutting down", flush=True)
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        print(
            f"[fake-srv] closed after {sent_count} heartbeat(s)",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
