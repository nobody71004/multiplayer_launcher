"""Tkinter launcher GUI.

Layout:
  - settings (matchmaker URL + Apply + online/offline light)
  - account (register/login buttons + who's-signed-in status)
  - servers (Treeview of live servers, scrollbar)
  - actions (Refresh, Save alias, Forget alias, Launch, Stop game)

All networking happens in background threads; UI updates are scheduled via
root.after() to keep the loop tight.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Optional

from launcher_core import DEFAULT_CONFIG_PATH, DEFAULT_SAVED_PATH, MatchClient, SavedDB


class LauncherApp:
    GAME_STUB_REL = Path("game_engine") / "game_stub.py"

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Multiplayer Launcher")
        self.root.geometry("820x540")

        self.config = self._load_config()
        self.client = MatchClient(self.config["matchmaker_url"])
        self.saved = SavedDB()

        self.token: Optional[str] = None
        self.username: Optional[str] = None
        self._game_proc: Optional[subprocess.Popen] = None

        self._build_ui()
        # Kick off an initial refresh so the user sees the green light /
        # server list immediately.
        self._refresh_servers_async()

    # -----------------------------------------------------------------------
    # config persistence

    def _load_config(self) -> dict:
        if DEFAULT_CONFIG_PATH.exists():
            try:
                return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {"matchmaker_url": "http://127.0.0.1:5000"}

    def _save_config(self) -> None:
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DEFAULT_CONFIG_PATH.with_suffix(
            DEFAULT_CONFIG_PATH.suffix + ".tmp"
        )
        tmp.write_text(
            json.dumps(self.config, indent=2, sort_keys=True), encoding="utf-8"
        )
        tmp.replace(DEFAULT_CONFIG_PATH)

    # -----------------------------------------------------------------------
    # UI construction

    def _build_ui(self) -> None:
        # --- settings row ---
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Matchmaker URL:").pack(side="left")
        self.url_var = tk.StringVar(value=self.config["matchmaker_url"])
        ttk.Entry(top, textvariable=self.url_var, width=44).pack(side="left", padx=4)
        ttk.Button(top, text="Apply", command=self._apply_url).pack(side="left")
        self.health_var = tk.StringVar(value="…")
        ttk.Label(top, textvariable=self.health_var).pack(side="right")

        # --- account row ---
        login = ttk.LabelFrame(self.root, text="Account", padding=8)
        login.pack(fill="x", padx=8)
        ttk.Label(login, text="Username:").grid(row=0, column=0, sticky="e")
        self.user_var = tk.StringVar()
        ttk.Entry(login, textvariable=self.user_var, width=24).grid(row=0, column=1)
        ttk.Label(login, text="Password:").grid(row=0, column=2, sticky="e")
        self.pw_var = tk.StringVar()
        ttk.Entry(
            login, textvariable=self.pw_var, width=24, show="*"
        ).grid(row=0, column=3)
        ttk.Button(login, text="Register", command=self._register).grid(
            row=0, column=4, padx=4
        )
        ttk.Button(login, text="Login", command=self._login).grid(
            row=0, column=5, padx=4
        )
        self.who_var = tk.StringVar(value="(not signed in)")
        ttk.Label(login, textvariable=self.who_var).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(6, 0)
        )

        # --- server tree ---
        servers_frame = ttk.LabelFrame(self.root, text="Servers", padding=8)
        servers_frame.pack(fill="both", expand=True, padx=8, pady=4)
        cols = ("name", "host", "port", "players", "id")
        self.tree = ttk.Treeview(
            servers_frame, columns=cols, show="headings", selectmode="browse"
        )
        for c, header in zip(
            cols, ("Name", "Host", "Port", "Players", "ID")
        ):
            self.tree.heading(c, text=header)
        self.tree.column("name", width=240)
        self.tree.column("host", width=160)
        self.tree.column("port", width=60)
        self.tree.column("players", width=80)
        self.tree.column("id", width=120)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(
            servers_frame, orient="vertical", command=self.tree.yview
        )
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

        # --- actions row ---
        bot = ttk.Frame(self.root, padding=8)
        bot.pack(fill="x")
        ttk.Button(bot, text="Refresh", command=self._refresh_servers_async).pack(
            side="left"
        )
        ttk.Button(bot, text="Save alias…", command=self._save_alias).pack(
            side="left", padx=4
        )
        ttk.Button(bot, text="Forget alias", command=self._forget_alias).pack(
            side="left"
        )
        ttk.Button(bot, text="Launch", command=self._launch).pack(side="right")
        ttk.Button(bot, text="Stop game", command=self._stop_game).pack(
            side="right", padx=4
        )

        # --- status bar ---
        self.status_var = tk.StringVar(value="ready")
        ttk.Label(
            self.root, textvariable=self.status_var, relief="sunken",
            anchor="w", padding=4,
        ).pack(fill="x", side="bottom")

    # -----------------------------------------------------------------------
    # URL handling

    def _apply_url(self) -> None:
        new = self.url_var.get().strip()
        if not new:
            messagebox.showinfo("Apply URL", "URL cannot be empty")
            return
        self.config["matchmaker_url"] = new
        self._save_config()
        self.client = MatchClient(new)
        self._set_status(f"URL applied: {new}")
        self._refresh_servers_async()

    # -----------------------------------------------------------------------
    # account actions

    def _register(self) -> None:
        u = self.user_var.get().strip()
        p = self.pw_var.get()
        if len(u) < 3 or len(p) < 4:
            messagebox.showinfo(
                "Register", "Username ≥ 3 chars, password ≥ 4 chars"
            )
            return
        try:
            self.client.register(u, p)
        except Exception as e:
            messagebox.showerror("Register failed", str(e))
            return
        self._set_status(f"registered '{u}' (now log in)")

    def _login(self) -> None:
        u = self.user_var.get().strip()
        p = self.pw_var.get()
        if not u or not p:
            return
        try:
            self.token = self.client.login(u, p)
        except Exception as e:
            messagebox.showerror("Login failed", str(e))
            return
        self.username = u
        self.who_var.set(f"signed in as {u}")
        self._set_status(f"signed in as {u}")

    # -----------------------------------------------------------------------
    # server list

    def _refresh_servers_async(self) -> None:
        threading.Thread(
            target=self._refresh_servers_worker, daemon=True
        ).start()

    def _refresh_servers_worker(self) -> None:
        try:
            servers = self.client.list_servers()
        except Exception as e:
            self.root.after(0, lambda: self.health_var.set("offline"))
            self.root.after(0, lambda: self._set_status(f"refresh failed: {e}"))
            return
        self.root.after(0, lambda: self._rebuild_tree(servers))
        self.root.after(0, lambda: self.health_var.set("online"))

    def _rebuild_tree(self, servers: list) -> None:
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for s in servers:
            self.tree.insert(
                "", "end", iid=s["id"],
                values=(
                    s["name"], s["host"], s["port"],
                    f"{s['players']}/{s['max_players']}", s["id"],
                ),
            )
        self._set_status(f"{len(servers)} server(s) live")

    def _selected_server(self) -> Optional[dict]:
        sel = self.tree.selection()
        if not sel:
            return None
        iid = sel[0]
        return {
            "id": iid,
            "name": self.tree.set(iid, "name"),
            "host": self.tree.set(iid, "host"),
            "port": self.tree.set(iid, "port"),
        }

    # -----------------------------------------------------------------------
    # saved-server aliases

    def _save_alias(self) -> None:
        s = self._selected_server()
        if s is None:
            messagebox.showinfo("Save alias", "Pick a server first")
            return
        alias = simpledialog.askstring(
            "Save alias", "Alias name (e.g. 'pvp-eu'):", parent=self.root
        )
        if not alias:
            return
        alias = alias.strip()
        if not alias:
            return
        self.saved.save_server(alias, s["id"])
        self._set_status(f"saved '{alias}' -> {s['id']}")

    def _forget_alias(self) -> None:
        mapping = self.saved.get_saved_servers()
        if not mapping:
            messagebox.showinfo("Saved aliases", "None remembered")
            return
        alias = simpledialog.askstring(
            "Forget alias",
            f"Which? Available: {', '.join(sorted(mapping))}",
            parent=self.root,
        )
        if not alias:
            return
        if self.saved.remove_server(alias.strip()):
            self._set_status(f"forgot '{alias}'")
        else:
            self._set_status(f"'{alias}' was not remembered")

    # -----------------------------------------------------------------------
    # launch / stop

    def _launch(self) -> None:
        s = self._selected_server()
        if s is None:
            messagebox.showinfo("Launch", "Pick a server first")
            return
        if not self.token or not self.username:
            messagebox.showinfo("Launch", "Sign in first")
            return

        engine = Path(__file__).resolve().parent / self.GAME_STUB_REL
        if not engine.exists():
            messagebox.showerror("Launch", f"engine stub missing: {engine}")
            return

        cmd = [
            sys.executable, str(engine),
            "--server", s["host"],
            "--port", str(s["port"]),
            "--token", self.token,
            "--username", self.username,
        ]
        try:
            self._game_proc = subprocess.Popen(cmd)
        except OSError as e:
            messagebox.showerror("Launch failed", str(e))
            return
        self._set_status(f"launched (pid {self._game_proc.pid}) -> {s['host']}:{s['port']}")

    def _stop_game(self) -> None:
        if self._game_proc is None:
            messagebox.showinfo("Stop game", "No game running")
            return
        try:
            self._game_proc.terminate()
        except Exception as e:
            messagebox.showerror("Stop failed", str(e))
            return
        self._set_status(f"terminated (pid {self._game_proc.pid})")

    # -----------------------------------------------------------------------
    # misc

    def _set_status(self, msg: str) -> None:
        self.status_var.set(msg)


def main() -> int:
    root = tk.Tk()
    LauncherApp(root)
    return root.mainloop()


if __name__ == "__main__":
    sys.exit(main())
