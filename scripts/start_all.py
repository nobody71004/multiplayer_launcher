"""Dev convenience: spin up matchmaker, exercise register/login/heartbeat/servers,
and run the engine stub subprocess end-to-end as a single sanity check.

Usage:
    python scripts/start_all.py

Exits 0 on success, 1 on any failure. CI / `just` targets can call this.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402


def start_matchmaker(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable, str(ROOT / "matchmaking_server.py"),
            "--host", "127.0.0.1", "--port", str(port),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def wait_for_health(base: str, attempts: int = 50, delay: float = 0.1) -> bool:
    for _ in range(attempts):
        try:
            r = requests.get(f"{base}/api/health", timeout=1)
            if r.ok:
                return True
        except requests.RequestException:
            time.sleep(delay)
    return False


def heartbeat(base: str, token: str, server_id: str, name: str,
              players: int = 3, max_players: int = 16) -> None:
    requests.post(
        f"{base}/api/heartbeat",
        json={
            "token": token, "server_id": server_id, "name": name,
            "host": "127.0.0.1", "port": 7777,
            "players": players, "max_players": max_players,
        },
        timeout=5,
    )


def main() -> int:
    pid = os.getpid()
    port = int(os.environ.get("MATCHMAKER_PORT", "5017"))
    base = f"http://127.0.0.1:{port}"
    # Per-PID-scoped identifiers so re-runs of this script can't 409 on a
    # fresh register call against a still-warm matchmaker.
    username = f"demo-{pid}"
    server_id = f"srv-{pid}"

    proc = start_matchmaker(port)
    try:
        if not wait_for_health(base):
            print("matchmaker never came up", file=sys.stderr)
            return 1
        print(f"[start_all] matchmaker healthy at {base}")

        # Every step below hits the SUBPROCESS matchmaker — single source of
        # state truth. Tokens are in-process state, so an in-process Flask
        # test_client would issue a token invisible to the subprocess.
        r = requests.post(
            f"{base}/api/register",
            json={"username": username, "password": "demopw"},
            timeout=5,
        )
        assert r.status_code == 201, (r.status_code, r.text)
        tok = requests.post(
            f"{base}/api/login",
            json={"username": username, "password": "demopw"},
            timeout=5,
        ).json()["token"]

        heartbeat(base, tok, server_id=server_id, name=f"server-{pid}")
        srvs = requests.get(f"{base}/api/servers", timeout=5).json()["servers"]
        assert srvs, "no servers visible after heartbeat"
        assert srvs[0]["id"] == server_id, srvs

        # Engine stub subprocess — exercises the full argv contract.
        stub = subprocess.run(
            [
                sys.executable, str(ROOT / "game_engine" / "game_stub.py"),
                "--server", "127.0.0.1", "--port", "7777",
                "--token", tok, "--username", username,
                "--ticks", "2",
            ],
            capture_output=True, text=True, timeout=10,
        )
        assert stub.returncode == 0, stub.stderr
        assert "[engine] connect" in stub.stdout, stub.stdout
        assert "[engine] tick 2/2" in stub.stdout, stub.stdout
        assert "[engine] exit" in stub.stdout, stub.stdout

        print("start_all: OK")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
