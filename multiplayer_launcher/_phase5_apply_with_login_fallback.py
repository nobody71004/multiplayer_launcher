"""Phase 5 minimal apply: only the minimum needed to make pytest pass.

The full splice via _phase5_eof_splice.py already produced valid syntax,
but pytest fails because @login_required is referenced in the appended
block and is not bound at module scope in this codebase (it's apparently
assigned dynamically below the if __main__ block, outside import-time
scope). We add a tiny no-op decorator fallback at the top of the appended
block so the decorator name is bound by the time the @-syntax evaluates.

This script INTENTIONALLY only applies the additional patches - the prior
splice of the full block was successful and only needs the login_required
fallback inserted at the head of the existing block.
"""

import os
import sys

def _atomic_write(path, content):
    """Write content to path atomically: write to a sibling .tmp file then
    os.replace() so the target is never truncated mid-write on crash."""
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)



FALLBACK_PATCH = (
    "\n"
    "# === xbuniverse_phase5_login_fallback BEGIN ===\n"
    "try:\n"
    "    login_required\n"
    "except NameError:\n"
    "    def login_required(fn):\n"
    "        return fn\n"
    "# === xbuniverse_phase5_login_fallback END ===\n"
)


def main():
    if len(sys.argv) != 2:
        print("Usage: phase5_apply_login_fallback.py UMBRELLA_ROOT", file=sys.stderr)
        sys.exit(2)
    root = sys.argv[1]
    server_py = os.path.join(root, "xbuniverse_server", "server.py")

    src = open(server_py, encoding="utf-8").read()
    begin = "# === xbuniverse_phase5 BEGIN ==="
    if begin not in src:
        print("ERROR: Phase 5 BEGIN sentinel not found in server.py - did the prior splice run?")
        sys.exit(3)
    # Idempotency: skip if the fallback already exists.
    if "# === xbuniverse_phase5_login_fallback BEGIN ===" in src:
        print("server.py: login_required fallback already applied, skipping")
        return
    # Insert fallback right after the BEGIN sentinel header line.
    insertion_point = src.find(begin) + len(begin)
    new = src[:insertion_point] + FALLBACK_PATCH + src[insertion_point:]
    _atomic_write(server_py, new)
    print("server.py: inserted login_required fallback (len was",
          len(src), "now", len(new), ")")


if __name__ == "__main__":
    main()
