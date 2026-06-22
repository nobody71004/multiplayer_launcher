"""Engine-shim argv-shape CI tests.

These verify that the shim at `game_engine/game_stub.py` correctly proxies
argv to a real custom-engine binary once the integrator wires
ENGINE_BIN in `engine_contract.py`.

The shim has two modes (default stub-loop vs real-engine proxy). This
file exercises both via subprocess with an env-var override:

  ENGINE_BIN_OVERRIDE        = sys.executable
  ENGINE_ARGV_PREFIX_OVERRIDE = "<argv-recorder-script-path> [<extra flags>]"

That way the "engine" in the test is just the Python interpreter
running a tiny recorder script that dumps sys.argv to a JSON file. So
we can assert on argv shape without depending on a real engine binary.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _write_argv_recorder(tmp_path: Path, name: str = "_argv_recorder.py") -> tuple[Path, Path]:
    """Write a tiny Python script that dumps its argv to a JSON file.

    Returns (recorder_path, json_out_path). Returning both prevents the
    writer/reader drift that produced the ".py_argv.json" double-suffix
    footgun in earlier versions -- callers cannot re-derive a JSON path
    that doesn't match what the writer just produced.
    """
    recorder = tmp_path / name
    json_out = tmp_path / f"{recorder.stem}_argv.json"
    recorder.write_text(
        "import sys, json\n"
        f"json.dump(sys.argv, open({str(json_out)!r}, 'w'))\n",
        encoding="utf-8",
    )
    return recorder, json_out


def _shim_subprocess_env(tmp_path: Path) -> dict:
    """Build a clean env dict (with PATH) for shim subprocess tests."""
    return {"PATH": os.environ.get("PATH", "")}


def test_default_mode_runs_stub_loop(tmp_path: Path):
    """No env override + ENGINE_BIN=None: the shim's stub loop runs,
    producing [engine] connect / [engine] tick / [engine] exit."""
    cmd = [
        sys.executable, str(ROOT / "game_engine" / "game_stub.py"),
        "--token", "tok-abc",
        "--username", "tester",
        "--ticks", "1",
        "--quiet",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        env=_shim_subprocess_env(tmp_path),
        timeout=10,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    assert "[engine] connect" in proc.stdout
    assert "[engine] tick" not in proc.stdout  # --quiet respected
    assert "[engine] exit" in proc.stdout


def test_proxy_mode_forwards_argv_to_engine(tmp_path: Path):
    """ENGINE_BIN_OVERRIDE=sys.executable -> the recorder script sees
    --server --port --token --username in the right slots."""
    recorder, argv_out = _write_argv_recorder(tmp_path, name="_recorder_argv.py")
    env = _shim_subprocess_env(tmp_path)
    env["ENGINE_BIN_OVERRIDE"] = sys.executable
    env["ENGINE_ARGV_PREFIX_OVERRIDE"] = str(recorder)

    cmd = [
        sys.executable, str(ROOT / "game_engine" / "game_stub.py"),
        "--server", "9.9.9.9",
        "--port", "12345",
        "--token", "tok-abc",
        "--username", "tester",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=15,
    )
    # The recorder wrote its argv via json.dump; locate it.
    assert argv_out.exists(), (proc.stdout, proc.stderr, list(tmp_path.iterdir()))
    argv = json.loads(argv_out.read_text())
    # argv[0] is the recorder script path (Python's sys.argv convention).
    # argv[1:] should contain the launcher's flags in canonical order.
    assert "9.9.9.9" in argv
    assert "12345" in argv
    assert "tok-abc" in argv
    assert "tester" in argv
    # Cruder: --server must appear before 9.9.9.9, etc. -- flag-before-value.
    idx_server = argv.index("9.9.9.9")
    assert argv[idx_server - 1] == "--server"
    idx_port = argv.index("12345")
    assert argv[idx_port - 1] == "--port"
    idx_token = argv.index("tok-abc")
    assert argv[idx_token - 1] == "--token"
    idx_user = argv.index("tester")
    assert argv[idx_user - 1] == "--username"


def test_proxy_argv_prefix_lands_before_server(tmp_path: Path):
    """ENGINE_ARGV_PREFIX_OVERRIDE applies BEFORE --server --port --token --username."""
    recorder, argv_out = _write_argv_recorder(tmp_path, name="_recorder_prefix.py")
    env = _shim_subprocess_env(tmp_path)
    env["ENGINE_BIN_OVERRIDE"] = sys.executable
    env["ENGINE_ARGV_PREFIX_OVERRIDE"] = f"{recorder} --join --mode=ranked"

    cmd = [
        sys.executable, str(ROOT / "game_engine" / "game_stub.py"),
        "--server", "1.1.1.1",
        "--port", "1111",
        "--token", "tk",
        "--username", "u",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=15,
    )
    assert argv_out.exists(), (proc.stdout, proc.stderr)
    argv = json.loads(argv_out.read_text())
    # The prefix flags --join and --mode=ranked must appear BEFORE --server.
    idx_join = argv.index("--join")
    idx_server = argv.index("--server")
    assert idx_join < idx_server
    # --mode=ranked is also a flag (single token); assert it's before --server too.
    idx_mode = argv.index("--mode=ranked")
    assert idx_mode < idx_server


def test_empty_token_rejected_in_default_mode(tmp_path: Path):
    """Default mode + no --token: exit 2 + refusing stderr."""
    cmd = [
        sys.executable, str(ROOT / "game_engine" / "game_stub.py"),
        # no --token
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        env=_shim_subprocess_env(tmp_path),
        timeout=10,
    )
    assert proc.returncode == 2
    assert "refusing" in proc.stderr


def test_empty_token_rejected_before_execvp_attempt(tmp_path: Path):
    """Proxy mode + no --token: refused before execvp -- the recorder must
    NOT have been invoked."""
    recorder, argv_out = _write_argv_recorder(tmp_path, name="_recorder_neg.py")
    env = _shim_subprocess_env(tmp_path)
    env["ENGINE_BIN_OVERRIDE"] = sys.executable
    env["ENGINE_ARGV_PREFIX_OVERRIDE"] = str(recorder)

    cmd = [
        sys.executable, str(ROOT / "game_engine" / "game_stub.py"),
        # no --token
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=10,
    )
    assert proc.returncode == 2
    assert "refusing" in proc.stderr
    # Crucially: the recorder file must NOT exist because the shim bailed
    # before invoking execvp.
    assert not argv_out.exists(), (proc.stdout, proc.stderr)
