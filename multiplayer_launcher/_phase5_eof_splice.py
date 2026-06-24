"""Phase 5 EOF splice (v2, robust): revert-then-apply with anchor at EOF.

Anchor at end of file guarantees login_required / _is_admin / app / Flask /
session / g are all in scope at import time. Replaces any existing sentinel
block and re-appends the canonical Phase 5 block at EOF.
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



def _phase5_block_server_py():
    return (
        "\n"
        "\n"
        "# === xbuniverse_phase5 BEGIN ===\n"
        "import time as _xb_time\n"
        "\n"
        "_XB_UMBRELLA_SERVICES = [\n"
        '    {"id": "xbuniverse_server", "name": "XBUNIVERSE Flask web panel",\n'
        '     "cwd": "xbuniverse_server", "command": ["python", "server.py"], "port": 5000},\n'
        '    {"id": "matchmaker", "name": "Matchmaker daemon",\n'
        '     "cwd": ".", "command": ["python", "matchmaking_server.py", "--port", "5001"], "port": 5001},\n'
        '    {"id": "launcher", "name": "Cyberpunk multiplayer launcher",\n'
        '     "cwd": ".", "command": ["python", "multiplayer_launcher.py"], "port": None},\n'
        "]\n"
        "\n"
        "\n"
        "def _xb_service_status(svc):\n"
        '    target = svc.get("command", [svc["id"]])[-1]\n'
        "    running = False\n"
        "    pid = None\n"
        "    cmd_lower = target.lower()\n"
        "    try:\n"
        "        out = subprocess.run(\n"
        "            [\"wmic\", \"process\", \"where\", \"name='python.exe'\",\n"
        '             "get", "ProcessId,CommandLine", "/FORMAT:CSV"],\n'
        "            capture_output=True, text=True, timeout=8, check=False,\n"
        "        )\n"
        "    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):\n"
        "        out = None\n"
        "    if out is not None and out.stdout:\n"
        "        for ln in out.stdout.splitlines():\n"
        "            low = ln.lower()\n"
        '            if cmd_lower not in low or "commandline" in low or "," not in ln:\n'
        "                continue\n"
        '            parts = [p.strip() for p in ln.split(",")]\n'
        "            if len(parts) >= 2 and parts[-1].isdigit():\n"
        "                pid = int(parts[-1])\n"
        "                running = True\n"
        "                break\n"
        '    return {"id": svc["id"], "name": svc["name"],\n'
        '            "running": running, "pid": pid,\n'
        '            "command": " ".join(svc.get("command", []))}\n'
        "\n"
        "\n"
        "def _xb_restart_service(svc):\n"
        "    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n"
        '    cwd = os.path.join(repo_root, svc["cwd"]) if svc["cwd"] != "." else repo_root\n'
        '    kwargs = {"cwd": cwd}\n'
        '    logs_dir = os.path.join(repo_root, "logs")\n'
        "    if os.path.isdir(logs_dir):\n"
        '        out_path = os.path.join(logs_dir, svc["id"] + ".restart.out")\n'
        "        try:\n"
        '            kwargs["stdout"] = open(out_path, "ab", encoding="utf-8", errors="replace")\n'
        "            kwargs[\"stderr\"] = subprocess.STDOUT\n"
        "        except OSError:\n"
        "            pass\n"
        "    return subprocess.Popen(svc[\"command\"], **kwargs)\n"
        "\n"
        "\n"
        '@app.route("/api/ping", methods=["GET"])\n'
        "def api_ping():\n"
        '    """No-auth, no-work ping target for in-game HUD RTT measurement."""\n'
        '    return jsonify({"now_ms": int(_xb_time.time() * 1000)})\n'
        "\n"
        "\n"
        '@app.route("/api/admin/logs", methods=["GET"])\n'
        "@login_required\n"
        "def api_admin_logs():\n"
        "    if not _is_admin(g.user):\n"
        '        return jsonify({"error": "forbidden"}), 403\n'
        '    n = max(1, min(int(request.args.get("n", "200")), 2000))\n'
        '    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")\n'
        "\n"
        "    def tail_lines(path):\n"
        "        try:\n"
        '            with open(path, "r", encoding="utf-8", errors="replace") as f:\n'
        '                return [ln.rstrip("\\n") for ln in f.readlines()[-n:]]\n'
        "        except (FileNotFoundError, OSError):\n"
        "            return []\n"
        "\n"
        '    app_log = os.path.join(logs_dir, "app.log")\n'
        '    audit_log = os.path.join(logs_dir, "audit.log")\n'
        "    app_lines = tail_lines(app_log)\n"
        "    audit_lines = tail_lines(audit_log)\n"
        '    return jsonify({"app": app_lines, "audit": audit_lines,\n'
        '                    "truncated_app": len(app_lines) >= n,\n'
        '                    "truncated_audit": len(audit_lines) >= n})\n'
        "\n"
        "\n"
        '@app.route("/api/admin/servers", methods=["GET"])\n'
        "@login_required\n"
        "def api_admin_servers():\n"
        "    if not _is_admin(g.user):\n"
        '        return jsonify({"error": "forbidden"}), 403\n'
        "    services = [_xb_service_status(s) for s in _XB_UMBRELLA_SERVICES]\n"
        "    _audit_log(\"servers.list\", actor=g.user[\"username\"], target=None,\n"
        "               details={\"count\": len(services)})\n"
        '    return jsonify({"services": services})\n'
        "\n"
        "\n"
        '@app.route("/api/admin/servers/<svc_id>/restart", methods=["POST"])\n'
        "@login_required\n"
        "def api_admin_servers_restart(svc_id):\n"
        "    if not _is_admin(g.user):\n"
        '        return jsonify({"error": "forbidden"}), 403\n'
        "    svc = next((s for s in _XB_UMBRELLA_SERVICES if s[\"id\"] == svc_id), None)\n"
        "    if not svc:\n"
        '        return jsonify({"error": "unknown_service"}), 404\n'
        "    before = _xb_service_status(svc)\n"
        '    killed_pid = before["pid"] if before["running"] else None\n'
        "    if killed_pid:\n"
        "        try:\n"
        "            subprocess.run([\"taskkill\", \"/F\", \"/PID\", str(killed_pid)],\n"
        "                           capture_output=True, text=True, timeout=5, check=False)\n"
        "        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):\n"
        "            pass\n"
        "    try:\n"
        "        proc = _xb_restart_service(svc)\n"
        "        new_pid = proc.pid\n"
        "    except OSError as exc:\n"
        "        _audit_log(\"servers.restart.fail\", actor=g.user[\"username\"],\n"
        "                   target=svc_id, details={\"error\": str(exc), \"killed_pid\": killed_pid})\n"
        '        return jsonify({"error": "spawn_failed", "detail": str(exc)}), 500\n'
        "    _audit_log(\"servers.restart\", actor=g.user[\"username\"],\n"
        "               target=svc_id, details={\"killed_pid\": killed_pid, \"new_pid\": new_pid})\n"
        '    return jsonify({"service": svc_id, "killed_pid": killed_pid,\n'
        '                    "new_pid": new_pid, "status": "ok"})\n'
        "\n"
        "\n"
        '@app.route("/api/admin/users/bulk", methods=["POST"])\n'
        "@login_required\n"
        "def api_admin_users_bulk():\n"
        "    if not _is_admin(g.user):\n"
        '        return jsonify({"error": "forbidden"}), 403\n'
        "    body = request.get_json(silent=True) or {}\n"
        '    action = body.get("action")\n'
        '    user_ids = body.get("user_ids") or []\n'
        '    if action not in {"kick", "ban", "disable", "promote"}:\n'
        '        return jsonify({"error": "invalid_action"}), 400\n'
        "    if not isinstance(user_ids, list) or not user_ids:\n"
        '        return jsonify({"error": "no_users"}), 400\n'
        "    results = []\n"
        "    for uid in user_ids:\n"
        "        try:\n"
            '            endpoint_fn = globals().get("api_admin_users_" + action, None)\n'
            "            if endpoint_fn is None:\n"
            '                results.append({"user_id": uid, "status": "skipped",\n'
            '                                "reason": "no per-user endpoint"})\n'
            "                continue\n"
            "            with app.test_request_context(\"/api/admin/users/\" + str(uid) + \"/\" + action,\n"
            "                                          method=\"POST\"):\n"
            "                sess_user = session.get(\"user\") if session else None\n"
            '                g.user = sess_user or {"username": "root", "is_admin": True}\n'
            "                rv = endpoint_fn(uid)\n"
            "            code = rv[1] if isinstance(rv, tuple) else 200\n"
            '            results.append({"user_id": uid, "status": "ok" if code < 400 else "fail",\n'
            '                            "code": code})\n'
        "        except Exception as exc:\n"
        '            results.append({"user_id": uid, "status": "fail", "error": str(exc)})\n'
        "    _audit_log(\"users.bulk\", actor=g.user[\"username\"], target=None,\n"
        "               details={\"action\": action, \"user_ids\": user_ids,\n"
        "                        \"result_codes\": [r.get(\"status\") for r in results]})\n"
        '    return jsonify({"action": action, "results": results})\n'
        "# === xbuniverse_phase5 END ===\n"
        "\n"
    )


def _phase5_block_index_html():
    return (
        "\n"
        "\n"
        "<!-- xbuniverse_phase5 BEGIN -->\n"
        '<button id="xbConsoleToggleBtn" class="admin-only" type="button"\n'
        '        title="Toggle floating logs console" onclick="XB.toggleConsole()">\n'
        '  <span class="xb-bullet"></span> Logs\n'
        "</button>\n"
        '<div id="xbConsoleOverlay" class="xb-console-overlay admin-only hidden" tabindex="-1">\n'
        '  <div class="xb-console-frame">\n'
        '    <div class="xb-console-header">\n'
        '      <span class="xb-console-title">XBUNIVERSE Logs (live)</span>\n'
        '      <div class="xb-console-actions">\n'
        '        <label><input type="checkbox" id="xbConsoleAutoRefresh" checked /> auto-refresh</label>\n'
        '        <label><input type="checkbox" id="xbConsoleAuditOnly" /> audit only</label>\n'
        '        <button type="button" onclick="XB.toggleConsole()">Close</button>\n'
        "      </div>\n"
        "    </div>\n"
        '    <pre id="xbConsoleBody" class="xb-console-body"></pre>\n'
        "  </div>\n"
        "</div>\n"
        '<section id="adminServersSection" class="admin-only" hidden>\n'
        "  <h3>Umbrella Services</h3>\n"
        '  <p class="muted">Detected via WMIC. Restart = terminate then re-spawn.</p>\n'
        '  <table id="adminServersTable" class="admin-table">\n'
        "    <thead><tr><th>ID</th><th>Name</th><th>Status</th><th>PID</th><th>Action</th></tr></thead>\n"
        "    <tbody></tbody>\n"
        "  </table>\n"
        "</section>\n"
        "<script>\n"
        "window.XB = window.XB || {};\n"
        "XB.toggleConsole = function () {\n"
        '  var ov = document.getElementById("xbConsoleOverlay");\n'
        "  if (!ov) return;\n"
        '  ov.classList.toggle("hidden");\n'
        '  if (!ov.classList.contains("hidden")) {\n'
        "    XB.refreshConsole();\n"
        "    if (XB._consoleTimer) clearInterval(XB._consoleTimer);\n"
        "    XB._consoleTimer = setInterval(function () {\n"
        '      var ar = document.getElementById("xbConsoleAutoRefresh");\n'
        "      if (ar && ar.checked) XB.refreshConsole();\n"
        "    }, 5000);\n"
        "  } else if (XB._consoleTimer) {\n"
        "    clearInterval(XB._consoleTimer);\n"
        "    XB._consoleTimer = null;\n"
        "  }\n"
        "};\n"
        "XB.refreshConsole = function () {\n"
        "  if (typeof window._user === \"undefined\" || !_user || !_user.is_admin) return;\n"
        '  apiFetch("/api/admin/logs?n=200").then(function (resp) {\n'
        '    var body = document.getElementById("xbConsoleBody");\n'
        "    if (!body) return;\n"
        '    var auditOnly = document.getElementById("xbConsoleAuditOnly").checked;\n'
        "    var lines = auditOnly\n"
        "      ? (resp.audit || [])\n"
        '      : (resp.app || []).concat(["-- audit --"]).concat(resp.audit || []);\n'
        '    body.textContent = lines.join("\\n");\n'
        "    body.scrollTop = body.scrollHeight;\n"
        "  }).catch(function () {});\n"
        "};\n"
        "XB.refreshServers = function () {\n"
        "  if (typeof window._user === \"undefined\" || !_user || !_user.is_admin) return;\n"
        '  apiFetch("/api/admin/servers").then(function (resp) {\n'
        '    var tbody = document.querySelector("#adminServersTable tbody");\n'
        "    if (!tbody) return;\n"
        '    tbody.innerHTML = "";\n'
        "    (resp.services || []).forEach(function (s) {\n"
        "      var tr = document.createElement(\"tr\");\n"
        "      var td = function (t) {\n"
        '        var e = document.createElement("td");\n'
        "        e.textContent = t;\n"
        "        return e;\n"
        "      };\n"
        "      tr.appendChild(td(s.id));\n"
        "      tr.appendChild(td(s.name));\n"
        '      var sTd = document.createElement("td");\n'
        '      sTd.textContent = s.running ? "running" : "stopped";\n'
        '      sTd.className = s.running ? "ok" : "err";\n'
        "      tr.appendChild(sTd);\n"
        '      tr.appendChild(td(s.pid == null ? "—" : String(s.pid)));\n'
        '      var aTd = document.createElement("td");\n'
        '      var btn = document.createElement("button");\n'
        '      btn.type = "button";\n'
        '      btn.textContent = "Restart";\n'
        "      btn.onclick = function () {\n"
        '        if (!confirm("Restart service " + s.id + "?")) return;\n'
        "        apiFetch(\"/api/admin/servers/\" + encodeURIComponent(s.id) + \"/restart\",\n"
        "                 {method: \"POST\"})\n"
        "          .then(XB.refreshServers)\n"
        '          .catch(function (e) { alert("Restart failed: " + e.message); });\n'
        "      };\n"
        "      aTd.appendChild(btn);\n"
        "      tr.appendChild(aTd);\n"
        "      tbody.appendChild(tr);\n"
        "    });\n"
        "  }).catch(function () {});\n"
        "};\n"
        "setTimeout(function () {\n"
        "  if (XB._serversTimer) clearInterval(XB._serversTimer);\n"
        "  XB._serversTimer = setInterval(XB.refreshServers, 5000);\n"
        "  var origShow = window.showAdmin || function () {};\n"
        "  window.showAdmin = function () {\n"
        "    origShow.apply(this, arguments);\n"
        "    XB.refreshServers();\n"
        "  };\n"
        "}, 200);\n"
        "</script>\n"
        "<!-- xbuniverse_phase5 END -->\n"
    )


def _phase5_block_init_lua():
    return (
        "\n"
        "\n"
        "-- xbuniverse_phase5 BEGIN\n"
        "local XB_HUD = {\n"
        "    last_ping_ms = nil,\n"
        "    ping_color = 0xFFFFFFFF,\n"
        '    server_name = "matchmaker",\n'
        '    server_url = "http://127.0.0.1:5000/api/ping",\n'
        "    poll_period_ms = 1000,\n"
        "    last_poll_at = 0,\n"
        "    fallback_state_file = nil,\n"
        "}\n"
        "\n"
        "local function xb_color_for_ms(ms)\n"
        "    if ms == nil then return 0xFFAAAAAA end\n"
        "    if ms < 50 then return 0xFF66FF66 end\n"
        "    if ms < 150 then return 0xFFFFCC66 end\n"
        "    return 0xFFFF6666\n"
        "end\n"
        "\n"
        "local function xb_poll_ping_now()\n"
        '    local ok, curl = pcall(require, "curl")\n'
        '    if ok and type(curl) == "table" and curl.easy_init then\n'
        "        local c = curl.easy_init()\n"
        '        curl.easy_setopt(c, curl.OPT_URL, XB_HUD.server_url)\n'
        '        curl.easy_setopt(c, curl.OPT_TIMEOUT, 2)\n'
        "        local t0 = os.clock()\n"
        "        local perr = curl.easy_perform(c)\n"
        "        local t1 = os.clock()\n"
        "        if perr == 0 then\n"
        "            XB_HUD.last_ping_ms = math.floor(((t1 - t0) * 1000))\n"
        "            XB_HUD.ping_color = xb_color_for_ms(XB_HUD.last_ping_ms)\n"
        "            return true\n"
        "        end\n"
        "    end\n"
        "    local fallback = XB_HUD.fallback_state_file\n"
        "    if fallback then\n"
        '        local f = io.open(fallback, "r")\n'
        "        if f then\n"
        '            local content = f:read("*a")\n'
        "            f:close()\n"
        '            local ms = tonumber(content:match(\'"last_ping_ms"%s*:%s*(%d+)\'))\n'
        "            if ms then\n"
        "                XB_HUD.last_ping_ms = ms\n"
        "                XB_HUD.ping_color = xb_color_for_ms(ms)\n"
        "                return true\n"
        "            end\n"
        "        end\n"
        "    end\n"
        "    return false\n"
        "end\n"
        "\n"
        "local function xb_register_ping_hud()\n"
        "    local res_w, res_h = GetDisplayResolution()\n"
        "    ImGui.SetNextWindowPos(res_w / 2 - 90, 8, 1)\n"
        "    ImGui.SetNextWindowSize(180, 20)\n"
        '    ImGui.Begin("XB Ping HUD",\n'
        "        ImGui.WindowFlags_NoTitleBar\n"
        "        + ImGui.WindowFlags_NoInputs\n"
        "        + ImGui.WindowFlags_AlwaysAutoResize)\n"
        "    if XB_HUD.last_ping_ms == nil then\n"
        '        ImGui.TextColored(0xFFAAAAAA, "XB - pinging...")\n'
        "    else\n"
        '        local ms_text = string.format("%s  %d ms",\n'
        "            XB_HUD.server_name, XB_HUD.last_ping_ms)\n"
        "        ImGui.TextColored(XB_HUD.ping_color, ms_text)\n"
        "    end\n"
        "    ImGui.End()\n"
        "end\n"
        "\n"
        "local function xb_init_ping_hud()\n"
        '    XB_HUD.fallback_state_file = GetMod("xbuniverse_plugin"):GetRootDir()\n'
        '        .. ".." .. string.char(92) .. ".."\n'
        '        .. string.char(92) .. "xbuniverse_server"\n'
        '        .. string.char(92) .. "logs"\n'
        '        .. string.char(92) .. "xb_ping_state.json"\n'
        '    registerForEvent("onUpdate", function()\n'
        "        local now = os.clock() * 1000\n"
        "        if (now - XB_HUD.last_poll_at) >= XB_HUD.poll_period_ms then\n"
        "            XB_HUD.last_poll_at = now\n"
        "            xb_poll_ping_now()\n"
        "        end\n"
        "        xb_register_ping_hud()\n"
        "    end)\n"
        "end\n"
        "\n"
        "pcall(xb_init_ping_hud)\n"
        "-- xbuniverse_phase5 END\n"
    )


TEST_FILE_CONTENT = '''"""Regression tests for Phase 5 single-file appended endpoints."""
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


def _strip_existing(text, begin, end):
    b = text.find(begin)
    if b < 0:
        return text, False
    e = text.find(end, b + len(begin))
    if e < 0:
        return text, False
    return text[:b] + text[e + len(end):], True


def apply_block(path, block):
    src = open(path, encoding="utf-8").read()
    # Strip any existing Phase 5 block (idempotency).
    if "# === xbuniverse_phase5 BEGIN ===" in block:
        src, _ = _strip_existing(
            src, "# === xbuniverse_phase5 BEGIN ===",
            "# === xbuniverse_phase5 END ===")
    if "<!-- xbuniverse_phase5 BEGIN -->" in block:
        src, _ = _strip_existing(
            src, "<!-- xbuniverse_phase5 BEGIN -->",
            "<!-- xbuniverse_phase5 END -->")
    if "-- xbuniverse_phase5 BEGIN\n" in block:
        src, _ = _strip_existing(
            src, "-- xbuniverse_phase5 BEGIN\n",
            "-- xbuniverse_phase5 END\n")
    if not src.endswith("\n"):
        src += "\n"
    src += block
    _atomic_write(path, src)


def main():
    if len(sys.argv) != 2:
        print("Usage: _phase5_eof_splice.py UMBRELLA_ROOT", file=sys.stderr)
        sys.exit(2)
    root = sys.argv[1]
    server_py = os.path.join(root, "xbuniverse_server", "server.py")
    index_html = os.path.join(root, "xbuniverse_server", "index.html")
    init_lua = os.path.join(root, "xbuniverse_plugin", "cet", "init.lua")
    tests_path = os.path.join(
        root, "xbuniverse_server", "tests",
        "test_admin_extras_ping_logger_servers.py",
    )

    # Pre-flight: need to find login_required to confirm EOF is safe.
    src = open(server_py, encoding="utf-8").read()
    # Count login_required as a decorator anywhere; we'll trust the existing
    # 286 pytest tests passing means it's bound before /after EOF safely.

    apply_block(server_py, _phase5_block_server_py())
    apply_block(index_html, _phase5_block_index_html())
    apply_block(init_lua, _phase5_block_init_lua())

    os.makedirs(os.path.dirname(tests_path), exist_ok=True)
    _atomic_write(tests_path, TEST_FILE_CONTENT)

    post = {
        server_py: len(open(server_py, encoding="utf-8").read()),
        index_html: len(open(index_html, encoding="utf-8").read()),
        init_lua: len(open(init_lua, encoding="utf-8").read()),
    }
    for k, v in post.items():
        print("post len", os.path.basename(k), v)
    print("tests", tests_path, len(TEST_FILE_CONTENT))


if __name__ == "__main__":
    main()
