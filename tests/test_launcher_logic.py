"""Tests for the non-GUI parts of the launcher stack.

Covers:
  - SavedDB: round-trip + atomic-write isolation + remove_server()
  - MatchClient: end-to-end via Flask test_server (real HTTP loop)
  - game_engine/game_stub.py: refuses empty token; normal contract
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from launcher_core import MatchClient, SavedDB  # noqa: E402
from matchmaking_server import create_app, reset_state  # noqa: E402


# --- SavedDB tests ----------------------------------------------------------

def test_saved_db_roundtrip(tmp_path: Path):
    p = tmp_path / "saved.json"
    db = SavedDB(path=p)
    db.save_server("pvp-eu", "abc-1")
    db.save_server("pvp-us", "xyz-2")
    mapping = db.get_saved_servers()
    assert mapping == {"pvp-eu": "abc-1", "pvp-us": "xyz-2"}


def test_saved_db_remove_returns_bool(tmp_path: Path):
    p = tmp_path / "saved.json"
    db = SavedDB(path=p)
    db.save_server("a", "1")
    assert db.remove_server("a") is True
    assert db.remove_server("a") is False  # second remove is a no-op
    assert db.get_saved_servers() == {}


def test_saved_db_separate_files_dont_cross(tmp_path: Path):
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    SavedDB(path=a).save_server("in-a", "1")
    SavedDB(path=b).save_server("in-b", "2")
    assert SavedDB(path=a).get_saved_servers() == {"in-a": "1"}
    assert SavedDB(path=b).get_saved_servers() == {"in-b": "2"}


def test_saved_db_recovers_from_corrupt_file(tmp_path: Path):
    p = tmp_path / "saved.json"
    p.write_text("{ this is not valid json", encoding="utf-8")
    db = SavedDB(path=p)
    assert db.get_saved_servers() == {}
    db.save_server("k", "v")
    assert db.get_saved_servers() == {"k": "v"}


def test_saved_db_default_path_under_user_dir():
    # Default path lives under user dir (AppData on win, ~/.local/share/...).
    db = SavedDB()
    assert db.path.parent.exists()
    assert db.path.name == "saved.json"


# --- MatchClient via Flask test_server (real HTTP loop) ---------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def matchmaker_server():
    """Start the Flask app on a real random port in a daemon thread."""
    reset_state()
    app = create_app()
    port = _free_port()
    th = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, use_reloader=False, debug=False),
        daemon=True,
    )
    th.start()
    base = f"http://127.0.0.1:{port}"
    # wait for /api/health
    import time
    for _ in range(30):
        try:
            if requests.get(f"{base}/api/health", timeout=0.5).ok:
                break
        except requests.RequestException:
            pass
        time.sleep(0.1)
    yield f"{base}"
    # daemon thread dies with pytest process


def test_matchclient_health(matchmaker_server: str):
    assert MatchClient(matchmaker_server).health() is True


def test_match_client_register_login_list(matchmaker_server: str):
    c = MatchClient(matchmaker_server)
    c.register("mary", "pw1234")
    tok = c.login("mary", "pw1234")
    assert isinstance(tok, str) and len(tok) > 8
    # Server list is empty pre-heartbeat.
    assert c.list_servers() == []


def test_match_client_register_dup_raises(matchmaker_server: str):
    c = MatchClient(matchmaker_server)
    c.register("dup", "pw1234")
    with pytest.raises(ValueError, match="user exists"):
        c.register("dup", "pw1234")


def test_match_client_login_bad_creds_raises(matchmaker_server: str):
    c = MatchClient(matchmaker_server)
    c.register("bad", "pw1234")
    with pytest.raises(ValueError, match="bad credentials"):
        c.login("bad", "wrong")


def test_match_client_trailing_slash_in_url_still_works(matchmaker_server: str):
    assert MatchClient(matchmaker_server + "/").health() is True


# --- game_stub subprocess ---------------------------------------------------

def _run_stub(*extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "game_engine" / "game_stub.py"),
         "--ticks", "1", *extra],
        capture_output=True, text=True, timeout=10,
    )


def test_stub_refuses_empty_token():
    r = _run_stub()
    assert r.returncode == 2
    assert "refusing to join" in r.stderr


def test_stub_normal_contract():
    r = _run_stub("--token", "tok-abc", "--username", "tester", "--server", "9.9.9.9",
                  "--port", "12345")
    assert r.returncode == 0
    assert "[engine] connect to 9.9.9.9:12345 as 'tester' (token=7 chars)" in r.stdout
    assert "[engine] tick 1/1" in r.stdout
    assert "[engine] exit" in r.stdout


def test_stub_quiet_flag_suppresses_ticks():
    r = _run_stub("--token", "tok", "--quiet")
    assert r.returncode == 0
    assert "[engine] tick" not in r.stdout
