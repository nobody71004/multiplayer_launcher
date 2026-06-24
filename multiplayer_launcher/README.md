# multiplayer_launcher patch scripts

This sub-directory contains a set of CLI patch scripts that add Phase 5
admin / HUD functionality to the `xbuniverse` umbrella repository.
The scripts are CLI-only (run as `python <script>.py UMBRELLA_ROOT`).

## Scripts

| Script                                     | Patches                                                                                                                            |
|--------------------------------------------|------------------------------------------------------------------------------------------------------------------------------------|
| `_xb_splice_phase5.py`                     | Inserts canonical phase5 blocks into `xbuniverse_server/server.py`, `index.html`, and `init.lua`. Also writes `xbuniverse_server/tests/test_admin_extras_ping_logger_servers.py`. |
| `_phase5_eof_splice.py`                    | Same targets via EOF append (composed blocks via `_phase5_block_*` helpers, stripping any existing block).                          |
| `_xb_reanchor_phase5.py`                   | Re-anchors only `xbuniverse_server/server.py` to the latest tail block.                                                             |
| `_phase5_apply_with_login_fallback.py`     | Inserts a no-op `login_required` decorator right after the phase5 BEGIN sentinel (fixes pytest `NameError`). Idempotent.            |

## Idempotency

Each script uses sentinel markers of the form
`# === xbuniverse_phase5 BEGIN ===` / `# === xbuniverse_phase5 END ===`
(or the HTML / Lua comment equivalents).  Re-running any script a
second time replaces the existing block instead of duplicating it.  See
`tests/test_phase5_patches.py` for cross-script idempotency coverage.

## Atomic writes

All scripts write to a sibling `<path>.tmp` and then `os.replace` it
onto the target so a crash mid-write cannot corrupt the target file.

## Usage

```
python multiplayer_launcher/multiplayer_launcher/_xb_splice_phase5.py /path/to/umbrella_root
python multiplayer_launcher/multiplayer_launcher/_phase5_apply_with_login_fallback.py /path/to/umbrella_root
python multiplayer_launcher/multiplayer_launcher/_xb_reanchor_phase5.py /path/to/umbrella_root
```
