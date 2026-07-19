"""The compile nag (`status`) and the loopback HTTP sink (`daemon`)."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request

from trace_recorder import contract
from trace_recorder.cli import main as cli_main
from trace_recorder.status import scan_status


def _make_trace(make_core, session_id):
    core = make_core()
    core.record(session_id, contract.SESSION_META, {"phase": "start"})
    core.end_session(session_id)
    return core


def test_status_nags_on_uncompiled_trace(make_core, tmp_path):
    core = _make_trace(make_core, "n1")
    report = scan_status(core.config.trace_dir, tmp_path)
    assert [s.session_id for s in report.uncompiled] == ["n1"]

    # Compiling (a newer *.workflow.yaml appearing anywhere under the root)
    # clears the nag.
    workflow = tmp_path / "workflows" / "n1.workflow.yaml"
    workflow.parent.mkdir()
    workflow.write_text("schema_version: 1\n")
    future = time.time() + 5
    os.utime(workflow, (future, future))
    report = scan_status(core.config.trace_dir, tmp_path)
    assert report.uncompiled == []


def test_status_cli_exit_codes(make_core, tmp_path, capsys):
    core = _make_trace(make_core, "n2")
    trace_dir = str(core.config.trace_dir)
    assert cli_main(["status", "--trace-dir", trace_dir, "--workflow-root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "UNCOMPILED" in out

    workflow = tmp_path / "done.workflow.yaml"
    workflow.write_text("schema_version: 1\n")
    future = time.time() + 5
    os.utime(workflow, (future, future))
    assert cli_main(["status", "--trace-dir", trace_dir, "--workflow-root", str(tmp_path)]) == 0


def test_verify_cli_exit_codes(make_core, tmp_path, capsys):
    core = _make_trace(make_core, "n3")
    path = core.trace_path("n3")
    assert cli_main(["verify", str(path)]) == 0
    # Tamper → non-zero.
    lines = path.read_text().splitlines()
    event = json.loads(lines[0])
    event["body"]["phase"] = "start-tampered"
    lines[0] = json.dumps(event, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    assert cli_main(["verify", str(path)]) == 1
    assert "DIVERGED" in capsys.readouterr().out


def test_daemon_accepts_events_and_recovers(tmp_path):
    from trace_recorder.core import CoreConfig, RecorderCore
    from trace_recorder.daemon import serve

    trace_dir = tmp_path / ".traces"
    # Leave an interrupted session behind for startup recovery.
    stale = RecorderCore(CoreConfig(trace_dir=trace_dir, fsync=False))
    stale.record("stale", contract.SESSION_META, {"phase": "start"})

    port = 7891
    ready = threading.Event()
    thread = threading.Thread(
        target=serve,
        kwargs={"port": port, "config": CoreConfig(trace_dir=trace_dir, fsync=False), "ready": ready},
        daemon=True,
    )
    thread.start()
    assert ready.wait(10)

    def post(payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/events",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())

    assert post({"session_id": "d1", "type": "session_meta", "body": {"phase": "start"}})["ok"]
    assert post(
        {
            "session_id": "d1",
            "type": "tool_call",
            "body": {"call_id": "c-1", "tool": "shell.run", "args": {"cmd": "ls"}},
        }
    )["seq"] == 2
    assert post(
        {
            "session_id": "d1",
            "type": "tool_result",
            "body": {"call_id": "c-1", "status": "ok", "result": {"files": []}},
        }
    )["ok"]
    assert post(
        {"session_id": "d1", "type": "session_meta", "body": {"phase": "end", "end_reason": "normal"}}
    )["ok"]

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
        health = json.loads(resp.read())
    assert health["ok"]

    from trace_recorder.verify import verify_trace

    report = verify_trace(trace_dir / "d1.jsonl")
    assert report.ok

    # The stale session got its definitive interrupted end record.
    stale_report = verify_trace(trace_dir / "stale.jsonl")
    assert stale_report.end_reason == "interrupted"
