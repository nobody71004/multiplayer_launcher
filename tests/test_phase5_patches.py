"""Tests for the phase-5 splice patch scripts.

Each script is invoked as a CLI subprocess against a fake umbrella root
built under pytest's ``tmp_path`` fixture.  The umbrella root mirrors the
real consumer layout:

* ``xbuniverse_server/server.py``         -- Flask app with ``login_required``
* ``xbuniverse_server/index.html``       -- admin panel HTML
* ``xbuniverse_plugin/cet/init.lua``      -- Cyber Engine Tweaks HUD entry
* ``xbuniverse_server/tests/``            -- where the splice emits the
                                               admin-extras regression test
"""
from __future__ import annotations

import ast
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest


CANONICAL_DIR = (
    Path(__file__).resolve().parent.parent / "multiplayer_launcher"
)


SERVER_PY_TEMPLATE = (
    '"""Test stub xbuniverse_server.server module."""\n'
    "from flask import Flask\n"
    "\n"
    "\n"
    "def login_required(fn):\n"
    "    def wrapper(*args, **kwargs):\n"
    "        return fn(*args, **kwargs)\n"
    "    return wrapper\n"
    "\n"
    "\n"
    'app = Flask(__name__)\n'
    "\n"
    "\n"
    '@app.route("/api/health")\n'
    "def health():\n"
    '    return {"status": "ok"}, 200\n'
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)

# Variant without login_required -- used to exercise
# _phase5_apply_with_login_fallback.py's no-op decorator.
SERVER_PY_NO_LOGIN_REQ = (
    '"""Test stub xbuniverse_server.server module without login_required."""\n'
    "from flask import Flask\n"
    "\n"
    "\n"
    'app = Flask(__name__)\n'
    "\n"
    "\n"
    '@app.route("/api/health")\n'
    "def health():\n"
    '    return {"status": "ok"}, 200\n'
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    app.run()\n"
)


INDEX_HTML_TEMPLATE = (
    "<!doctype html>\n"
    "<html>\n"
    "  <body>\n"
    '    <div id="root"></div>\n'
    "  </body>\n"
    "</html>\n"
)


INIT_LUA_TEMPLATE = "-- Placeholder init.lua (CET HUD entry)\nreturn {}\n"


def _build_umbrella(root: Path, *, with_login_required: bool = True) -> Path:
    """Build a minimal umbrella root matching the real consumer layout."""
    server_dir = root / "xbuniverse_server" / "tests"
    server_dir.mkdir(parents=True, exist_ok=True)
    (root / "xbuniverse_server" / "__init__.py").write_text("")
    (root / "xbuniverse_server" / "server.py").write_text(
        SERVER_PY_TEMPLATE if with_login_required else SERVER_PY_NO_LOGIN_REQ
    )
    (root / "xbuniverse_server" / "index.html").write_text(INDEX_HTML_TEMPLATE)
    cet_dir = root / "xbuniverse_plugin" / "cet"
    cet_dir.mkdir(parents=True, exist_ok=True)
    (cet_dir / "init.lua").write_text(INIT_LUA_TEMPLATE)
    return root


@pytest.fixture()
def umbrella(tmp_path: Path) -> Path:
    return _build_umbrella(tmp_path, with_login_required=True)


@pytest.fixture()
def umbrella_no_login_required(tmp_path: Path) -> Path:
    return _build_umbrella(tmp_path, with_login_required=False)


def _run(script: str, root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CANONICAL_DIR / script), str(root)],
        check=True,
        capture_output=True,
        text=True,
    )


def _assert_compiles(path: Path) -> None:
    py_compile.compile(str(path), doraise=True)


PY_BEGIN = "# === xbuniverse_phase5 BEGIN ==="
PY_END = "# === xbuniverse_phase5 END ==="
LFB_BEGIN = "# === xbuniverse_phase5_login_fallback BEGIN ==="
HTML_BEGIN = "<!-- === xbuniverse_phase5 BEGIN === -->"
LUA_BEGIN = "-- === xbuniverse_phase5 BEGIN ==="


# ---- _xb_splice_phase5 ---------------------------------------------------



def test_login_fallback_provides_decorator_when_missing(
    umbrella_no_login_required: Path,
) -> None:
    """The fallback decorator exists to fix a pytest NameError when the
    umbrella's server.py lacks a module-scope login_required. Exercise
    that path: build an umbrella WITHOUT login_required, then verify the
    fallback script still produces a server.py that:
      - has the login_required fallback BEGIN marker,
      - compiles cleanly,
      - binds login_required as a callable so @-syntax works.
    """
    pytest.importorskip("flask")
    umbrella = umbrella_no_login_required
    _run("_xb_splice_phase5.py", umbrella)
    server_path = umbrella / "xbuniverse_server" / "server.py"

    pre = server_path.read_text(encoding="utf-8")
    assert "def login_required" not in pre
    assert "@login_required" in pre  # Splice injected the @-usage.

    _run("_phase5_apply_with_login_fallback.py", umbrella)
    post = server_path.read_text(encoding="utf-8")

    assert LFB_BEGIN in post
    _assert_compiles(server_path)

    # AST-level check that the fallback inserted a real module-scope
    # binding for login_required via `except NameError: def login_required(...)`.
    # We avoid exec() here -- it would force a Flask import (and Flask's
    # get_root_path chokes on the unparented __name__ namespace).
    tree = ast.parse(post)
    found_fallback = False
    for node in tree.body:
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                type_ = handler.type
                type_id = getattr(type_, "id", None) if type_ is not None else None
                if type_id == "NameError" and handler.body and isinstance(
                    handler.body[0], ast.FunctionDef
                ) and handler.body[0].name == "login_required":
                    found_fallback = True
                    break
            if found_fallback:
                break
    assert found_fallback, (
        "fallback `try/except NameError -> def login_required` not found"
    )
