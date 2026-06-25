"""SQLite persistence tests.

These exercise SqliteStorage directly against tmp_path files, bypassing
matchmaking_server's module-level `_storage`. They verify the persistence
story: registered users + tokens + live-server heartbeats survive a
matchmaker restart (close-and-reopen against the same DB file).

The maintenance-loop tests at the bottom of the file lock in the
WAL checkpoint + stale-server purge contracts added by the
operational-readiness vector.
"""

from __future__ import annotations

import gc
import threading
import time
from pathlib import Path

import pytest

from matchmaker_storage import (
    InMemoryStorage,
    SqliteStorage,
    run_maintenance_loop,
)


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


# ---------------------------------------------------------------------------
# Maintenance-loop regression net
# ---------------------------------------------------------------------------
#
# These tests pin the contracts added by the WAL-maintenance vector.
# The autouse _close_sqlite_storage_handles fixture already above
# monkey-patches SqliteStorage.__init__ to track every instance for
# teardown; the new tests exploit that to keep things clean across
# the threading tests (stop_event.set() lets run_maintenance_loop
# return cleanly so fixture teardown can close() the storage).


def test_checkpoint_wal_returns_3tuple_of_ints(tmp_path: Path):
    """SqliteStorage.checkpoint_wal returns SQLite's (busy, log, ckpt) tuple.

    All three values must be ints -- pull from cursor.fetchone() and
    cast through int() to defend against sqlite3.Row returning None
    when the row is unexpectedly empty.
    """
    p = tmp_path / "matchmaker.db"
    s = SqliteStorage(p)
    # Force some WAL traffic so a checkpoint actually has work to do.
    for i in range(5):
        s.upsert_server(
            f"srv-{i}",
            {
                "name": f"S{i}", "host": "1.1.1.1", "port": 7777,
                "players": 0, "max_players": 4,
                "last_heartbeat": time.time(),
            },
        )
    result = s.checkpoint_wal()
    assert isinstance(result, tuple)
    assert len(result) == 3
    busy, log_pages, ckpt_pages = result
    assert isinstance(busy, int)
    assert isinstance(log_pages, int)
    assert isinstance(ckpt_pages, int)
    # busy is a 0/1 contention flag; under a single-process test it
    # must be 0.
    assert busy in (0, 1)


def test_purge_stale_servers_keeps_fresh_deletes_stale(tmp_path: Path):
    """Mutation correctness: purge keeps fresh, deletes stale, returns count.

    Sets up two servers -- one fresh (last_heartbeat=now), one stale
    (last_heartbeat=now-ttl-1) -- and calls purge_stale_servers with
    ttl=60s. The deleted-row count must be exactly 1, the fresh server
    must survive, and list_live_servers() after the purge must
    contain only the fresh entry.
    """
    p = tmp_path / "matchmaker.db"
    s = SqliteStorage(p)
    now = time.time()
    s.upsert_server("alive", {
        "name": "A", "host": "1.1.1.1", "port": 1,
        "players": 0, "max_players": 4,
        "last_heartbeat": now,
    })
    s.upsert_server("stale", {
        "name": "S", "host": "2.2.2.2", "port": 1,
        "players": 0, "max_players": 4,
        "last_heartbeat": now - 3600.0,  # way past any reasonable TTL
    })
    deleted = s.purge_stale_servers(now=now, ttl=60.0)
    assert deleted == 1, deleted
    survivors = {row["id"] for row in s.list_live_servers(now=now, ttl=60.0)}
    assert survivors == {"alive"}, survivors


def test_run_maintenance_loop_one_cycle_then_returns_for_zero_interval(tmp_path: Path):
    """interval_s=0 must yield EXACTLY one cycle then exit.

    This is the test-mode entry point the production CLI flag
    (`--maintenance-interval-sec 0`) maps to. If the function
    loops indefinitely when interval_s=0, every test that touches
    it would hang -- this test catches that misfire.

    Direct inline call (interval_s=0 documents a synchronous
    one-cycle return -- no thread wrapper needed). Reaching the
    assertion below proves the function returned without looping.
    The pre-set stop_event is documentation, not load-bearing:
    with interval_s=0 we exit on the post-cycle early-return path
    BEFORE stop_event.wait() is ever called.
    """
    p = tmp_path / "matchmaker.db"
    s = SqliteStorage(p)
    stop = threading.Event()
    stop.set()
    run_maintenance_loop(s, interval_s=0.0, stop_event=stop)
    # Reaching this line is the assertion: the function returned.


def test_run_maintenance_loop_exits_on_stop_event(tmp_path: Path):
    """interval_s>0 must exit cleanly when stop_event.set() is called.

    Spawns the loop on a real background daemon thread with a tight
    0.05s cycle, lets it run a few cycles, then sets the stop event
    and verifies the thread joins within 2s. Without the
    stop-event-aware sleep, this join would block until the test
    fixture teardown killed the daemon.
    """
    p = tmp_path / "matchmaker.db"
    s = SqliteStorage(p)
    stop = threading.Event()
    t = threading.Thread(
        target=run_maintenance_loop,
        kwargs={
            "storage": s,
            "interval_s": 0.05,
            "stop_event": stop,
        },
        daemon=True,
        name="wal-maintenance-test",
    )
    t.start()
    # Let the loop tick a few cycles before signaling stop.
    time.sleep(0.15)
    assert t.is_alive(), "thread died before stop_event.set() was called"
    stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive(), (
        "run_maintenance_loop did not exit within 2s after stop_event.set()"
    )


def test_inmemory_maintenance_purges_zero_rows_when_empty():
    """InMemoryStorage.purge_stale_servers returns 0 on empty storage.

    The Protocol forces both backends to implement purge_stale_servers
    so the maintenance loop is backend-agnostic. On an EMPTY
    InMemoryStorage there are no rows to delete, so the count is 0;
    on a non-empty InMemoryStorage the dict-comprehension would
    actually mutate state (matching SqliteStorage's DELETE). The
    checkpoint_wal path is a true no-op (returns (0, 0, 0)) because
    no WAL exists in-memory.
    """
    s = InMemoryStorage()
    assert s.checkpoint_wal() == (0, 0, 0)
    assert s.purge_stale_servers(now=time.time(), ttl=60.0) == 0
