"""Phase 5 splice for XBUNIVERSE umbrella.

Designed to be invoked as:
    python _xb_splice_phase5.py <UMBRELLA_PATH>

Where UMBRELLA_PATH is the absolute path to the umbrella repo root.

The script is IDEMPOTENT: it uses sentinel markers so re-running won't double-append.

Edit shape:
  - server.py: append a contiguous block of routes/Helpers just before the
    `if __name__ == "__main__":` block, enclosed between
    `# === xbuniverse_phase5 BEGIN ===` and `# === xbuniverse_phase5 END ===`.
  - index.html: append a `<button>` + floating console + admin servers
    section + bootstrap script just before `</body>`, between
    `<!-- xbuniverse_phase5 BEGIN -->` and `<!-- xbuniverse_phase5 END -->`.
  - init.lua: append the HUD module at EOF, between
    `-- xbuniverse_phase5 BEGIN` and `-- xbuniverse_phase5 END`.
  - tests file at xbuniverse_server/tests/test_admin_extras_ping_logger_servers.py.
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


SERVER_PY_BEGIN = "# === xbuniverse_phase5 BEGIN ==="
SERVER_PY_END = "# === xbuniverse_phase5 END ==="

INDEX_BEGIN = "<!-- xbuniverse_phase5 BEGIN -->"
INDEX_END = "<!-- xbuniverse_phase5 END -->"

LUA_BEGIN = "-- xbuniverse_phase5 BEGIN"
LUA_END = "-- xbuniverse_phase5 END"

SERVER_PY_BLOCK = r'''

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
    """Best-effort cross-platform process discovery via subprocess.

    Uses `wmic` on Windows to match a python.exe whose CommandLine ends with
    the target script name. Returns a dict with id/name/running/pid/command.
    """
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
                return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
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
'''.lstrip("\n")


INDEX_BLOCK = r'''

<!-- xbuniverse_phase5 BEGIN -->
<button id="xbConsoleToggleBtn" class="admin-only" type="button"
        title="Toggle floating logs console" onclick="XB.toggleConsole()">
  <span class="xb-bullet"></span> Logs
</button>
<div id="xbConsoleOverlay" class="xb-console-overlay admin-only hidden" tabindex="-1">
  <div class="xb-console-frame">
    <div class="xb-console-header">
      <span class="xb-console-title">XBUNIVERSE Logs (live)</span>
      <div class="xb-console-actions">
        <label><input type="checkbox" id="xbConsoleAutoRefresh" checked /> auto-refresh</label>
        <label><input type="checkbox" id="xbConsoleAuditOnly" /> audit only</label>
        <button type="button" onclick="XB.toggleConsole()">Close</button>
      </div>
    </div>
    <pre id="xbConsoleBody" class="xb-console-body"></pre>
  </div>
</div>
<section id="adminServersSection" class="admin-only" hidden>
  <h3>Umbrella Services</h3>
  <p class="muted">Detected via WMIC. Restart = terminate then re-spawn.</p>
  <table id="adminServersTable" class="admin-table">
    <thead><tr><th>ID</th><th>Name</th><th>Status</th><th>PID</th><th>Action</th></tr></thead>
    <tbody></tbody>
  </table>
</section>
<script>
window.XB = window.XB || {};
XB.toggleConsole = function () {
  var ov = document.getElementById("xbConsoleOverlay");
  if (!ov) return;
  ov.classList.toggle("hidden");
  if (!ov.classList.contains("hidden")) {
    XB.refreshConsole();
    if (XB._consoleTimer) clearInterval(XB._consoleTimer);
    XB._consoleTimer = setInterval(function () {
      var ar = document.getElementById("xbConsoleAutoRefresh");
      if (ar && ar.checked) XB.refreshConsole();
    }, 5000);
  } else if (XB._consoleTimer) {
    clearInterval(XB._consoleTimer);
    XB._consoleTimer = null;
  }
};
XB.refreshConsole = function () {
  if (typeof window._user === "undefined" || !_user || !_user.is_admin) return;
  apiFetch("/api/admin/logs?n=200").then(function (resp) {
    var body = document.getElementById("xbConsoleBody");
    if (!body) return;
    var auditOnly = document.getElementById("xbConsoleAuditOnly").checked;
    var lines = auditOnly
      ? (resp.audit || [])
      : (resp.app || []).concat(["-- audit --"]).concat(resp.audit || []);
    body.textContent = lines.join("\n");
    body.scrollTop = body.scrollHeight;
  }).catch(function () {});
};
XB.refreshServers = function () {
  if (typeof window._user === "undefined" || !_user || !_user.is_admin) return;
  apiFetch("/api/admin/servers").then(function (resp) {
    var tbody = document.querySelector("#adminServersTable tbody");
    if (!tbody) return;
    tbody.innerHTML = "";
    (resp.services || []).forEach(function (s) {
      var tr = document.createElement("tr");
      var td = function (t) {
        var e = document.createElement("td");
        e.textContent = t;
        return e;
      };
      tr.appendChild(td(s.id));
      tr.appendChild(td(s.name));
      var sTd = document.createElement("td");
      sTd.textContent = s.running ? "running" : "stopped";
      sTd.className = s.running ? "ok" : "err";
      tr.appendChild(sTd);
      tr.appendChild(td(s.pid == null ? "\u2014" : String(s.pid)));
      var aTd = document.createElement("td");
      var btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = "Restart";
      btn.onclick = function () {
        if (!confirm("Restart service " + s.id + "?")) return;
        apiFetch("/api/admin/servers/" + encodeURIComponent(s.id) + "/restart",
                 {method: "POST"})
          .then(XB.refreshServers)
          .catch(function (e) { alert("Restart failed: " + e.message); });
      };
      aTd.appendChild(btn);
      tr.appendChild(aTd);
      tbody.appendChild(tr);
    });
  }).catch(function () {});
};
setTimeout(function () {
  if (XB._serversTimer) clearInterval(XB._serversTimer);
  XB._serversTimer = setInterval(XB.refreshServers, 5000);
  var origShow = window.showAdmin || function () {};
  window.showAdmin = function () {
    origShow.apply(this, arguments);
    XB.refreshServers();
  };
}, 200);
</script>
<!-- xbuniverse_phase5 END -->
'''.lstrip("\n")


LUA_BLOCK = r'''

-- xbuniverse_phase5 BEGIN
local XB_HUD = {
    last_ping_ms = nil,
    ping_color = 0xFFFFFFFF,
    server_name = "matchmaker",
    server_url = "http://127.0.0.1:5000/api/ping",
    poll_period_ms = 1000,
    last_poll_at = 0,
    fallback_state_file = nil,
}

local function xb_color_for_ms(ms)
    if ms == nil then return 0xFFAAAAAA end
    if ms < 50 then return 0xFF66FF66 end
    if ms < 150 then return 0xFFFFCC66 end
    return 0xFFFF6666
end

local function xb_poll_ping_now()
    local ok, curl = pcall(require, "curl")
    if ok and type(curl) == "table" and curl.easy_init then
        local c = curl.easy_init()
        curl.easy_setopt(c, curl.OPT_URL, XB_HUD.server_url)
        curl.easy_setopt(c, curl.OPT_TIMEOUT, 2)
        local t0 = os.clock()
        local perr = curl.easy_perform(c)
        local t1 = os.clock()
        if perr == 0 then
            XB_HUD.last_ping_ms = math.floor(((t1 - t0) * 1000))
            XB_HUD.ping_color = xb_color_for_ms(XB_HUD.last_ping_ms)
            return true
        end
    end
    local fallback = XB_HUD.fallback_state_file
    if fallback then
        local f = io.open(fallback, "r")
        if f then
            local content = f:read("*a")
            f:close()
            local ms = tonumber(content:match('"last_ping_ms"%s*:%s*(%d+)'))
            if ms then
                XB_HUD.last_ping_ms = ms
                XB_HUD.ping_color = xb_color_for_ms(ms)
                return true
            end
        end
    end
    return false
end

local function xb_register_ping_hud()
    local res_w, res_h = GetDisplayResolution()
    ImGui.SetNextWindowPos(res_w / 2 - 90, 8, 1)
    ImGui.SetNextWindowSize(180, 20)
    ImGui.Begin("XB Ping HUD",
        ImGui.WindowFlags_NoTitleBar
        + ImGui.WindowFlags_NoInputs
        + ImGui.WindowFlags_AlwaysAutoResize)
    if XB_HUD.last_ping_ms == nil then
        ImGui.TextColored(0xFFAAAAAA, "XB - pinging...")
    else
        local ms_text = string.format("%s  %d ms",
            XB_HUD.server_name, XB_HUD.last_ping_ms)
        ImGui.TextColored(XB_HUD.ping_color, ms_text)
    end
    ImGui.End()
end

local function xb_init_ping_hud()
    XB_HUD.fallback_state_file = GetMod("xbuniverse_plugin"):GetRootDir()
        .. ".." .. string.char(92) .. ".."
        .. string.char(92) .. "xbuniverse_server"
        .. string.char(92) .. "logs"
        .. string.char(92) .. "xb_ping_state.json"
    registerForEvent("onUpdate", function()
        local now = os.clock() * 1000
        if (now - XB_HUD.last_poll_at) >= XB_HUD.poll_period_ms then
            XB_HUD.last_poll_at = now
            xb_poll_ping_now()
        end
        xb_register_ping_hud()
    end)
end

pcall(xb_init_ping_hud)
-- xbuniverse_phase5 END
'''.lstrip("\n")


TEST_FILE_BLOCK = '''"""Regression tests for Phase 5 single-file appended endpoints."""
import sys
import pathlib

p = str(pathlib.Path(__file__).resolve().parents[2])
if p not in sys.path:
    sys.path.insert(0, p)

import pytest
from xbuniverse_server.server import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


def _set_admin_session(client, username="root"):
    with client.session_transaction() as sess:
        sess["user"] = {"username": username, "is_admin": True}


def test_api_ping_returns_ms(client):
    resp = client.get("/api/ping")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "now_ms" in data
    assert isinstance(data["now_ms"], int)
    assert data["now_ms"] > 0


def test_api_admin_logs_requires_admin(client):
    resp = client.get("/api/admin/logs?n=10")
    assert resp.status_code in (401, 403)


def test_api_admin_logs_returns_lines(client):
    _set_admin_session(client)
    resp = client.get("/api/admin/logs?n=10")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, dict)
    assert "app" in data and "audit" in data
    assert isinstance(data["app"], list)
    assert isinstance(data["audit"], list)


def test_api_admin_servers_lists_known(client):
    _set_admin_session(client)
    resp = client.get("/api/admin/servers")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "services" in data
    ids = [s["id"] for s in data["services"]]
    for required in ("xbuniverse_server", "matchmaker", "launcher"):
        assert required in ids, "missing " + required


def test_api_admin_servers_restart_unknown_404(client):
    _set_admin_session(client)
    resp = client.post("/api/admin/servers/no-such-svc/restart")
    assert resp.status_code == 404


def test_api_admin_users_bulk_requires_admin(client):
    resp = client.post(
        "/api/admin/users/bulk",
        json={"action": "kick", "user_ids": ["root"]},
    )
    assert resp.status_code in (401, 403)


def test_api_admin_users_bulk_validates_action(client):
    _set_admin_session(client)
    resp = client.post(
        "/api/admin/users/bulk",
        json={"action": "nope", "user_ids": ["root"]},
    )
    assert resp.status_code == 400


def test_api_admin_users_bulk_no_users(client):
    _set_admin_session(client)
    resp = client.post(
        "/api/admin/users/bulk",
        json={"action": "kick", "user_ids": []},
    )
    assert resp.status_code == 400
'''


def splice_around(src, begin, end, block, anchor_text=None):
    """Idempotent splice: replace any existing begin..end block, or
    insert before anchor_text (or at EOF if anchor_text is None).
    """
    b_idx = src.find(begin)
    e_idx = src.find(end, b_idx + len(begin) + 1) if b_idx >= 0 else -1
    if b_idx >= 0 and e_idx >= 0:
        replaced = src[:b_idx] + block + src[e_idx + len(end):]
        return replaced, "replaced"
    if anchor_text is None:
        return src + block, "appended"
    a_idx = src.find(anchor_text)
    if a_idx < 0:
        raise ValueError("anchor_text not found: " + repr(anchor_text))
    inserted = src[:a_idx] + block + src[a_idx:]
    return inserted, "inserted-at-anchor"


def splice_server_py(text):
    anchor = '\nif __name__ == "__main__":\n'
    return splice_around(text, SERVER_PY_BEGIN, SERVER_PY_END,
                          SERVER_PY_BLOCK, anchor)


def splice_index_html(text):
    return splice_around(text, INDEX_BEGIN, INDEX_END,
                          INDEX_BLOCK, "</body>")


def splice_init_lua(text):
    return splice_around(text, LUA_BEGIN, LUA_END,
                          LUA_BLOCK, None)


def main():
    if len(sys.argv) != 2:
        print("Usage: _xb_splice_phase5.py UMBRELLA_ROOT", file=sys.stderr)
        sys.exit(2)
    root = sys.argv[1]
    server_py = os.path.join(root, "xbuniverse_server", "server.py")
    index_html = os.path.join(root, "xbuniverse_server", "index.html")
    init_lua = os.path.join(root, "xbuniverse_plugin", "cet", "init.lua")
    tests_path = os.path.join(
        root, "xbuniverse_server", "tests",
        "test_admin_extras_ping_logger_servers.py",
    )

    for path in (server_py, index_html, init_lua):
        if not os.path.isfile(path):
            print("missing: " + path, file=sys.stderr)
            sys.exit(3)

    src_server = open(server_py, encoding="utf-8").read()
    src_index = open(index_html, encoding="utf-8").read()
    src_lua = open(init_lua, encoding="utf-8").read()

    new_server, info_s = splice_server_py(src_server)
    new_index, info_i = splice_index_html(src_index)
    new_lua, info_l = splice_init_lua(src_lua)

    _atomic_write(server_py, new_server)
    _atomic_write(index_html, new_index)
    _atomic_write(init_lua, new_lua)
    os.makedirs(os.path.dirname(tests_path), exist_ok=True)
    _atomic_write(tests_path, TEST_FILE_BLOCK)

    print("server.py:", info_s, "len before", len(src_server),
          "after", len(new_server))
    print("index.html:", info_i, "len before", len(src_index),
          "after", len(new_index))
    print("init.lua:", info_l, "len before", len(src_lua),
          "after", len(new_lua))
    print("tests file:", tests_path, "len", len(TEST_FILE_BLOCK))


if __name__ == "__main__":
    main()
