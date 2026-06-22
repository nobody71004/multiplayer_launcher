"""Verify the persistence story end-to-end.

Specifically: open the saved MATCHMAKER_DB from disk in a fresh Python
process and assert the EXACT persistent state written by the prior
matchmaker subprocess + closed cleanly. This is the EXCLUSIVE
end-to-end test for SqliteStorage persistence across the matchmaker
restart boundary; tests/test_persistence.py only validates the
SqliteStorage API surface area in-process.

The assertions are intentionally tighter than a "non-empty tables" check:

  - len(users)  == 1                # start_all.py registers exactly 1 user
  - users[0][0].startswith("demo-") # matches username = f"demo-{pid}"
  - len(tokens) == 1                # exactly 1 token after login
  - len(servers) == 1                # exactly 1 server heartbeat

If any of those go missing OR the user-name pattern drifts, the verify
step fails loudly -- catching silent partial-register regressions that
a non-empty check would miss entirely.

This script is cwd-agnostic: matches start_all.py's sys.path.insert
so `python scripts/verify_cross_restart.py [<db_path>]` works from
any invocation directory. It also reads MATCHMAKER_DB from
os.environ as a fallback when argv is empty, so the CI workflow does
NOT need shell-variable expansion of `"$MATCHMAKER_DB"` -- and the
exact same workflow runs on bash (Linux runners) AND on pwsh (the GHA
windows-latest default shell that doesn't expand `$NAME` to env-vars).

Usage:
    python scripts/verify_cross_restart.py [<matchmaker_db_path>]

Exit codes:
    0  PASS: cross-restart visibility verified
    1  FAIL: db file missing / wrong row counts / user-name pattern drift
    2  USAGE: positional arg AND env var both missing or empty
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Mirror start_all.py's bootstrap: insert the project root (parent of
# scripts/) into sys.path so the matchmaker_storage import is resolvable
# from any cwd where this script is invoked.
_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))

from matchmaker_storage import SqliteStorage  # noqa: E402


def _resolve_db_path() -> Path | None:
    """Resolve the DB path with explicit precedence:
         1. sys.argv[1] if non-empty (allow local-dev override)
         2. MATCHMAKER_DB env var (workflow-driven; safe on bash AND pwsh)
         3. None (caller prints a usage error)
    """
    if len(sys.argv) >= 2 and sys.argv[1]:
        return Path(sys.argv[1])
    env = os.environ.get("MATCHMAKER_DB", "")
    if env:
        return Path(env)
    return None


def main() -> int:
    db_path = _resolve_db_path()
    if db_path is None:
        print(
            "usage: verify_cross_restart.py [<matchmaker_db_path>]\n"
            "       or set MATCHMAKER_DB env var. Both are required.",
            file=sys.stderr,
        )
        return 2
    if not db_path.exists():
        print(
            f"FAIL: cross-restart visibility -- db file does not "
            f"exist at {db_path}",
            file=sys.stderr,
        )
        return 1
    storage = SqliteStorage(db_path)
    try:
        cur = storage._conn.cursor()
        users = cur.execute("SELECT username FROM users").fetchall()
        tokens = cur.execute("SELECT token FROM tokens").fetchall()
        servers = cur.execute(
            "SELECT server_id, name FROM servers"
        ).fetchall()
    finally:
        storage.close()

    failures: list[str] = []
    if len(users) != 1:
        failures.append(f"expected 1 user, got {len(users)}")
    elif not users[0][0].startswith("demo-"):
        failures.append(
            f"user name does not start with 'demo-' (start_all.py's "
            f"pid-prefixed pattern): got {users[0][0]!r}"
        )
    if len(tokens) != 1:
        failures.append(f"expected 1 token, got {len(tokens)}")
    if len(servers) != 1:
        failures.append(f"expected 1 server, got {len(servers)}")

    if failures:
        print(
            "FAIL: cross-restart visibility -- "
            + "; ".join(failures)
            + f" at {db_path}",
            file=sys.stderr,
        )
        return 1

    print(f"PASS: cross-restart visibility verified at {db_path}")
    print(f"  user   : {users[0][0]}")
    print(f"  token  : truncated first 8 chars: {tokens[0][0][:8]!r}...")
    print(f"  server : id={servers[0][0]} name={servers[0][1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
