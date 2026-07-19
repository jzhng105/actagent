"""Fail-open everywhere: agent traffic proceeds, stderr explains.

An adapter that crashes, blocks, or slows the agent gets disabled, and a
disabled recorder is the worst outcome.
"""

from __future__ import annotations

import json

from trace_recorder.adapters.core_client import CoreClient
from trace_recorder.core import CoreConfig


def test_emit_to_unreachable_endpoint_does_not_raise(capsys):
    client = CoreClient("s1", endpoint="http://127.0.0.1:1", timeout=0.5)
    client.emit("tool_call", {"call_id": "c-1", "tool": "t", "args": {}})
    client.end()
    err = capsys.readouterr().err
    assert "dropped tool_call event" in err
    assert "dropped session_meta event" in err


def test_embedded_core_in_unwritable_dir_fails_open(tmp_path, capsys):
    # A regular file where the trace dir's parent should be: mkdir fails for
    # any user (chmod tricks don't block root, which CI may run as).
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    client = CoreClient(
        "s2", core_config=CoreConfig(trace_dir=blocked / ".traces", fsync=False)
    )
    client.emit("tool_call", {"call_id": "c-1", "tool": "t", "args": {}})
    err = capsys.readouterr().err
    assert "trace-recorder:" in err


def test_malformed_event_type_is_dropped_not_raised(tmp_path, capsys):
    client = CoreClient("s3", core_config=CoreConfig(trace_dir=tmp_path / ".traces", fsync=False))
    client.emit("not_a_real_type", {"x": 1})
    assert "dropped not_a_real_type event" in capsys.readouterr().err
    # The good path still works afterwards.
    client.emit("session_meta", {"phase": "start"})
    client.end()
    trace = (tmp_path / ".traces" / "s3.jsonl").read_text()
    events = [json.loads(l) for l in trace.splitlines()]
    assert [e["type"] for e in events] == ["session_meta", "session_meta"]
