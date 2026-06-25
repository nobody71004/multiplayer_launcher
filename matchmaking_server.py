"""Matchmaking REST server.

State is pluggable via matchmaker_storage.default_storage(). See that module
for the env-var contract; the brief version:

  MATCHMAKER_USE_INMEMORY=1   -> InMemoryStorage  (used by tests via conftest.py)
  MATCHMAKER_DB=<path>        -> SqliteStorage at <path>
                                  (default: ./matchmaker.db)

Exposes `create_app()` so tests can mount the Flask test_client in-process:

    python matchmaking_server.py --host 127.0.0.1 --port 5000
    python matchmaking_server.py --db /custom/path.db --port 5000

Endpoints:
    GET  /api/health         -> {ok, ts}
    POST /api/register       -> 201 / 400 / 409 (validation)
    POST /api/login          -> 200 / 401 (token)
    GET  /api/servers        -> 200 (live servers filtered by < TTL)
    POST /api/heartbeat      -> 200 / 400 / 401 (token-authed keepalive)
    GET  /metrics            -> Prometheus text-format exposition
    GET  /api/logs           -> JSONL tail of the in-process event log
    GET  /admin/             -> HTML log viewer (auto-poll every 2s)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, g, jsonify, render_template, request

from matchmaker_storage import (
    SqliteStorage,
    Storage,
    default_storage,
    run_maintenance_loop,
)
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


# --- module-level state ------------------------------------------------------

SERVER_TTL_SEC = 60.0

# Lazy-resolved at first import. tests/conftest.py sets
# MATCHMAKER_USE_INMEMORY=1 BEFORE this module is loaded so test runs get
# InMemoryStorage; production callers get SqliteStorage at ./matchmaker.db.
_storage: Storage = default_storage()


# --- Prometheus instrumentation ---------------------------------------------

REGISTRATIONS_TOTAL = Counter(
    "matchmaker_registrations_total",
    "Number of /api/register requests partitioned by outcome",
    ["result"],
)
LOGINS_TOTAL = Counter(
    "matchmaker_logins_total",
    "Number of /api/login requests partitioned by outcome",
    ["result"],
)
HEARTBEATS_TOTAL = Counter(
    "matchmaker_heartbeats_total",
    "Number of /api/heartbeat requests partitioned by outcome",
    ["result"],
)
ACTIVE_SERVERS_COUNT = Gauge(
    "matchmaker_active_servers_count",
    "Number of servers passing the live-TTL filter on the last /api/servers hit",
)
REQUEST_LATENCY_SECONDS = Histogram(
    "matchmaker_request_latency_seconds",
    "End-to-end request duration partitioned by Flask endpoint",
    ["endpoint"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)
MAINTENANCE_PURGED_TOTAL = Counter(
    "matchmaker_maintenance_purged_total",
    "Total legacy server rows deleted by the background maintenance loop",
)


# --- JSONL event log (logs/matchmaker/<pid>.jsonl) --------------------------

LOG_DIR = Path(__file__).resolve().parent / "logs" / "matchmaker"
LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_PATH = LOG_DIR / f"{os.getpid()}.jsonl"
_LOG_LOCK = threading.Lock()


def _log_event(level: str, message: str, **extra: object) -> None:
    """Append a single JSONL event to the per-PID log file.

    Hand-rolled (not stdlib logging) to avoid root-logger side-effect
    baggage. Thread-safe: every write acquires ``_LOG_LOCK`` and
    opens the file inside the with-block (each call writes a single
    short line << PIPE_BUF so no interleave risk on Windows).
    """
    # Millisecond-precision ISO8601 -- lexicographic == issue order, so
    # /api/logs?since=<ts> filters never hit the same-second tie race
    # that bare-second strftime() would produce under burst events.
    rec: dict = {
        "ts": (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        ),
        "level": level,
        "message": message,
    }
    rec.update(extra)
    line = json.dumps(rec, separators=(",", ":")) + "\n"
    with _LOG_LOCK:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)


def _read_recent_events(limit: int, since_ts: str | None) -> list:
    """Return the last ``limit`` events from this process's log file.

    Filters out lines whose ``ts`` is <= ``since_ts`` -- strict-greater
    on the request side, so a poller advancing past its last-seen
    timestamp never re-emits the boundary event (lexicographic
    comparison works for ISO8601 because _log_event emits a
    millisecond-precision string).
    Reads the whole file then trims -- adequate because the per-PID
    file is bounded by this process's uptime.
    """
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            raw = f.read().splitlines()
    except FileNotFoundError:
        return []
    out: list = []
    for line in raw[-limit * 4:]:  # over-read then trim
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Strict-greater (`<=`) advances the poller cleanly past the
        # boundary second without re-emitting same-second siblings.
        rec_ts = rec.get("ts")
        if (
            since_ts
            and isinstance(rec_ts, str)
            and isinstance(since_ts, str)
            and rec_ts <= since_ts
        ):
            continue
        out.append(rec)
    return out[-limit:]


# --- helpers ----------------------------------------------------------------

def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _issue_token(username: str) -> str:
    tok = secrets.token_urlsafe(24)
    _storage.add_token(tok, username)
    return tok


def reset_state() -> None:
    """Test helper: wipe the active backend (works for InMemoryStorage and
    SqliteStorage alike -- this is the migration guard)."""
    _storage.reset()


def use_storage(storage: Storage) -> Storage:
    """Test helper: swap the backend at runtime. Returns the previous backend
    so callers can restore it."""
    global _storage
    prior = _storage
    _storage = storage
    return prior


# --- app factory ------------------------------------------------------------


def create_app() -> Flask:
    app = Flask(__name__)

    @app.before_request
    def _start_timer():
        g._mm_start = time.time()

    @app.after_request
    def _observe_latency(resp):
        start = getattr(g, "_mm_start", None)
        if start is not None and request.endpoint:
            REQUEST_LATENCY_SECONDS.labels(endpoint=request.endpoint).observe(
                time.time() - start,
            )
        return resp

    @app.get("/api/health")
    def health():
        # Don't pass ts= explicitly -- _log_event already stamps the
        # string ISO8601 ts; passing an int kwarg would overwrite it
        # via rec.update(extra) and break the schema.
        _log_event("info", "health_check")
        return jsonify({"ok": True, "ts": int(time.time())})

    @app.post("/api/register")
    def register():
        body = request.get_json(silent=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if len(username) < 3 or len(password) < 4:
            REGISTRATIONS_TOTAL.labels(result="invalid").inc()
            _log_event(
                "warn", "registration_rejected_invalid",
                username=username,
            )
            return jsonify({"error": "invalid credentials"}), 400
        if not _storage.add_user(username, _hash(password)):
            REGISTRATIONS_TOTAL.labels(result="duplicate").inc()
            _log_event(
                "info", "registration_rejected_duplicate",
                username=username,
            )
            return jsonify({"error": "user exists"}), 409
        REGISTRATIONS_TOTAL.labels(result="created").inc()
        _log_event("info", "registration_ok", username=username)
        return jsonify({"ok": True, "username": username}), 201

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        user = _storage.get_user(username)
        if not user:
            LOGINS_TOTAL.labels(result="unknown_user").inc()
            _log_event("warn", "login_unknown_user", username=username)
            return jsonify({"error": "bad credentials"}), 401
        if user["pw"] != _hash(password):
            LOGINS_TOTAL.labels(result="bad_pw").inc()
            _log_event("warn", "login_bad_pw", username=username)
            return jsonify({"error": "bad credentials"}), 401
        tok = _issue_token(username)
        LOGINS_TOTAL.labels(result="ok").inc()
        _log_event("info", "login_ok", username=username)
        return jsonify({"token": tok, "username": username})

    @app.get("/api/servers")
    def list_servers():
        live = _storage.list_live_servers(time.time(), SERVER_TTL_SEC)
        live.sort(key=lambda s: s["players"], reverse=True)
        ACTIVE_SERVERS_COUNT.set(len(live))
        return jsonify({"servers": live})

    @app.post("/api/heartbeat")
    def heartbeat():
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        if _storage.get_token_username(token) is None:
            HEARTBEATS_TOTAL.labels(result="invalid_token").inc()
            _log_event("warn", "heartbeat_invalid_token")
            return jsonify({"error": "invalid token"}), 401
        sid = (body.get("server_id") or "").strip()
        if not sid:
            HEARTBEATS_TOTAL.labels(result="server_id_required").inc()
            _log_event("warn", "heartbeat_missing_server_id")
            return jsonify({"error": "server_id required"}), 400
        _storage.upsert_server(sid, {
            "name": body.get("name", sid),
            "host": body.get("host", "127.0.0.1"),
            "port": body.get("port", 7777),
            "players": body.get("players", 0),
            "max_players": body.get("max_players", 16),
            "last_heartbeat": time.time(),
        })
        HEARTBEATS_TOTAL.labels(result="ok").inc()
        _log_event("info", "heartbeat_ok", server_id=sid)
        return jsonify({"ok": True})

    @app.get("/metrics")
    def metrics():
        body = generate_latest()
        return body, 200, {"Content-Type": CONTENT_TYPE_LATEST}

    @app.get("/api/logs")
    def logs():
        try:
            limit = int(request.args.get("limit", "200"))
        except ValueError:
            limit = 200
        since = request.args.get("since") or None
        events = _read_recent_events(limit=min(limit, 1000), since_ts=since)
        return jsonify({"events": events})

    @app.get("/admin/")
    @app.get("/admin")
    def admin_view():
        return render_template("admin/index.html")

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "not found"}), 404

    return app


# --- CLI bootstrap -----------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument(
        "--db", default=None,
        help="path to SQLite DB (ignored when MATCHMAKER_USE_INMEMORY=1)",
    )
    ap.add_argument(
        "--maintenance-interval-sec",
        type=float,
        default=300.0,
        help=(
            "Interval (in seconds) at which a background daemon thread "
            "runs PRAGMA wal_checkpoint(TRUNCATE) and DELETEs stale "
            "server rows (last_heartbeat older than 5 minutes). "
            "Default: 300. Set to 0 to disable the loop entirely "
            "(useful for one-shot scripts and tests)."
        ),
    )
    args = ap.parse_args(argv)
    if args.db is not None and os.environ.get("MATCHMAKER_USE_INMEMORY") != "1":
        global _storage
        _storage = SqliteStorage(Path(args.db))

    # Background WAL maintenance loop. Lifecycle:
    #   - interval > 0: daemon thread runs run_maintenance_loop forever.
    #     on_purge observer increments MAINTENANCE_PURGED_TOTAL so /metrics
    #     surfaces the work without re-querying the storage layer.
    #   - interval <= 0: loop disabled (test / one-shot mode).
    if args.maintenance_interval_sec > 0:
        stop_event = threading.Event()
        maintenance_thread = threading.Thread(
            target=run_maintenance_loop,
            kwargs={
                "storage": _storage,
                "interval_s": args.maintenance_interval_sec,
                "stop_event": stop_event,
                "on_purge": MAINTENANCE_PURGED_TOTAL.inc,
            },
            daemon=True,
            name="wal-maintenance",
        )
        maintenance_thread.start()
        sys.stderr.write(
            f"[maintenance] started daemon thread (interval="
            f"{args.maintenance_interval_sec}s)\n"
        )
        sys.stderr.flush()
        _log_event(
            "info", "maintenance_started",
            interval_s=args.maintenance_interval_sec,
        )

    create_app().run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
