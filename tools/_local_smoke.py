"""Local end-to-end smoke for the headless launcher stack.

Spins up matchmaking_server.py + tools/fake_game_server.py in-process
via subprocess.Popen, verifies GET /api/servers returns the demo-srv-01
entry, then tears down. Used for one-off verification — NOT a
long-lived dev tool.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT  # write logs to project root for easy post-mortem; clean after


def main() -> int:
    mm_log = open(LOGS / "smoke_mm.log", "w", encoding="utf-8")
    fs_log = open(LOGS / "smoke_fs.log", "w", encoding="utf-8")
    mm = None
    fs = None
    # Explicit success flag -- the teardown loop in `finally:` always
    # completes (and sets mm.returncode / fs.returncode) regardless of
    # whether the try block returned 0 or raised/returned non-zero,
    # so returncode alone can't distinguish success from failure.
    # Gate the log-scrubber on this flag instead so failures PRESERVE
    # the post-mortem logs.
    success = False
    try:
        # 1) matchmaker
        mm = subprocess.Popen(
            [sys.executable, str(ROOT / "matchmaking_server.py"),
             "--host", "127.0.0.1", "--port", "5017"],
            stdout=mm_log, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                r = requests.get("http://127.0.0.1:5017/api/health", timeout=1)
                if r.ok:
                    print("[smoke] matchmaker healthy on http://127.0.0.1:5017", flush=True)
                    break
            except requests.RequestException:
                time.sleep(0.3)
        else:
            print("[smoke] ! matchmaker never came up within 10s", flush=True)
            return 2

        # 2) fake_game_server
        fs = subprocess.Popen(
            [sys.executable, str(ROOT / "tools" / "fake_game_server.py"),
             "--matchmaker-url", "http://127.0.0.1:5017",
             "--bind-port", "7777",
             "--heartbeat-s", "5",
             "--username", "demo-fake-srv",
             "--password", "demo-fake-pw-1234",
             "--server-id", "demo-srv-01",
             "--server-name", "Demo Setup Server"],
            stdout=fs_log, stderr=subprocess.STDOUT, text=True,
        )
        print(f"[smoke] fake_game_server started (pid={fs.pid})", flush=True)

        # 3) wait for first heartbeat (heartbeat-s=5 + slack)
        print("[smoke] waiting 7s for first heartbeat to land...", flush=True)
        time.sleep(7)

        # 4) curl /api/servers
        srvs = requests.get("http://127.0.0.1:5017/api/servers", timeout=3).json()["servers"]
        print("[smoke] === GET /api/servers ===", flush=True)
        for s in srvs:
            print(
                f"  id={s['id']} name={s['name']!r} host={s['host']}:{s['port']} "
                f"players={s['players']}/{s['max_players']}",
                flush=True,
            )
        found = [s for s in srvs if s["id"] == "demo-srv-01"]
        if not found:
            print("[smoke] ! demo-srv-01 NOT in /api/servers", flush=True)
            return 3
        print("[smoke] PASS: demo-srv-01 visible in /api/servers", flush=True)

        # 5) wait a heartbeat's worth more so we can confirm heartbeats keep firing
        print("[smoke] waiting another 6s to confirm heartbeat keeps firing...", flush=True)
        time.sleep(6)

        # 6) capture fake_game_server log so the parent can summarize
        fs.terminate()
        try:
            fs_out, _ = fs.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            fs.kill()
            fs_out, _ = fs.communicate()
        fs = None
        print("[smoke] === fake_game_server stdout ===", flush=True)
        print(fs_out)

        # 7) final /api/servers snapshot
        srvs2 = requests.get(
            "http://127.0.0.1:5017/api/servers", timeout=3
        ).json()["servers"]
        print("[smoke] === final /api/servers (after second heartbeat) ===", flush=True)
        for s in srvs2:
            print(
                f"  id={s['id']} name={s['name']!r} host={s['host']}:{s['port']} "
                f"players={s['players']}/{s['max_players']}",
                flush=True,
            )
        print("[smoke] SMOKE TEST COMPLETE", flush=True)
        success = True
        return 0
    finally:
        for proc_name, proc in (("fake_game_server", fs), ("matchmaker", mm)):
            if proc is None:
                continue
            try:
                proc.terminate()
                proc.wait(timeout=3)
                print(f"[smoke] torn down {proc_name} (pid={proc.pid})", flush=True)
            except Exception as e:
                print(
                    f"[smoke] error tearing {proc_name} down: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                try:
                    proc.kill()
                except Exception:
                    pass
        mm_log.close()
        fs_log.close()
        # Scrub the smoke logs ONLY on success so a clean re-run starts
        # from a fresh state. On failure the logs are preserved so the
        # operator can post-mortem which subprocess printed what.
        if success:
            for log_path in (LOGS / "smoke_mm.log", LOGS / "smoke_fs.log"):
                try:
                    log_path.unlink(missing_ok=True)
                except OSError:
                    pass


if __name__ == "__main__":
    sys.exit(main())
