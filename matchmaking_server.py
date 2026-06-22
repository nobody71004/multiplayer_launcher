"""Matchmaking REST server.

In-memory state, thread-locked. Exposes `create_app()` so tests can mount the
Flask test_client in-process. Run as a module for the actual server:

    python matchmaking_server.py --host 127.0.0.1 --port 5000

Endpoints:
    GET  /api/health         -> {ok, ts}
    POST /api/register       -> 201 / 400 / 409 (user/pw validation)
    POST /api/login          -> 200 / 401 (token issued)
    GET  /api/servers        -> 200 (live servers filtered by < TTL)
    POST /api/heartbeat      -> 200 / 400 / 401 (token-authed keepalive)
"""

from __future__ import annotations

import argparse
import hashlib
import secrets
import sys
import threading
import time
from typing import Any, Dict

from flask import Flask, jsonify, request


# --- module-level state ------------------------------------------------------

_lock = threading.RLock()
_users: Dict[str, Dict[str, str]] = {}     # username -> {"pw": <hash>}
_tokens: Dict[str, str] = {}                # token -> username
_servers: Dict[str, Dict[str, Any]] = {}     # server_id -> metadata

SERVER_TTL_SEC = 60.0  # heartbeat older than this is filtered from /api/servers


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _issue_token(username: str) -> str:
    tok = secrets.token_urlsafe(24)
    with _lock:
        _tokens[tok] = username
    return tok


def reset_state() -> None:
    """Test helper: wipe all in-memory state."""
    with _lock:
        _users.clear()
        _tokens.clear()
        _servers.clear()


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
        with _lock:
            if username in _users:
                return jsonify({"error": "user exists"}), 409
            _users[username] = {"pw": _hash(password)}
        return jsonify({"ok": True, "username": username}), 201

    @app.post("/api/login")
    def login():
        body = request.get_json(silent=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        with _lock:
            user = _users.get(username)
            if not user or user["pw"] != _hash(password):
                return jsonify({"error": "bad credentials"}), 401
            tok = _issue_token(username)
        return jsonify({"token": tok, "username": username})

    @app.get("/api/servers")
    def list_servers():
        now = time.time()
        with _lock:
            live = [
                {
                    "id": sid,
                    "name": meta.get("name", sid),
                    "host": meta.get("host", "127.0.0.1"),
                    "port": int(meta.get("port", 7777)),
                    "players": int(meta.get("players", 0)),
                    "max_players": int(meta.get("max_players", 16)),
                    "last_heartbeat": meta.get("last_heartbeat", now),
                }
                for sid, meta in _servers.items()
                if now - meta.get("last_heartbeat", 0) < SERVER_TTL_SEC
            ]
        live.sort(key=lambda s: s["players"], reverse=True)
        return jsonify({"servers": live})

    @app.post("/api/heartbeat")
    def heartbeat():
        body = request.get_json(silent=True) or {}
        token = (body.get("token") or "").strip()
        with _lock:
            if token not in _tokens:
                return jsonify({"error": "invalid token"}), 401
            sid = (body.get("server_id") or "").strip()
            if not sid:
                return jsonify({"error": "server_id required"}), 400
            _servers[sid] = {
                "name": body.get("name", sid),
                "host": body.get("host", "127.0.0.1"),
                "port": int(body.get("port", 7777)),
                "players": int(body.get("players", 0)),
                "max_players": int(body.get("max_players", 16)),
                "last_heartbeat": time.time(),
            }
        return jsonify({"ok": True})

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "not found"}), 404

    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    args = ap.parse_args(argv)
    create_app().run(host=args.host, port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
