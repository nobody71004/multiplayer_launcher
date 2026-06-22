# Multiplayer Launcher

A from-scratch starter for a multiplayer game's launcher stack. Tkinter GUI,
Flask REST matchmaker, custom-engine contract, pytest suite. Swapping the
Python engine stub for a real game binary is a single-file change.

This project lives under the existing repo as `multiplayer_launcher/`. It has
no dependency on, no path overlap with, and no shared code with the parent
project. Treat it as a standalone package.

## Layout

```
multiplayer_launcher/
├── README.md                       # this file
├── requirements.txt                # flask, requests, pytest
├── matchmaking_server.py           # Flask REST matchmaker
├── launcher_core.py                # MatchClient + SavedDB (no Tkinter dep)
├── multiplayer_launcher.py         # Tkinter GUI launcher
├── game_engine/
│   ├── README.md                   # custom-engine contract
│   ├── engine_contract.py          # shared constants
│   └── game_stub.py                # python stand-in engine
├── scripts/
│   └── start_all.py                # dev: matchmaker + stub + self-test
└── tests/
    ├── __init__.py
    ├── test_launcher_logic.py      # SavedDB + MatchClient + game-stub
    └── test_matchmaking.py         # Flask endpoint contract tests
```

## Quickstart (dev, two terminals)

```bash
cd multiplayer_launcher
python -m pip install -r requirements.txt

# terminal 1: matchmaker
python matchmaking_server.py --port 5000

# terminal 2: GUI
python multiplayer_launcher.py
```

## Quickstart (headless self-test)

```bash
python scripts/start_all.py
# exercises: matchmaker boot + register + login + heartbeat + /api/servers
#           + game-stub subprocess integration — exits non-zero on any failure
```

## Tests

```bash
python -m pytest tests/ -v
```

The tests reset the matchmaker's in-memory state between tests via an
`autouse` fixture, so they can run in any order without leaking state.

## Integrating your custom engine

See [`game_engine/README.md`](game_engine/README.md). Summary:

- The launcher spawns the engine as a subprocess.
- The launcher passes `--server <host> --port <int> --token <jwt> --username <str>`.
- Replace `game_engine/game_stub.py` with a shim that proxies argv into your
  real engine binary.

## Where state lives

- **Matchmaker user/session state** is held in a SQLite database at
  `./matchmaker.db` by default (path overridable via the `MATCHMAKER_DB`
  env var or `--db` CLI flag). Restart `matchmaking_server.py` and
  registered users, tokens, and live-server heartbeats are restored from
  disk. The schema and concurrency semantics live in
  [`matchmaker_storage.SqliteStorage`](matchmaker_storage.py). The
  in-memory variant (`InMemoryStorage`) is used by the test suite --
  forced via `MATCHMAKER_USE_INMEMORY=1`, which `tests/conftest.py` sets
  before any test file imports `matchmaking_server`. That conftest line
  is the migration guard that keeps the existing 26 tests working
  unchanged.
- **Launcher config** is `~/.multiplayer_launcher/config.json` (matchmaker URL).
- **Saved server aliases** are `~/.multiplayer_launcher/saved.json`
  (user-chosen names → server IDs from the matchmaker).
