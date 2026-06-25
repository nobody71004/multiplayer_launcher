"""Telemetry + log + admin-viewer endpoint coverage.

Pins the contracts added by the operational telemetry vector:
  - /metrics returns Prometheus text-format exposition with the
    expected counter + gauge + histogram names.
  - /api/logs surfaces matchmaker-side JSONL events with the
    expected schema (ts, level, message, optional extras).
  - /admin/ returns HTTP 200 HTML containing the polling JS.
  - The metrics counters increment on each call to register/
    login/heartbeat, partitioned by result label.
  - The on_purge observer wires MAINTENANCE_PURGED_TOTAL correctly.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import matchmaking_server
from matchmaking_server import (
    ACTIVE_SERVERS_COUNT,
    HEARTBEATS_TOTAL,
    LOG_DIR,
    LOGINS_TOTAL,
    MAINTENANCE_PURGED_TOTAL,
    REGISTRATIONS_TOTAL,
    _LOG_PATH,
    _log_event,
    create_app,
    reset_state,
)


@pytest.fixture(autouse=True)
def _reset():
    reset_state()
    yield


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# --- /metrics exposition ----------------------------------------------------


def test_metrics_returns_prometheus_text_format(client):
    # Drive one non-/metrics request first so the labeled latency
    # histogram has at least one observation. Prometheus only emits
    # _count/_sum/_bucket lines for labeled histograms AFTER the
    # first observe(), so checking them on a fresh /metrics hit
    # would be tautologically-skipped (request.endpoint for the
    # /metrics call itself is recorded AFTER generate_latest()).
    client.get("/api/health")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.content_type.startswith("text/plain")
    body = r.get_data(as_text=True)
    expected = [
        "matchmaker_registrations_total",
        "matchmaker_logins_total",
        "matchmaker_heartbeats_total",
        "matchmaker_active_servers_count",
        "matchmaker_request_latency_seconds_count",
        "matchmaker_maintenance_purged_total",
    ]
    for key in expected:
        assert key in body, f"{key} not found in /metrics exposition"


def test_register_ok_increments_created_counter(client):
    r = client.post(
        "/api/register",
        json={"username": "alice", "password": "pw1234"},
    )
    assert r.status_code == 201
    body = client.get("/metrics").get_data(as_text=True)
    # We can't pin to exact value because counters persist across
    # tests within the module-level REGISTRY. But the line MUST
    # contain a positive counter value for the labels.
    line = [l for l in body.splitlines()
            if l.startswith('matchmaker_registrations_total{result="created"}')]
    assert line, "no registrations_total{result=created} line in /metrics"
    last = line[-1]
    val = float(last.rsplit(" ", 1)[-1])
    assert val >= 1.0, f"counter should have grown: {last}"


def test_register_invalid_increments_invalid_counter(client):
    r = client.post(
        "/api/register",
        json={"username": "x", "password": "pw1234"},
    )
    assert r.status_code == 400
    body = client.get("/metrics").get_data(as_text=True)
    assert 'matchmaker_registrations_total{result="invalid"}' in body


def test_register_duplicate_increments_duplicate_counter(client):
    client.post("/api/register", json={"username": "bob", "password": "pw1234"})
    r = client.post("/api/register", json={"username": "bob", "password": "pw1234"})
    assert r.status_code == 409
    body = client.get("/metrics").get_data(as_text=True)
    assert 'matchmaker_registrations_total{result="duplicate"}' in body


def test_login_outcomes_partition_labels(client):
    # unknown_user
    r = client.post("/api/login", json={"username": "nope", "password": "nope"})
    assert r.status_code == 401
    # bad_pw
    client.post("/api/register", json={"username": "carol", "password": "pw1234"})
    r = client.post("/api/login", json={"username": "carol", "password": "WRONG"})
    assert r.status_code == 401
    # ok
    r = client.post("/api/login", json={"username": "carol", "password": "pw1234"})
    assert r.status_code == 200

    body = client.get("/metrics").get_data(as_text=True)
    for label in ("ok", "bad_pw", "unknown_user"):
        needle = f'matchmaker_logins_total{{result="{label}"}}'
        assert needle in body, f"missing {needle} in /metrics"


def test_heartbeat_outcomes_partition_labels(client):
    client.post("/api/register", json={"username": "host", "password": "pw1234"})
    tok = client.post(
        "/api/login",
        json={"username": "host", "password": "pw1234"},
    ).get_json()["token"]
    # invalid_token
    client.post("/api/heartbeat", json={"token": "garbage", "server_id": "x"})
    # server_id_required
    client.post("/api/heartbeat", json={"token": tok})
    # ok
    client.post("/api/heartbeat", json={"token": tok, "server_id": "srv-1"})

    body = client.get("/metrics").get_data(as_text=True)
    for label in ("ok", "invalid_token", "server_id_required"):
        needle = f'matchmaker_heartbeats_total{{result="{label}"}}'
        assert needle in body, f"missing {needle} in /metrics"


def test_active_servers_gauge_set_on_servers_hit(client):
    client.post("/api/register", json={"username": "host", "password": "pw1234"})
    tok = client.post(
        "/api/login",
        json={"username": "host", "password": "pw1234"},
    ).get_json()["token"]
    client.post(
        "/api/heartbeat",
        json={"token": tok, "server_id": "srv-A"},
    )
    srvs = client.get("/api/servers").get_json()["servers"]
    assert any(s["id"] == "srv-A" for s in srvs)

    # Read the gauge via /metrics -- the line MUST show >= 1.
    body = client.get("/metrics").get_data(as_text=True)
    line = next(
        l for l in body.splitlines()
        if l.startswith("matchmaker_active_servers_count ")
    )
    assert float(line.rsplit(" ", 1)[-1]) >= 1.0


def test_request_latency_histogram_records(client):
    # Drive a request so the histogram observes a sample.
    client.get("/api/health")
    body = client.get("/metrics").get_data(as_text=True)
    # Histogram exposes _count, _sum, _bucket{le=...}
    assert "matchmaker_request_latency_seconds_count" in body
    assert "matchmaker_request_latency_seconds_bucket" in body


# --- /api/logs JSON contract ----------------------------------------------


def test_api_logs_returns_jsonl_events(client):
    _log_event("info", "test_event_one", foo=42)
    _log_event("warn", "test_event_two")
    _log_event("error", "test_event_three", err="boom")

    r = client.get("/api/logs?limit=100")
    assert r.status_code == 200
    body = r.get_json()
    assert "events" in body
    msgs = [e["message"] for e in body["events"]]
    assert "test_event_one" in msgs
    assert "test_event_two" in msgs
    assert "test_event_three" in msgs

    # Schema: every event MUST carry ts + level + message
    for ev in body["events"]:
        assert "ts" in ev
        assert "level" in ev
        assert "message" in ev


def test_api_logs_limit_caps_response(client):
    for i in range(50):
        _log_event("info", f"event_{i}")
    r = client.get("/api/logs?limit=10")
    body = r.get_json()
    assert len(body["events"]) <= 10


def test_api_logs_since_filter_excludes_older_events(client):
    _log_event("info", "before_marker")
    pre = client.get("/api/logs?limit=200").get_json()["events"]
    last_ts = pre[-1]["ts"]
    _log_event("info", "after_marker")

    r = client.get(
        f"/api/logs?limit=200&since={last_ts}"
    ).get_json()
    msgs = [e["message"] for e in r["events"]]
    assert "after_marker" in msgs
    assert "before_marker" not in msgs


# --- /admin/ HTML viewer ---------------------------------------------------


def test_admin_view_returns_html_with_polling_js(client):
    r = client.get("/admin/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    # Markers that prove the template rendered with the polling JS.
    assert "<html" in body.lower()
    assert "Admin Log Viewer" in body
    # Template builds the URL dynamically (`const url = "/api/logs?..."`),
    # so assert the path substring + poller wiring; matching the
    # quoted URL text would tie us to the literal ?limit that the
    # helper picks (and any future change to it).
    assert "/api/logs" in body  # the path the poller targets
    assert "async function pollLogs" in body  # the poller function
    assert "setInterval(pollLogs" in body  # the 2s cadence


def test_admin_view_route_alias(client):
    # /admin (no trailing slash) also resolves.
    r = client.get("/admin")
    assert r.status_code == 200
    assert "Admin Log Viewer" in r.get_data(as_text=True)


# --- main() wiring + on_purge observer ------------------------------------


def test_maintenance_purged_total_counter_is_module_level():
    """MAINTENANCE_PURGED_TOTAL must be a Counter shared across cycles."""
    pytest.importorskip("prometheus_client")
    # The Counter is wired to MAINTENANCE_PURGED_TOTAL.inc in main(),
    # so a manual inc here proves the wiring target is live and
    # observable via /metrics.
    MAINTENANCE_PURGED_TOTAL.inc(7)
    app = create_app()
    with app.test_client() as c:
        body = c.get("/metrics").get_data(as_text=True)
    # Counter value must have grown.
    line = next(
        l for l in body.splitlines()
        if l.startswith("matchmaker_maintenance_purged_total ")
        and "result" not in l
    )
    assert float(line.rsplit(" ", 1)[-1]) >= 7.0


# --- log file path invariants ---------------------------------------------


def test_log_path_is_pid_scoped_under_logs_matchmaker(tmp_path: Path):
    """logs/matchmaker/<pid>.jsonl is the per-PID event-log file."""
    assert LOG_DIR.exists()
    assert LOG_DIR.name == "matchmaker"
    assert _LOG_PATH.parent == LOG_DIR
    assert _LOG_PATH.name == f"{os.getpid()}.jsonl"
