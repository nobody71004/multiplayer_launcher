"""Re-anchor Phase 5 splice block to end-of-file.

Reads each target file, strips any existing sentinel block, and appends
the canonical block at EOF. Idempotent: if the sentinel is already at EOF,
no-op. Run with one argument: UMBRELLA_ROOT.
"""

import os
import sys

def _atomic_write(path, content):
    """Write content to path atomically: write to a sibling .tmp file then
    os.replace() so the target is never truncated mid-write on crash."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


SENTINEL_PAIRS = {
    "server_py": ("# === xbuniverse_phase5 BEGIN ===",
                  "# === xbuniverse_phase5 END ==="),
    "index_html": ("<!-- xbuniverse_phase5 BEGIN -->",
                   "<!-- xbuniverse_phase5 END -->"),
    "init_lua": ("-- xbuniverse_phase5 BEGIN",
                 "-- xbuniverse_phase5 END"),
}

SERVER_PY_BLOCK_TAIL = '''

# === xbuniverse_phase5 BEGIN ===
import time as _xb_time

_XB_UMBRELLA_SERVICES = [
    {"id": "xbuniverse_server", "name": "XBUNIVERSE Flask web panel",
     "cwd": "xbuniverse_server", "command": ["python", "server.py"], "port": 5000},
    {"id": "matchmaker", "name": "Matchmaker daemon",
     "cwd": ".", "command": ["python", "matchmaking_server.py", "--port", "5001"], "port": 5001},
    {"id": "launcher", "name": "Cyberpunk multiplayer launcher",
     "cwd": ".", "command": ["python", "multiplayer_launcher.py"], "port": None},
]


def _xb_service_status(svc):
    target = svc.get("command", [svc["id"]])[-1]
    running = False
    pid = None
    cmd_lower = target.lower()
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "ProcessId,CommandLine", "/FORMAT:CSV"],
            capture_output=True, text=True, timeout=8, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        out = None
    if out is not None and out.stdout:
        for ln in out.stdout.splitlines():
            low = ln.lower()
            if cmd_lower not in low or "commandline" in low or "," not in ln:
                continue
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) >= 2 and parts[-1].isdigit():
                pid = int(parts[-1])
                running = True
                break
    return {"id": svc["id"], "name": svc["name"],
            "running": running, "pid": pid,
            "command": " ".join(svc.get("command", []))}


def _xb_restart_service(svc):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cwd = os.path.join(repo_root, svc["cwd"]) if svc["cwd"] != "." else repo_root
    kwargs = {"cwd": cwd}
    logs_dir = os.path.join(repo_root, "logs")
    if os.path.isdir(logs_dir):
        out_path = os.path.join(logs_dir, svc["id"] + ".restart.out")
        try:
            kwargs["stdout"] = open(out_path, "ab", encoding="utf-8", errors="replace")
            kwargs["stderr"] = subprocess.STDOUT
        except OSError:
            pass
    return subprocess.Popen(svc["command"], **kwargs)


@app.route("/api/ping", methods=["GET"])
def api_ping():
    """No-auth, no-work ping target for in-game HUD RTT measurement."""
    return jsonify({"now_ms": int(_xb_time.time() * 1000)})


@app.route("/api/admin/logs", methods=["GET"])
@login_required
def api_admin_logs():
    if not _is_admin(g.user):
        return jsonify({"error": "forbidden"}), 403
    n = max(1, min(int(request.args.get("n", "200")), 2000))
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")

    def tail_lines(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return [ln.rstrip("\\n") for ln in f.readlines()[-n:]]
        except (FileNotFoundError, OSError):
            return []

    app_log = os.path.join(logs_dir, "app.log")
    audit_log = os.path.join(logs_dir, "audit.log")
    app_lines = tail_lines(app_log)
    audit_lines = tail_lines(audit_log)
    return jsonify({"app": app_lines, "audit": audit_lines,
                    "truncated_app": len(app_lines) >= n,
                    "truncated_audit": len(audit_lines) >= n})


@app.route("/api/admin/servers", methods=["GET"])
@login_required
def api_admin_servers():
    if not _is_admin(g.user):
        return jsonify({"error": "forbidden"}), 403
    services = [_xb_service_status(s) for s in _XB_UMBRELLA_SERVICES]
    _audit_log("servers.list", actor=g.user["username"], target=None,
               details={"count": len(services)})
    return jsonify({"services": services})


@app.route("/api/admin/servers/<svc_id>/restart", methods=["POST"])
@login_required
def api_admin_servers_restart(svc_id):
    if not _is_admin(g.user):
        return jsonify({"error": "forbidden"}), 403
    svc = next((s for s in _XB_UMBRELLA_SERVICES if s["id"] == svc_id), None)
    if not svc:
        return jsonify({"error": "unknown_service"}), 404
    before = _xb_service_status(svc)
    killed_pid = before["pid"] if before["running"] else None
    if killed_pid:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(killed_pid)],
                           capture_output=True, text=True, timeout=5, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass
    try:
        proc = _xb_restart_service(svc)
        new_pid = proc.pid
    except OSError as exc:
        _audit_log("servers.restart.fail", actor=g.user["username"],
                   target=svc_id, details={"error": str(exc), "killed_pid": killed_pid})
        return jsonify({"error": "spawn_failed", "detail": str(exc)}), 500
    _audit_log("servers.restart", actor=g.user["username"],
               target=svc_id, details={"killed_pid": killed_pid, "new_pid": new_pid})
    return jsonify({"service": svc_id, "killed_pid": killed_pid,
                    "new_pid": new_pid, "status": "ok"})


@app.route("/api/admin/users/bulk", methods=["POST"])
@login_required
def api_admin_users_bulk():
    if not _is_admin(g.user):
        return jsonify({"error": "forbidden"}), 403
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    user_ids = body.get("user_ids") or []
    if action not in {"kick", "ban", "disable", "promote"}:
        return jsonify({"error": "invalid_action"}), 400
    if not isinstance(user_ids, list) or not user_ids:
        return jsonify({"error": "no_users"}), 400
    results = []
    for uid in user_ids:
        try:
            endpoint_fn = globals().get("api_admin_users_" + action, None)
            if endpoint_fn is None:
                results.append({"user_id": uid, "status": "skipped",
                                "reason": "no per-user endpoint"})
                continue
            with app.test_request_context("/api/admin/users/" + str(uid) + "/" + action,
                                          method="POST"):
                sess_user = session.get("user") if session else None
                g.user = sess_user or {"username": "root", "is_admin": True}
                rv = endpoint_fn(uid)
            code = rv[1] if isinstance(rv, tuple) else 200
            results.append({"user_id": uid, "status": "ok" if code < 400 else "fail",
                            "code": code})
        except Exception as exc:
            results.append({"user_id": uid, "status": "fail", "error": str(exc)})
    _audit_log("users.bulk", actor=g.user["username"], target=None,
               details={"action": action, "user_ids": user_ids,
                        "result_codes": [r.get("status") for r in results]})
    return jsonify({"action": action, "results": results})
# === xbuniverse_phase5 END ===


'''.lstrip("\\n")


def strip_block(text, begin, end):
    """Strip the begin..end block (inclusive of both markers). Returns
    (new_text, present)."""
    b = text.find(begin)
    if b < 0:
        return text, False
    e_start = text.find(end, b + len(begin))
    if e_start < 0:
        return text, False
    e_end = e_start + len(end)
    new = text[:b] + text[e_end:]
    return new, True


def main():
    root = sys.argv[1]
    server_py = os.path.join(root, "xbuniverse_server", "server.py")
    src = open(server_py, encoding="utf-8").read()
    begin, end = SENTINEL_PAIRS["server_py"]
    src2, was_present = strip_block(src, begin, end)
    if was_present:
        print("server.py: stripped existing block (was at offset "
              + str(src.find(begin)) + ")")
    else:
        print("server.py: no existing block to strip")
    # Ensure file ends with newline before appending.
    if not src2.endswith("\\n"):
        src2 += "\\n"
    src2 += SERVER_PY_BLOCK_TAIL
    _atomic_write(server_py, src2)
    print("server.py: appended block at EOF, new length", len(src2))


if __name__ == "__main__":
    main()
