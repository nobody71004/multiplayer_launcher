"""Tkinter-free core: HTTP client to the matchmaker + saved-server JSON store.

Kept in its own module so tests and the GUI share the same business logic
without dragging in tkinter at test time.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import requests

# If running on Windows, prefer the user-profile-based directory.
if sys.platform.startswith("win"):
    _DEFAULT_DATA_DIR = Path.home() / "AppData" / "Local" / "multiplayer_launcher"
else:
    _DEFAULT_DATA_DIR = Path.home() / ".local" / "share" / "multiplayer_launcher"
    # fall back to ~/.multiplayer_launcher if XDG_HOME isn't standard
    if not _DEFAULT_DATA_DIR.parent.exists():
        _DEFAULT_DATA_DIR = Path.home() / ".multiplayer_launcher"

DEFAULT_CONFIG_PATH = _DEFAULT_DATA_DIR / "config.json"
DEFAULT_SAVED_PATH = _DEFAULT_DATA_DIR / "saved.json"


class MatchClient:
    """Thin HTTP wrapper around the matchmaker. Safe to call from worker threads."""

    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # --- public methods -----------------------------------------------------

    def health(self) -> bool:
        r = requests.get(f"{self.base_url}/api/health", timeout=self.timeout)
        return r.ok

    def register(self, username: str, password: str) -> None:
        """Create a new account. Raises ValueError('user exists')."""
        r = requests.post(
            f"{self.base_url}/api/register",
            json={"username": username, "password": password},
            timeout=self.timeout,
        )
        if r.status_code == 409:
            raise ValueError("user exists")
        r.raise_for_status()

    def login(self, username: str, password: str) -> str:
        """Login. Returns the auth token. Raises ValueError('bad credentials')."""
        r = requests.post(
            f"{self.base_url}/api/login",
            json={"username": username, "password": password},
            timeout=self.timeout,
        )
        if r.status_code == 401:
            raise ValueError("bad credentials")
        r.raise_for_status()
        return r.json()["token"]

    def list_servers(self) -> list:
        r = requests.get(f"{self.base_url}/api/servers", timeout=self.timeout)
        r.raise_for_status()
        return r.json()["servers"]


class SavedDB:
    """Tiny JSON-backed alias=>server_id store. Atomic writes via tempfile + replace."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else DEFAULT_SAVED_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    # --- private helpers ----------------------------------------------------

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    # --- public methods -----------------------------------------------------

    def get_saved_servers(self) -> dict:
        return dict(self._read().get("saved_servers", {}))

    def save_server(self, alias: str, server_id: str) -> None:
        data = self._read()
        s = data.setdefault("saved_servers", {})
        s[alias] = server_id
        self._write(data)

    def remove_server(self, alias: str) -> bool:
        """Returns True if an entry was actually removed."""
        data = self._read()
        s = data.setdefault("saved_servers", {})
        removed = s.pop(alias, None) is not None
        if removed:
            self._write(data)
        return removed
