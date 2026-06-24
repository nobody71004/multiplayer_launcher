"""Multi-client integration test for the matchmaker + launcher.

Boots the matchmaker as a real subprocess on a free port, registers
two users via the launcher's ``MatchClient``, fires heartbeats from
two in-process stubs (each in its own thread), and asserts that
``MatchClient.list_servers()`` returns both servers with the
expected ``players`` counts and ordering.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # noqa: E402

import launcher_core  # noqa: E402
MatchClient = launcher_core.MatchClient


def _free_port() -> int:
    """Bind an ephemeral socket to port 0 and return the assigned port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_health(base_url: str, timeout: float = 10.0) -> None:
    """Poll /api/health until 200 ok=true or timeout."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = requests.get(f"{base_url}/api/health", timeout=0.5)
            if r.status_code == 200 and r.json().get("ok") is True:
                return
            last_err = AssertionError(f"status={r.status_code}")
        except requests.exceptions.RequestException as e:
            last_err = e
        time.sleep(0.1)
    raise RuntimeError(
        f"matchmaker at {base_url} never became healthy within {timeout}s: {last_err!r}"
    )


@pytest.fixture(scope="module")
def matchmaker_subprocess():
    """Boot the matchmaker on a free port as a real subprocess for the
    duration of this test module.  Uses InMemoryStorage (set explicitly
    in the subprocess env) so the test stays hermetic and port-free.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    repo_root = ROOT
    server_script = repo_root / "matchmaking_server.py"
    if not server_script.exists():
        pytest.skip(f"matchmaking_server.py not found at {server_script}")

    env = {
        **os.environ,
        "MATCHMAKER_USE_INMEMORY": "1",
        "PYTHONUNBUFFERED": "1",
    }

    proc = subprocess.Popen(
        [
            sys.executable,
            str(server_script),
            "--host", "127.0.0.1",
            "--port", str(port),
        ],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_health(base_url, timeout=10.0)
    except Exception:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            out = ""
            if proc.stdout is not None:
                out = proc.stdout.read().decode("utf-8", errors="replace")
            sys.stderr.write(
                f"\n[matchmaker boot failed; stdout/stderr was]\n{out}\n"
            )
        finally:
            raise

    yield base_url

    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=3)
        except Exception:
            pass


def _heartbeat_loop(
    base_url: str,
    token: str,
    server_id: str,
    players: int,
    stop_event: threading.Event,
    iterations: int = 4,
) -> None:
    """Emit ``iterations`` heartbeats spaced ~0.4 s apart, then stop."""
    for _ in range(iterations):
        if stop_event.is_set():
            return
        try:
            requests.post(
                f"{base_url}/api/heartbeat",
                json={
                    "token": token,
                    "server_id": server_id,
                    "name": f"stub-{server_id}",
                    "host": "127.0.0.1",
                    "port": 7777,
                    "players": players,
                    "max_players": 16,
                },
                timeout=2.0,
            )
        except requests.exceptions.RequestException:
            pass
        time.sleep(0.4)


def test_two_clients_heartbeat_and_launcher_lists_both(
    matchmaker_subprocess: str,
) -> None:
    base_url = matchmaker_subprocess

    # Two launcher-side MatchClient instances (alice + bob).
    alice = MatchClient(base_url=base_url)
    bob = MatchClient(base_url=base_url)
    assert alice.health(), "/api/health should be 200"
    assert bob.health(), "/api/health should be 200"

    # Register and login both users.
    alice.register("alice", "alice-pw")
    bob.register("bob", "bob-pw")
    alice_token = alice.login("alice", "alice-pw")
    bob_token = bob.login("bob", "bob-pw")
    assert alice_token and bob_token
    assert alice_token != bob_token

    # Run two concurrent stub heartbeats: 4 players vs 8 players.
    stop_event = threading.Event()
    t_alice = threading.Thread(
        target=_heartbeat_loop,
        args=(base_url, alice_token, "server-alice", 4, stop_event),
        daemon=True,
    )
    t_bob = threading.Thread(
        target=_heartbeat_loop,
        args=(base_url, bob_token, "server-bob", 8, stop_event),
        daemon=True,
    )
    t_alice.start()
    t_bob.start()

    try:
        # 4 iterations * 0.4 s = 1.6 s of heartbeats, plus a buffer for
        # in-process scheduling and 60-s TTL margin.
        time.sleep(2.0)

        # Verify both servers appear in the launcher's listing.
        servers = alice.list_servers()
        server_ids = [s.get("id") for s in servers]
        assert "server-alice" in server_ids, f"server-alice missing: {server_ids}"
        assert "server-bob" in server_ids, f"server-bob missing: {server_ids}"

        s_alice = next(s for s in servers if s.get("id") == "server-alice")
        s_bob = next(s for s in servers if s.get("id") == "server-bob")
        assert s_alice.get("players") == 4, s_alice
        assert s_bob.get("players") == 8, s_bob

        # The matchmaking server sorts the listing desc by players, so
        # server-bob (8 players) precedes server-alice (4 players) and
        # the caller sees the most populated server first.
        idx_alice = server_ids.index("server-alice")
        idx_bob = server_ids.index("server-bob")
        assert idx_bob < idx_alice, (
            f"servers must be ordered desc by player count (got {server_ids})"
        )

        # Soft-connect sanity: the launcher's listing carries the same
        # human-readable ``name`` the heartbeat stub registered, so the
        # launcher can show "stub-server-bob" etc. in the GUI.
        assert s_alice.get("name", "").startswith("stub-"), s_alice
        assert s_bob.get("name", "").startswith("stub-"), s_bob
    finally:
        stop_event.set()
        for t in (t_alice, t_bob):
            t.join(timeout=3)
