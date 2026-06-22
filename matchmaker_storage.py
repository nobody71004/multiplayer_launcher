"""Storage backends for the matchmaker.

Two implementations of the same Protocol:

  - InMemoryStorage  -- fast, RLock-guarded; what tests use (forced via
                        the MATCHMAKER_USE_INMEMORY=1 env var set in
                        tests/conftest.py).
  - SqliteStorage    -- SQLite-backed, persists across matchmaker restarts;
                        default for production.

Default selection at module-import time of matchmaking_server:

  - MATCHMAKER_USE_INMEMORY=1  -> InMemoryStorage()
  - any other / unset          -> SqliteStorage at $MATCHMAKER_DB
                                   or ./matchmaker.db if MATCHMAKER_DB unset

The Storage Protocol is structural (typing.Protocol), so concrete
implementations don't need to inherit. Thread-safety is each impl's
responsibility -- both wrap operations in a per-instance RLock so Flask's
threaded request handlers are safe.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol


class Storage(Protocol):
    """Minimal contract the matchmaker handlers rely on."""

    def reset(self) -> None: ...
    def add_user(self, username: str, pw_hash: str) -> bool: ...
    def get_user(self, username: str) -> Optional[Dict[str, str]]: ...
    def add_token(self, token: str, username: str) -> None: ...
    def get_token_username(self, token: str) -> Optional[str]: ...
    def upsert_server(self, server_id: str, meta: Dict[str, Any]) -> None: ...
    def list_live_servers(self, now: float, ttl: float) -> List[Dict[str, Any]]: ...
    def close(self) -> None:
        """Release any underlying resources (file handles, sockets).

        SqliteStorage closes its sqlite3 connection so pytest's tmp_path
        teardown on Windows runners doesn't hit WinError 32; InMemoryStorage
        defines this as a no-op so callers can use a single teardown path
        without backend-specific branching.
        """
        ...


# ---------------------------------------------------------------------------
# In-memory backend (used by tests)
# ---------------------------------------------------------------------------


class InMemoryStorage:
    """Pure-Python, RLock-guarded. State is lost on process exit."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._users: Dict[str, Dict[str, str]] = {}
        self._tokens: Dict[str, str] = {}
        self._servers: Dict[str, Dict[str, Any]] = {}

    def reset(self) -> None:
        with self._lock:
            self._users.clear()
            self._tokens.clear()
            self._servers.clear()

    def add_user(self, username: str, pw_hash: str) -> bool:
        with self._lock:
            if username in self._users:
                return False
            self._users[username] = {"pw": pw_hash}
            return True

    def get_user(self, username: str) -> Optional[Dict[str, str]]:
        with self._lock:
            u = self._users.get(username)
            return dict(u) if u else None

    def add_token(self, token: str, username: str) -> None:
        with self._lock:
            self._tokens[token] = username

    def get_token_username(self, token: str) -> Optional[str]:
        with self._lock:
            return self._tokens.get(token)

    def upsert_server(self, server_id: str, meta: Dict[str, Any]) -> None:
        with self._lock:
            self._servers[server_id] = dict(meta)

    def list_live_servers(self, now: float, ttl: float) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "id": sid,
                    "name": meta.get("name", sid),
                    "host": meta.get("host", "127.0.0.1"),
                    "port": int(meta.get("port", 7777)),
                    "players": int(meta.get("players", 0)),
                    "max_players": int(meta.get("max_players", 16)),
                    "last_heartbeat": float(meta.get("last_heartbeat", now)),
                }
                for sid, meta in self._servers.items()
                if now - float(meta.get("last_heartbeat", 0)) < ttl
            ]

    def close(self) -> None:
        # InMemoryStorage has no OS handles to release. close() exists for
        # symmetry with SqliteStorage so callers can use a single teardown
        # path (see tests/test_persistence.py autouse fixture).
        return None


# ---------------------------------------------------------------------------
# SQLite backend (persistence)
# ---------------------------------------------------------------------------


class SqliteStorage:
    """SQLite-backed, survives matchmaker restarts. Thread-safe via RLock.

    PRAGMA journal_mode=WAL gives concurrent readers + a single writer,
    which matches our write-light / read-medium workload.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Flask dev server runs handlers in worker threads; we wrap each op
        # with the per-instance RLock instead of relying on sqlite3's
        # connection mutex alone.
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock, self._conn:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    pw_hash  TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    token    TEXT PRIMARY KEY,
                    username TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS servers (
                    server_id      TEXT PRIMARY KEY,
                    name           TEXT NOT NULL,
                    host           TEXT NOT NULL,
                    port           INTEGER NOT NULL,
                    players        INTEGER NOT NULL,
                    max_players    INTEGER NOT NULL,
                    last_heartbeat REAL    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tokens_username
                    ON tokens(username);
                CREATE INDEX IF NOT EXISTS idx_servers_heartbeat
                    ON servers(last_heartbeat);
                """
            )

    # --- public Storage API -----------------------------------------------

    def reset(self) -> None:
        # Order matches Flask-handler semantics (users -> tokens -> servers)
        # so a hypothetical reader of /api/servers after reset() doesn't see
        # tokens for users that no longer exist.
        with self._lock, self._conn:
            for table in ("users", "tokens", "servers"):
                self._conn.execute(f"DELETE FROM {table}")

    def add_user(self, username: str, pw_hash: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.cursor()
            # SELECT-then-INSERT is race-free within a single process because
            # the RLock serializes add_user. Across processes sqlite's own
            # connection locking plus the PK constraint protects us.
            cur.execute("SELECT 1 FROM users WHERE username = ?", (username,))
            if cur.fetchone():
                return False
            self._conn.execute(
                "INSERT INTO users(username, pw_hash) VALUES (?, ?)",
                (username, pw_hash),
            )
            return True

    def get_user(self, username: str) -> Optional[Dict[str, str]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT pw_hash FROM users WHERE username = ?", (username,))
            row = cur.fetchone()
            return {"pw": row["pw_hash"]} if row else None

    def add_token(self, token: str, username: str) -> None:
        with self._lock, self._conn:
            # Plain INSERT (not OR IGNORE): token collisions would indicate a
            # cryptographic failure of secrets.token_urlsafe and we want it
            # loud, not silently dropped. With 24 random bytes P(collision)
            # is ~2^-192 for any pair.
            self._conn.execute(
                "INSERT INTO tokens(token, username) VALUES (?, ?)",
                (token, username),
            )

    def get_token_username(self, token: str) -> Optional[str]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("SELECT username FROM tokens WHERE token = ?", (token,))
            row = cur.fetchone()
            return row["username"] if row else None

    def upsert_server(self, server_id: str, meta: Dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO servers(
                       server_id, name, host, port, players, max_players, last_heartbeat
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    server_id,
                    str(meta.get("name", server_id)),
                    str(meta.get("host", "127.0.0.1")),
                    int(meta.get("port", 7777)),
                    int(meta.get("players", 0)),
                    int(meta.get("max_players", 16)),
                    float(meta.get("last_heartbeat", time.time())),
                ),
            )

    def list_live_servers(self, now: float, ttl: float) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT * FROM servers WHERE last_heartbeat >= ?",
                (now - ttl,),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": row["server_id"],
                    "name": row["name"],
                    "host": row["host"],
                    "port": row["port"],
                    "players": row["players"],
                    "max_players": row["max_players"],
                    "last_heartbeat": row["last_heartbeat"],
                }
                for row in rows
            ]

    def close(self) -> None:
        # sqlite3.Connection.close() is idempotent (safe to call twice --
        # the second call is a no-op). On Windows runners, leaving the
        # connection open blocks pytest's tmp_path teardown with WinError
        # 32 ("file in use") when it tries to delete the .db file. Linux
        # cleanup handles this gracefully via GC so the leak is harmless
        # there, but we close unconditionally so the cross-platform
        # behaviour is identical.
        self._conn.close()


# ---------------------------------------------------------------------------
# Default selection
# ---------------------------------------------------------------------------


def default_storage() -> Storage:
    """Pick the backend the matchmaker should boot with.

    Env-var contract:
      MATCHMAKER_USE_INMEMORY=1   -> InMemoryStorage (used by tests)
      MATCHMAKER_DB=<path>        -> SqliteStorage at <path>
                                     (default: ./matchmaker.db)
    """
    if os.environ.get("MATCHMAKER_USE_INMEMORY") == "1":
        return InMemoryStorage()
    db_path = Path(os.environ.get("MATCHMAKER_DB", "matchmaker.db"))
    return SqliteStorage(db_path)
