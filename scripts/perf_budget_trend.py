#!/usr/bin/env python3
"""Build the perf-budget regression trend table from the last 30
nightly `integration-junit` artifacts emitted by
`.github/workflows/ci.yml`.

Invoked by the `build perf-budget trend` step in the `integration`
job on `schedule` events.  Downloads each prior-artifact ZIP,
unzips the embedded `junit-integration.xml` in memory, extracts the
`perf_budget_guard` record_property that
`tests/test_multi_client_integration.py::test_many_clients_heartbeat`
emitted, and writes a markdown table to `$GITHUB_STEP_SUMMARY` so
the nightly run's GitHub Actions UI surfaces a one-shot trend.

Required env (auto-provisioned in workflows):
- GH_TOKEN or GITHUB_TOKEN  : token with `actions:read` on this repo;
  `${{ secrets.GITHUB_TOKEN }}` is fine.
- GH_REPO or GITHUB_REPOSITORY : `owner/repo` slug.

Falls back to printing the markdown to stdout when
`$GITHUB_STEP_SUMMARY` is unset (e.g. local dry-run).
"""
import os
import sys
import io
import json
import zipfile
import ast
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from urllib.parse import urlencode


REPO = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY", "")
TOK = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
BASE = "https://api.github.com/repos/" + REPO


def gh(path, **params):
    """GET https://api.github.com/repos/<repo>/<path>.  Returns None on
    HTTP error so the caller can skip that run silently."""
    url = BASE + path + ("?" + urlencode(params) if params else "")
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + TOK,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ct = r.headers.get("Content-Type", "")
            if ct.startswith("application/json"):
                return json.load(r)
            return r.read()
    except (urllib.error.URLError, OSError) as e:
        # Broadened from HTTPError-only so transient network
        # failures (DNS, connection reset, timeout) yield a soft
        # skip instead of crashing the whole trend table.
        # `urllib.error.HTTPError` is a subclass of `URLError` so
        # 4xx/5xx responses are still caught here.
        print(
            "  warn: GitHub API %s on %s: %s"
            % (type(e).__name__, path, e),
            file=sys.stderr,
        )
        return None


def _scrape_perf_from_run(run):
    """Download the run's `integration-junit` artifact ZIP and pull
    the `perf_budget_guard` dict out of the embedded JUnit XML.
    Returns a row dict suitable for the trend table, or None if the
    run has no usable artifact yet."""
    arts = gh("/actions/runs/%d/artifacts" % run["id"]) or {}
    art = next(
        (
            a
            for a in arts.get("artifacts", [])
            if a.get("name") == "integration-junit"
        ),
        None,
    )
    if not art or art.get("expired"):
        return None
    try:
        with urllib.request.urlopen(art["archive_download_url"], timeout=30) as r:
            zbytes = r.read()
    except (urllib.error.URLError, OSError) as e:
        print(
            "  warn: download artifact %s: %s"
            % (type(e).__name__, e),
            file=sys.stderr,
        )
        return None
    xml_name = None
    try:
        with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
            for n in zf.namelist():
                if n.endswith("junit-integration.xml"):
                    xml_name = n
                    break
            if not xml_name:
                return None
            xml_bytes = zf.read(xml_name)
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        # BadZipFile: truncated/corrupted archive (rare but happens
        #   when GH uploads are mid-flight or storage is flaky).
        # KeyError: zf.read(xml_name) races with zipfile iteration
        #   on weird zip layouts where the entry is iterated but
        #   missing on lookup.
        # OSError: disk-level read fault on Windows runners.
        print(
            "  warn: artifact zip read failed (%s): %s"
            % (type(e).__name__, e),
            file=sys.stderr,
        )
        return None
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    perf_value = next(
        (
            p.get("value")
            for p in root.iter("property")
            if p.get("name") == "perf_budget_guard"
        ),
        None,
    )
    if perf_value is None:
        return None
    perf = None
    try:
        perf = ast.literal_eval(perf_value)
    except (ValueError, SyntaxError):
        try:
            # Fallback: pytest's record_property may serialize the
            # dict via json.dumps on some runners, in which case the
            # value is a JSON-encoded object literal (not a Python
            # repr). Try json.loads as a second pass before giving up.
            perf = json.loads(perf_value)
        except (ValueError, SyntaxError) as e:
            print(
                "  warn: perf_budget_guard parse failed: %s" % e,
                file=sys.stderr,
            )
            return None
    return {
        "created_at": run["created_at"],
        "n_stubs": perf.get("n_stubs"),
        "heartbeat_p95_ms": perf.get("heartbeat_p95_ms"),
        "list_servers_ms": perf.get("list_servers_ms"),
    }


def _render_markdown(rows):
    """Format the sorted rows into the GH Actions step-summary markdown."""
    def fmt(x):
        if x is None:
            return "n/a"
        if isinstance(x, float):
            return "%.1f" % x
        return str(x)

    lines = ["## Perf-budget trend (last %d nightly runs)" % len(rows), ""]
    if not rows:
        lines.append(
            "_No prior nightly runs with valid `integration-junit` artifacts yet._"
        )
        return "\n".join(lines) + "\n"
    lines.append("| Date (UTC) | N | hb p95 (ms) | list_servers (ms) |")
    lines.append("| --- | --- | --- | --- |")
    for r in sorted(rows, key=lambda x: x["created_at"], reverse=True):
        lines.append(
            "| %s | %s | %s | %s |"
            % (
                r["created_at"],
                fmt(r["n_stubs"]),
                fmt(r["heartbeat_p95_ms"]),
                fmt(r["list_servers_ms"]),
            )
        )
    return "\n".join(lines) + "\n"


def main():
    runs = (
        gh(
            "/actions/runs",
            workflow_file="ci.yml",
            branch="main",
            event="schedule",
            status="completed",
            per_page=30,
        )
        or {}
    ).get("workflow_runs", [])
    print("  info: scanning %d nightly runs" % len(runs), file=sys.stderr)

    rows = []
    for run in runs:
        row = _scrape_perf_from_run(run)
        if row is not None:
            rows.append(row)

    md = _render_markdown(rows)
    out = os.environ.get("GITHUB_STEP_SUMMARY")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(md)
        print(
            "  info: appended %d rows to $GITHUB_STEP_SUMMARY" % len(rows),
            file=sys.stderr,
        )
    else:
        sys.stdout.write(md)


if __name__ == "__main__":
    main()
