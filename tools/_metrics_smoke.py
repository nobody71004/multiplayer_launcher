"""End-to-end smoke for /metrics + /api/logs + /admin/.

Boots matchmaking_server.py as a subprocess on an ephemeral port,
hits each new endpoint via HTTP, and asserts the contract:
  - /metrics returns text-format with our counter + gauge names.
  - /api/logs returns JSON envelope {"events": [...]}.
  - /admin/ returns HTML containing the polling JS.
  - After a /api/register, the registrations_total counter
    increments in the next /metrics hit.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _wait_for_health(base: str, deadline_s: float = 5.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            r = requests.get(base + "/api/health", timeout=0.5)
            if r.ok:
                return True
        except requests.RequestException:
            time.sleep(0.1)
    return False


def main() -> int:
    port = _free_port()
    db_path = ROOT / ".tmp" / f"_metrics_smoke_{os.getpid()}.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    proc_args = [
        sys.executable,
        str(ROOT / "matchmaking_server.py"),
        "--host", "127.0.0.1",
        "--port", str(port),
        "--db", str(db_path),
        "--maintenance-interval-sec", "0",  # disable the maintenance loop
    ]
    env = os.environ.copy()
    env.pop("MATCHMAKER_USE_INMEMORY", None)  # force SqliteStorage

    proc = subprocess.Popen(
        proc_args, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, env=env,
    )
    print(f"[metrics-smoke] started matchmaker pid={proc.pid} port={port}", flush=True)
    try:
        base = f"http://127.0.0.1:{port}"
        if not _wait_for_health(base):
            print("[metrics-smoke] ! matchmaker never came up", flush=True)
            return 1

        # ---- /metrics ----
        r = requests.get(base + "/metrics", timeout=3)
        assert r.status_code == 200
        assert r.headers["Content-Type"].startswith("text/plain")
        for key in [
            "matchmaker_registrations_total",
            "matchmaker_logins_total",
            "matchmaker_heartbeats_total",
            "matchmaker_active_servers_count",
            "matchmaker_request_latency_seconds_count",
            "matchmaker_maintenance_purged_total",
        ]:
            assert key in r.text, f"{key} missing from /metrics"
        print("[metrics-smoke] /metrics PASS", flush=True)

        # ---- /api/logs ----
        r = requests.get(base + "/api/logs?limit=20", timeout=3)
        assert r.status_code == 200
        body = r.json()
        assert "events" in body
        print(
            f"[metrics-smoke] /api/logs PASS (events={len(body['events'])})",
            flush=True,
        )

        # ---- /admin/ ----
        r = requests.get(base + "/admin/", timeout=3)
        assert r.status_code == 200
        assert "Admin Log Viewer" in r.text
        # Template builds the URL dynamically (`const url = "/api/logs?..."`),
        # so assert the path + poller wiring; matches test_telemetry.py
        # so the two can't drift apart on a future template tweak.
        assert "/api/logs" in r.text
        assert "async function pollLogs" in r.text
        assert "setInterval(pollLogs" in r.text
        print("[metrics-smoke] /admin/ PASS", flush=True)

        # ---- counter increments on a real register round-trip ----
        requests.post(
            base + "/api/register",
            json={"username": "smoke_user", "password": "smoke_pw_1234"},
        )
        r = requests.get(base + "/metrics", timeout=3)
        assert 'matchmaker_registrations_total{result="created"}' in r.text
        print(
            "[metrics-smoke] registrations_total{result=created} "
            "incremented PASS",
            flush=True,
        )

        print("[metrics-smoke] all PASS", flush=True)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
        try:
            db_path.unlink(missing_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
