"""Flask matchmaker endpoint contract tests.

Covers:
  - /api/health
  - /api/register  (validation, dedupe)
  - /api/login     (good + bad credentials)
  - /api/heartbeat (token-auth, missing server_id)
  - /api/servers   (TTL filtering of stale heartbeats)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from matchmaking_server import (  # noqa: E402
    SERVER_TTL_SEC, create_app, reset_state,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_state()
    yield


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert "ts" in body


def test_register_new_user(client):
    r = client.post("/api/register", json={"username": "alice", "password": "pw1234"})
    assert r.status_code == 201
    assert r.get_json()["ok"] is True


def test_register_validates_inputs(client):
    for bad in [{}, {"username": "x", "password": "pw1234"}, {"username": "alice", "password": "x"}]:
        assert client.post("/api/register", json=bad).status_code == 400


def test_register_dup_409(client):
    assert client.post("/api/register", json={"username": "bob", "password": "pw1234"}).status_code == 201
    r = client.post("/api/register", json={"username": "bob", "password": "pw1234"})
    assert r.status_code == 409


def test_login_good_returns_token(client):
    client.post("/api/register", json={"username": "carol", "password": "pw1234"})
    tok = client.post("/api/login", json={"username": "carol", "password": "pw1234"}).get_json()["token"]
    assert isinstance(tok, str) and len(tok) > 8


def test_login_bad_pw_is_401(client):
    client.post("/api/register", json={"username": "dan", "password": "pw1234"})
    assert client.post("/api/login", json={"username": "dan", "password": "nope"}).status_code == 401


def test_login_unknown_user_is_401(client):
    assert client.post("/api/login", json={"username": "nope", "password": "nope"}).status_code == 401


def test_heartbeat_requires_token(client):
    assert client.post("/api/heartbeat", json={"server_id": "s1"}).status_code == 401


def test_heartbeat_requires_server_id(client):
    client.post("/api/register", json={"username": "host", "password": "pw1234"})
    tok = client.post("/api/login", json={"username": "host", "password": "pw1234"}).get_json()["token"]
    assert client.post("/api/heartbeat", json={"token": tok}).status_code == 400


def test_heartbeat_then_visible_in_servers(client):
    client.post("/api/register", json={"username": "host", "password": "pw1234"})
    tok = client.post("/api/login", json={"username": "host", "password": "pw1234"}).get_json()["token"]
    r = client.post("/api/heartbeat", json={
        "token": tok, "server_id": "srv-1", "name": "NA-East",
        "host": "38.0.0.1", "port": 7777, "players": 5, "max_players": 16,
    })
    assert r.status_code == 200, r.get_json()
    srvs = client.get("/api/servers").get_json()["servers"]
    assert any(s["id"] == "srv-1" and s["players"] == 5 for s in srvs), srvs


def test_servers_filters_stale_heartbeats(client, monkeypatch):
    # Login first.
    client.post("/api/register", json={"username": "host", "password": "pw1234"})
    tok = client.post("/api/login", json={"username": "host", "password": "pw1234"}).get_json()["token"]

    # Register alive + stale via direct state poke.
    import matchmaking_server as ms
    with ms._lock:
        now = time.time()
        ms._servers["alive"] = {"name": "A", "host": "1.1.1.1", "port": 1, "players": 1,
                                "max_players": 4, "last_heartbeat": now}
        ms._servers["stale"] = {"name": "S", "host": "2.2.2.2", "port": 1, "players": 1,
                                "max_players": 4,
                                "last_heartbeat": now - SERVER_TTL_SEC - 5}
    srvs = client.get("/api/servers").get_json()["servers"]
    ids = [s["id"] for s in srvs]
    assert "alive" in ids
    assert "stale" not in ids


def test_servers_sorted_by_player_count_desc(client):
    client.post("/api/register", json={"username": "host", "password": "pw1234"})
    tok = client.post("/api/login", json={"username": "host", "password": "pw1234"}).get_json()["token"]
    for sid, players in [("a", 2), ("b", 8), ("c", 5)]:
        client.post("/api/heartbeat", json={
            "token": tok, "server_id": sid, "players": players,
        })
    srvs = client.get("/api/servers").get_json()["servers"]
    counts = [s["players"] for s in srvs]
    assert counts == sorted(counts, reverse=True), counts


def test_unknown_route_returns_404_json(client):
    r = client.get("/nope")
    assert r.status_code == 404
    assert r.get_json() == {"error": "not found"}
