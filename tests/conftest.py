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


import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register custom pytest markers used by this test suite.

    Markers are declared up-front so a marker-based selector like
    ``pytest -m 'not integration'`` (used by the dedicated
    ``integration`` CI job in .github/workflows/ci.yml) runs
    without "unknown marker" warnings at collection time.
    See ``tests/test_multi_client_integration.py`` for the
    consumer of these markers.
    """
    config.addinivalue_line(
        "markers",
        "integration: marks slow multi-subprocess integration tests "
        "(deselect with 'pytest -m \"not integration\"' to skip "
        "the matchmaker subprocess boot in fast unit loops)",
    )
