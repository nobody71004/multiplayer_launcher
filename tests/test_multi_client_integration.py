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


def _stress_register(client: MatchClient, username: str, password: str) -> str:
    """Register + login helper that tolerates a pre-existing username.

    The matchmaker subprocess is module-scoped, so its InMemoryStorage
    persists across tests in the same module.  Wrapping the register
    call in a try/except keeps the stress variant idempotent if the
    module is ever executed twice in-process (e.g. when a developer
    iterates with pytest --repeat-scope=module).
    """
    try:
        client.register(username, password)
    except ValueError:
        pass  # 409 user-exists -> already registered; that's fine
    return client.login(username, password)


@pytest.mark.integration
def test_many_clients_heartbeat(matchmaker_subprocess: str, record_property) -> None:
    """Stress variant + perf-budget guard against future regressions.

    Spins up ``n_stubs`` (16, sitting inside the 10..20 range the
    user asked for) concurrent in-process heartbeat stubs into the
    same subprocess the ``matchmaker_subprocess`` fixture already
    booted.  After the stubs have settled, asserts that **all**
    stubs are present in ``MatchClient.list_servers()`` (well
    within the 60 s TTL the server enforces), each stub's recorded
    ``players`` matches its last payload, and that the
    heartbeat-acceptance p95 latency plus the ``list_servers``
    read latency stay under explicitly loose ceilings so a slow
    CI box still passes while a real regression -- e.g.
    accidentally serializing every heartbeat behind a global lock
    or re-fetching the whole DB on every list_servers call --
    trips the guard.

    The perf observation is emitted as both a pytest
    ``record_property`` (which lands in the JUnit XML report that
    most CIs scrape, so it shows up in the test artifact
    without depending on pytest's stderr capture mode) AND
    as a ``[perf-budget-guard]`` line on ``sys.stderr`` --
    a human-readable backup for local / dev logs -- so
    consumers can grep either surface to detect drift over time.
    """
    base_url = matchmaker_subprocess

    n_stubs = 16
    sweep_delay_seconds = 0.25
    settle_seconds = 2.5
    heartbeat_p95_budget_ms = 250.0
    list_servers_budget_ms = 250.0

    # Read-only MatchClient for the listing check; threads each
    # use their own MatchClient so we don't share a ConnectionPool
    # across 16 concurrent stubs.
    observer = MatchClient(base_url=base_url)
    assert observer.health(), "/api/health should be 200"

    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    heartbeat_latencies_ms: list[float] = []
    latencies_lock = threading.Lock()

    def _stub(idx: int) -> None:
        username = f"stress-user-{idx:02d}"
        password = f"stress-pw-{idx:02d}"
        server_id = f"stress-server-{idx:02d}"
        # 1..4, distinct counts so listing order is verifiable
        players = (idx % 4) + 1

        client = MatchClient(base_url=base_url)
        token = _stress_register(client, username, password)
        assert token, f"login failed for {username}"

        deadline = time.monotonic() + settle_seconds
        while time.monotonic() < deadline and not stop_event.is_set():
            t0 = time.monotonic()
            try:
                r = requests.post(
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
                latency_ms = (time.monotonic() - t0) * 1000.0
                if r.status_code == 200:
                    with latencies_lock:
                        heartbeat_latencies_ms.append(latency_ms)
            except requests.exceptions.RequestException:
                pass
            time.sleep(sweep_delay_seconds)

    for i in range(n_stubs):
        t = threading.Thread(target=_stub, args=(i,), daemon=True)
        threads.append(t)
        t.start()

    try:
        # Give every stub a moment to land its first heartbeat so the
        # read-side measurement catches a steady-state mix rather than
        # a cold-start spike.
        time.sleep(0.4)

        # Authoritative read-side measurement: list_servers latency
        # with the full n_stubs corpus sitting in the in-memory backend.
        t0 = time.monotonic()
        servers = observer.list_servers()
        list_servers_latency_ms = (time.monotonic() - t0) * 1000.0

        # (1) Correctness: every stress stub is listed within the TTL.
        expected_ids = {f"stress-server-{i:02d}" for i in range(n_stubs)}
        listed_ids = {s.get("id") for s in servers}
        missing = expected_ids - listed_ids
        assert not missing, (
            f"missing stress stubs (TTL=60s should keep all {n_stubs}): "
            f"{sorted(missing)}"
        )

        # (2) Per-stub payload sanity: players matches each stub's
        # last heartbeat.  This catches "threads fired but never
        # landed" as well as "list_servers returned stale entries".
        players_by_id = {s.get("id"): s.get("players") for s in servers}
        for i in range(n_stubs):
            sid = f"stress-server-{i:02d}"
            assert players_by_id.get(sid) == (i % 4) + 1, (
                f"{sid} players={players_by_id.get(sid)!r}, "
                f"expected {(i % 4) + 1}"
            )

        # (3) Compute the heartbeat p95 so we can both assert against
        # the budget below AND emit the observation (next block)
        # regardless of whether the asserts trip.  Hoisting the
        # emission appears BEFORE the asserts so a regression that
        # trips the guard leaves the trip numbers in the JUnit XML
        # artifact, not silently discards its own evidence.
        p95_value: float | None = None
        sorted_latencies: list[float] = []
        if heartbeat_latencies_ms:
            sorted_latencies = sorted(heartbeat_latencies_ms)
            p95_index = max(0, int(round(0.95 * (len(sorted_latencies) - 1))))
            p95_value = sorted_latencies[p95_index]

        # (4) Surface the perf-budget observation FIRST.  When this
        # test fails (a regression tripped a guard), pytest still
        # flushes the JUnit XML + stderr below with the numbers that
        # tripped it.  Keys are snake_case + self-explanatory; the
        # heartbeat_p95_ms becomes "n/a" when no heartbeats
        # accepted, matching the stderr sentinel so both surfaces
        # present the same string to a downstream consumer.
        record_property(
            "perf_budget_guard",
            {
                "n_stubs": n_stubs,
                "heartbeats_accepted": len(heartbeat_latencies_ms),
                "heartbeat_p95_ms": p95_value if p95_value is not None else "n/a",
                "list_servers_ms": list_servers_latency_ms,
                "heartbeat_p95_ms_budget": heartbeat_p95_budget_ms,
                "list_servers_ms_budget": list_servers_budget_ms,
            },
        )
        p95_str = f"{p95_value:.1f}" if p95_value is not None else "n/a"
        sys.stderr.write(
            f"\n[perf-budget-guard] n_stubs={n_stubs} "
            f"heartbeats_accepted={len(heartbeat_latencies_ms)} "
            f"heartbeat_p95_ms={p95_str} "
            f"list_servers_ms={list_servers_latency_ms:.1f} "
            f"(hb_p95_budget_ms={heartbeat_p95_budget_ms:.0f} "
            f"list_budget_ms={list_servers_budget_ms:.0f})\n"
        )

        # (5) Perf-budget assert for heartbeat p95.
        # Genuinely serial per-process code (e.g. an accidental global
        # lock around the storage backend) would push p95 well past
        # 250 ms when 16 concurrent stubs are pounding the endpoint.
        if heartbeat_latencies_ms:
            assert p95_value < heartbeat_p95_budget_ms, (
                f"heartbeat p95 too slow under stress: "
                f"{p95_value:.1f} ms (n={len(sorted_latencies)}, "
                f"budget {heartbeat_p95_budget_ms:.0f} ms)"
            )

        # (6) Perf-budget assert for the read-side listing latency.
        assert list_servers_latency_ms < list_servers_budget_ms, (
            f"list_servers with {n_stubs} live stubs took "
            f"{list_servers_latency_ms:.1f} ms "
            f"(budget {list_servers_budget_ms:.0f} ms)"
        )
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=3)
