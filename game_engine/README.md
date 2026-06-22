# Custom-engine contract

The launcher spawns the engine as a subprocess with a fixed argv contract.
This file is the authoritative spec for that contract; the launcher reads
`ENGINE_BIN_REL` from `engine_contract.py` and the stub reads its own
`parse_args()`. Both should stay in sync with this doc.

## Argv contract

```
python game_engine/game_stub.py --server <host> --port <int> \
                                --token  <jwt>  --username <str> \
                              [--ticks N --quiet]
```

| argument      | required | meaning                                                  |
| ------------- | :------: | -------------------------------------------------------- |
| `--server`    |    Y     | game-server host (string, IPv4 or DNS)                   |
| `--port`      |    Y     | game-server port (integer; converted via `int()`)        |
| `--token`     |    Y     | matchmaker-issued JWT-style token (string; non-empty)    |
| `--username`  |    N     | display name in-game (string; may be empty)              |
| `--ticks N`   |    N     | **stub-only** simulated ticks before exit (default 4)    |
| `--quiet`     |    N     | **stub-only** suppress per-tick stdout output            |

## Exit-code contract

| code | meaning                                  |
| :--: | ---------------------------------------- |
|  0   | clean shutdown                           |
|  1   | unexpected error during run              |
|  2   | refusal to connect (no token, bad input) |

## stdio contract

- `[engine] ...` prefixed lines to **stdout** for normal status.
- `[engine] WARN ...` prefixed lines to **stderr** for non-fatal issues.
- The launcher does not read stdin (engine is responsible for any /chat or
  console input).

## Two ways to swap in your real engine

1. **Replace `game_stub.py`** with a shim that proxies argv into your
   binary:

   ```python
   # game_engine/real_engine_shim.py
   import subprocess, sys
   PROXY = "/path/to/your/engine.exe"
   if __name__ == "__main__":
       sys.exit(subprocess.call([PROXY] + sys.argv[1:]))
   ```

   Then update `GAME_STUB_REL` in `engine_contract.py` to point at it.

2. **Update `ENGINE_BIN_REL`** in `engine_contract.py` to point at your
   engine binary directly, and update the launcher's `_launch()` method
   to call it without `sys.executable`. (This is the simpler path for a
   compiled native engine.)

## Authentication

The token is **opaque to the engine**. The engine forwards it to the game
server, which validates it with the matchmaker over a separate channel.
The engine is not responsible for token validation.
