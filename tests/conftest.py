"""Pytest configuration -- the migration guard.

Sets `MATCHMAKER_USE_INMEMORY=1` BEFORE any test file imports
matchmaking_server. matchmaking_server's module-level `_storage` is
initialized at module-load time via ``matchmaker_storage.default_storage()``,
which reads this env var. Setting it here keeps the active backend on
InMemoryStorage for the entire pytest session, so the 26 existing tests in
test_matchmaking.py + test_launcher_logic.py continue to pass without any
modification.

Tests that need the production SQLite backend construct SqliteStorage
directly against a tmp_path file (see tests/test_persistence.py) rather
than untoggling this env var, which would mutate state for the whole
session.
"""

import os

os.environ.setdefault("MATCHMAKER_USE_INMEMORY", "1")
