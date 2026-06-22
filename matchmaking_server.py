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
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, request

from matchmaker_storage import (
    SqliteStorage,
    Storage,
    default_storage,
)


# --- module-level state ------------------------------------------------------

SERVER_TTL_SEC = 60.0

# Lazy-resolved at first import. tests/conftest.py sets
# MATCHMAKER_USE_INMEMORY=1 BEFORE this module is loaded so test runs get
# InMemoryStorage; production callers get SqliteStorage at ./matchmaker.db.
_storage: Storage = default_storage()


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


# --- app factory -------------------------------------------------------------


def create_app() -> Flask:
    app = Flask(__name__)

    @app.get("/api/health")
    def health():
        return jsonify({"ok": True, "ts": int(time.time())})

    @app.post("/api/register")
    def register():
        body = request.get_json(silent=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if len(username) < 3 or len(password) < 4:
            return jsonify({"error": "invalid credentials"}), 400
        if not _storage.add_user(username, _hash(password)):
            return jsonify({"error": "user exists"}), 409
        return jsonify({"ok": True, "username": username}), 201

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        user = _storage.get_user(username)
        if not user or user["pw"] != _hash(password):
            return jsonify({"error": "bad credentials"}), 401
        tok = _issue_token(username)
        return jsonify({"token": tok, "username": username})

    @app.get("/api/servers")
    def list_servers():
        live = _storage.list_live_servers(time.time(), SERVER_TTL_SEC)
        live.sort(key=lambda s: s["players"], reverse=True)
        return jsonify({"servers": live})

    @app.post("/api/heartbeat")
    def heartbeat():
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        if _storage.get_token_username(token) is None:
            return jsonify({"error": "invalid token"}), 401
        sid = (body.get("server_id") or "").strip()
        if not sid:
            return jsonify({"error": "server_id required"}), 400
        _storage.upsert_server(sid, {
            "name": body.get("name", sid),
            "host": body.get("host", "127.0.0.1"),
            "port": body.get("port", 7777),
            "players": body.get("players", 0),
            "max_players": body.get("max_players", 16),
            "last_heartbeat": time.time(),
        })
        return jsonify({"ok": True})

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
    args = ap.parse_args(argv)
    if args.db is not None and os.environ.get("MATCHMAKER_USE_INMEMORY") != "1":
        global _storage
        _storage = SqliteStorage(Path(args.db))
    create_app().run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
