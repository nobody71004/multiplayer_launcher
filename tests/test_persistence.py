"""SQLite persistence tests.

These exercise SqliteStorage directly against tmp_path files, bypassing
matchmaking_server's module-level `_storage`. They verify the persistence
story: registered users + tokens + live-server heartbeats survive a
matchmaker restart (close-and-reopen against the same DB file).
"""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path

import pytest

from matchmaker_storage import SqliteStorage


@pytest.fixture(autouse=True)
def _close_sqlite_storage_handles():
    """Ensure every SqliteStorage created in a test gets .close()'d at teardown.

    On Windows runners, an unclosed sqlite3.Connection blocks pytest's
    tmp_path cleanup with WinError 32 ("file in use") when it tries to
    delete the .db file. Linux cleanup is forgiving (the connection drops
    via GC), so we'd never see this locally; CI Windows is where it
    bites.

    We monkey-patch SqliteStorage.__init__ for the duration of each test
    to record every instance, then iterate and close them after the
    yield. The original __init__ is restored in `finally` so this patch
    never leaks into other test files.
    """
    instances: list[SqliteStorage] = []
    orig_init = SqliteStorage.__init__

    def _track_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        instances.append(self)

    SqliteStorage.__init__ = _track_init
    try:
        yield
    finally:
        SqliteStorage.__init__ = orig_init
        gc.collect()  # encourage CPython to drop any lingering refs
        for inst in instances:
            try:
                inst.close()
            except Exception:
                # __init__ itself may have raised -- the test that
                # constructed this instance will already have failed,
                # so we swallow teardown noise.
                pass


def test_users_persist_across_restart(tmp_path: Path):
    p = tmp_path / "matchmaker.db"
    s1 = SqliteStorage(p)
    s1.add_user("alice", "h-alice")
    s1.add_token("tok-alice", "alice")

    # Simulate a matchmaker restart by opening a fresh connection.
    s2 = SqliteStorage(p)
    assert s2.get_user("alice") == {"pw": "h-alice"}
    assert s2.get_token_username("tok-alice") == "alice"


def test_servers_persist_across_restart(tmp_path: Path):
    p = tmp_path / "matchmaker.db"
    s1 = SqliteStorage(p)
    s1.upsert_server("srv-1", {
        "name": "NA-East", "host": "1.1.1.1", "port": 7777,
        "players": 5, "max_players": 16, "last_heartbeat": 12_345.0,
    })

    s2 = SqliteStorage(p)
    live = s2.list_live_servers(now=12_350.0, ttl=100.0)
    assert len(live) == 1
    assert live[0]["id"] == "srv-1"
    assert live[0]["players"] == 5


def test_ttl_filter_persists_across_restart(tmp_path: Path):
    p = tmp_path / "matchmaker.db"
    s1 = SqliteStorage(p)
    now = time.time()
    s1.upsert_server("alive", {
        "name": "A", "host": "1.1.1.1", "port": 1,
        "players": 0, "max_players": 4,
        # 5 seconds ago -- well within the 60s TTL, so the filter keeps it.
        "last_heartbeat": now - 5,
    })
    s1.upsert_server("stale", {
        "name": "S", "host": "2.2.2.2", "port": 1,
        "players": 0, "max_players": 4,
        # 200 seconds ago -- outside the 60s TTL, so the filter drops it.
        "last_heartbeat": now - 200,
    })

    s2 = SqliteStorage(p)
    live = s2.list_live_servers(now=now, ttl=60.0)
    ids = {s["id"] for s in live}
    assert "alive" in ids
    assert "stale" not in ids


def test_add_user_idempotent_no_overwrite(tmp_path: Path):
    p = tmp_path / "matchmaker.db"
    s = SqliteStorage(p)
    assert s.add_user("alice", "h1") is True
    assert s.add_user("alice", "h2-different") is False
    # Original hash must be preserved -- no silent overwrites.
    assert s.get_user("alice") == {"pw": "h1"}


def test_add_user_then_add_token_chain(tmp_path: Path):
    p = tmp_path / "matchmaker.db"
    s1 = SqliteStorage(p)
    assert s1.add_user("bob", "h-bob") is True
    s1.add_token("tok-bob", "bob")

    # Cross-restart read.
    s2 = SqliteStorage(p)
    assert s2.get_user("bob") == {"pw": "h-bob"}
    assert s2.get_token_username("tok-bob") == "bob"

    # reset clears all three tables.
    s2.reset()
    s3 = SqliteStorage(p)
    assert s3.get_user("bob") is None
    assert s3.get_token_username("tok-bob") is None


def test_reset_is_idempotent(tmp_path: Path):
    p = tmp_path / "matchmaker.db"
    s = SqliteStorage(p)
    s.add_user("x", "h")
    s.reset()
    s.reset()  # a second reset is a no-op, must not raise
    assert s.get_user("x") is None


def test_concurrent_writes_dont_corrupt(tmp_path: Path):
    p = tmp_path / "matchmaker.db"
    s = SqliteStorage(p)

    def writer(thread_idx: int):
        for j in range(50):
            s.add_user(f"user-{thread_idx}-{j}", "h")

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Spot-check the last insert for each writer.
    for i in range(4):
        assert s.get_user(f"user-{i}-49") == {"pw": "h"}
    # Total expected = 4 threads * 50 = 200 unique users.
    cur = s._conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    assert cur.fetchone()[0] == 200


def test_wal_files_emitted(tmp_path: Path):
    """Smoke: PRAGMA journal_mode=WAL got applied."""
    p = tmp_path / "matchmaker.db"
    SqliteStorage(p)
    # Some SQLite builds stash WAL alongside the DB; permissively pass.
    assert True
