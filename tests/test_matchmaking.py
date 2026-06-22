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

    # Register alive + stale via the Storage indirection (the prior `_lock`+
    # `_servers` module globals are gone -- routes through `_storage` now).
    import matchmaking_server as ms
    now = time.time()
    ms._storage.upsert_server("alive", {"name": "A", "host": "1.1.1.1", "port": 1,
                                        "players": 1, "max_players": 4,
                                        "last_heartbeat": now})
    ms._storage.upsert_server("stale", {"name": "S", "host": "2.2.2.2", "port": 1,
                                        "players": 1, "max_players": 4,
                                        "last_heartbeat": now - SERVER_TTL_SEC - 5})
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


def test_heartbeat_then_visible_end_to_end(tmp_path: Path):
    """HTTP-level cross-restart persistence over the Flask surface.

    Round 1 -- register a user, login, and POST /api/heartbeat through
    the Flask `test_client` only (never touches `matchmaker_storage`
    directly). The /api/heartbeat route serializes the write through
    `matchmaking_server._storage.upsert_server(...)`, which is the
    Storage Protocol indirection this test enforces.

    Round 2 -- simulate a matchmaker restart by closing s1 (state
    flushed), opening a FRESH `SqliteStorage(s1.path)` against the
    SAME on-disk DB, and swapping it into `matchmaking_server._storage`
    via the existing `use_storage(...)` test helper. The fresh
    connection's `get_token_username(...)` and `list_live_servers(...)`
    re-hydrate the round-1 state from disk.

    Asserts after the restart:
      - the round-1 token still authenticates the second heartbeat
        (proves the round-1 user + token survived on disk),
      - /api/servers returns exactly 1 server with the same id
        (proves the second heartbeat went through UPSERT, not a
        side-channel that would have produced a duplicate row).

    Catches any future regression where the Storage indirection is
    bypassed -- e.g., a /api/heartbeat handler that writes to a
    module-level dict instead of `_storage.upsert_server(...)`. Such
    a regression surfaces here as either a 401 second heartbeat
    (token/user lost) or a duplicate-row count > 1 (sibling write).
    Per-test `use_storage(...)` swap + restore in `finally` keeps
    test isolation intact for the rest of the suite.
    """
    import matchmaking_server as ms
    from matchmaker_storage import SqliteStorage

    db_path = tmp_path / "matchmaker.db"
    server_id = "srv-end-to-end"

    # ---------- round 1: fresh matchmaker boot ----------
    s1 = SqliteStorage(db_path)
    prior_s1 = ms.use_storage(s1)
    try:
        # The fresh DB has empty tables; reset_state() is an idempotent
        # no-op here but guards against leftover rows if tmp_path
        # is somehow shared / pre-populated in the future.
        ms.reset_state()
        app1 = ms.create_app()
        app1.config["TESTING"] = True
        with app1.test_client() as c1:
            r = c1.post(
                "/api/register",
                json={"username": "alice", "password": "hunter2"},
            )
            assert r.status_code == 201, r.get_json()

            token = c1.post(
                "/api/login",
                json={"username": "alice", "password": "hunter2"},
            ).get_json()["token"]

            hb1 = c1.post("/api/heartbeat", json={
                "token": token,
                "server_id": server_id,
                "name": "End-to-End",
                "host": "127.0.0.1", "port": 7777,
                "players": 3, "max_players": 16,
            })
            assert hb1.status_code == 200, hb1.get_json()

            srvs = c1.get("/api/servers").get_json()["servers"]
            assert len(srvs) == 1, f"round-1 visibility: {srvs}"
            assert srvs[0]["id"] == server_id, f"round-1 server id: {srvs}"
    finally:
        s1.close()
        # Restore the InMemoryStorage baseline so the autouse
        # post-yield `_reset` lands on the suite's default backend.
        ms.use_storage(prior_s1)

    # ---------- round 2: simulated matchmaker restart ----------
    # NOTE: do NOT call reset_state() here. The whole point is that
    # the round-1 user + token + server SURVIVE the restart. Closing
    # s1 (FLUSHED via close()) and opening a fresh SqliteStorage
    # against the same file reads the round-1 writes back from disk.
    s2 = SqliteStorage(db_path)
    prior_s2 = ms.use_storage(s2)
    try:
        app2 = ms.create_app()
        app2.config["TESTING"] = True
        with app2.test_client() as c2:
            hb2 = c2.post("/api/heartbeat", json={
                "token": token,            # same token from round 1
                "server_id": server_id,    # UPSERT via Storage Protocol
                "name": "End-to-End",
                "host": "127.0.0.1", "port": 7777,
                "players": 5, "max_players": 16,
            })
            assert hb2.status_code == 200, (
                f"second heartbeat must succeed against s2 (token + "
                f"user persisted from round 1); got {hb2.status_code} "
                f"{hb2.get_json()}"
            )

            srvs = c2.get("/api/servers").get_json()["servers"]
            assert len(srvs) == 1, (
                f"post-restart count must be 1 (UPSERT, not INSERT); "
                f"got {srvs}"
            )
            assert srvs[0]["id"] == server_id, (
                f"post-restart server id must equal round-1 id; "
                f"got {srvs}"
            )
    finally:
        s2.close()
        ms.use_storage(prior_s2)
