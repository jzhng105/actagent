"""Core invariants: envelope, sequencing, chaining, spillover, lifecycle."""

from __future__ import annotations

import json

from trace_recorder import contract
from trace_recorder.core import mint_session_id, recover_interrupted
from trace_recorder.verify import verify_trace


def read_trace(core, session_id):
    lines = core.trace_path(session_id).read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_envelope_and_sequencing(make_core):
    core = make_core()
    core.record("s1", contract.SESSION_META, {"phase": "start", "producer": {"adapter": "test"}})
    core.record("s1", contract.TOOL_CALL, {"call_id": "c-1", "tool": "mcp__db__query", "args": {"q": 1}})
    core.record("s1", contract.TOOL_RESULT, {"call_id": "c-1", "status": "ok", "result": {"rows": []}})
    core.end_session("s1")

    events = read_trace(core, "s1")
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert all(e["v"] == contract.TRACE_SCHEMA_VERSION for e in events)
    assert all(e["session_id"] == "s1" for e in events)
    assert events[0]["type"] == "session_meta" and events[0]["body"]["phase"] == "start"
    assert events[-1]["body"] == {"phase": "end", "end_reason": "normal"}
    # Chain links
    assert events[0]["prev_hash"] == contract.genesis_hash("s1")
    for prev, cur in zip(events, events[1:]):
        assert cur["prev_hash"] == prev["hash"]
    for e in events:
        assert contract.check_event_hash(e)


def test_first_event_synthesizes_session_meta(make_core):
    core = make_core()
    core.record("s2", contract.TOOL_CALL, {"call_id": "c-1", "tool": "shell.run", "args": {"cmd": "ls"}})
    events = read_trace(core, "s2")
    assert events[0]["type"] == "session_meta"
    assert events[0]["body"]["phase"] == "start"
    assert events[1]["type"] == "tool_call"


def test_end_session_idempotent_and_closed(make_core):
    core = make_core()
    core.record("s3", contract.SESSION_META, {"phase": "start"})
    assert core.end_session("s3") is not None
    assert core.end_session("s3") is None
    try:
        core.record("s3", contract.TOOL_CALL, {"call_id": "x", "tool": "t", "args": {}})
        assert False, "expected RuntimeError on ended session"
    except RuntimeError:
        pass


def test_verify_ok_and_tamper_detection(make_core, tmp_path):
    core = make_core()
    core.record("s4", contract.SESSION_META, {"phase": "start"})
    core.record("s4", contract.TOOL_CALL, {"call_id": "c-1", "tool": "t", "args": {"a": 1}})
    core.record("s4", contract.TOOL_RESULT, {"call_id": "c-1", "status": "ok", "result": {}})
    core.end_session("s4")
    path = core.trace_path("s4")

    report = verify_trace(path)
    assert report.ok and report.chain_ok and not report.seq_gaps

    # Tamper with an argument value in event 2 → chain must diverge there.
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["body"]["args"]["a"] = 999
    lines[1] = json.dumps(tampered, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    report = verify_trace(path)
    assert not report.chain_ok
    assert report.first_divergence_seq == 2


def test_verify_reports_seq_gap_and_unpaired_calls(make_core):
    core = make_core()
    core.record("s5", contract.SESSION_META, {"phase": "start"})
    core.record("s5", contract.TOOL_CALL, {"call_id": "c-9", "tool": "t", "args": {}})
    core.record("s5", contract.TOOL_RESULT, {"call_id": "c-9", "status": "ok", "result": {}})
    core.end_session("s5")
    path = core.trace_path("s5")

    # Drop the tool_result line: a seq gap AND an unpaired call.
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[1], lines[3]]) + "\n")
    report = verify_trace(path)
    assert report.seq_gaps == [(2, 4)]
    assert report.unpaired_call_ids == ["c-9"]
    assert not report.ok


def test_spillover_for_large_results(make_core):
    core = make_core(inline_result_bytes=256)
    big = {"blob": "x" * 5000}
    core.record("s6", contract.SESSION_META, {"phase": "start"})
    core.record("s6", contract.TOOL_CALL, {"call_id": "c-1", "tool": "t", "args": {}})
    core.record("s6", contract.TOOL_RESULT, {"call_id": "c-1", "status": "ok", "result": big})
    core.end_session("s6")

    events = read_trace(core, "s6")
    result_event = events[2]["body"]
    assert result_event["result_truncated"] is True
    assert result_event["result_bytes"] > 256
    assert "head_sample" in result_event["result"]

    spill = core.spillover_path("s6", "c-1")
    assert spill.exists()
    full = spill.read_bytes()
    assert json.loads(full) == big
    assert result_event["result_sha256"] == contract.sha256_hex(full)


def test_small_results_inline_with_sha(make_core):
    core = make_core()
    core.record("s7", contract.SESSION_META, {"phase": "start"})
    core.record("s7", contract.TOOL_RESULT, {"call_id": "c-1", "status": "ok", "result": {"n": 1}})
    events = read_trace(core, "s7")
    body = events[1]["body"]
    assert body["result"] == {"n": 1}
    assert body["result_truncated"] is False
    assert body["result_sha256"] == contract.sha256_hex(
        contract.canonical_json({"n": 1}).encode()
    )


def test_recover_interrupted_appends_end_record(make_core, fixed_clock, tmp_path):
    core = make_core()
    core.record("s8", contract.SESSION_META, {"phase": "start"})
    core.record("s8", contract.TOOL_CALL, {"call_id": "c-1", "tool": "t", "args": {}})
    # No end record: simulate process death, then a fresh core startup.
    trace_dir = core.config.trace_dir
    closed = recover_interrupted(trace_dir, now_fn=fixed_clock)
    assert closed == ["s8"]

    report = verify_trace(core.trace_path("s8"))
    assert report.chain_ok
    assert report.end_reason == "interrupted"
    # Idempotent: a second recovery pass closes nothing.
    assert recover_interrupted(trace_dir, now_fn=fixed_clock) == []


def test_recover_truncates_partial_last_line(make_core, fixed_clock):
    core = make_core()
    core.record("s9", contract.SESSION_META, {"phase": "start"})
    core.record("s9", contract.TOOL_CALL, {"call_id": "c-1", "tool": "t", "args": {}})
    path = core.trace_path("s9")
    with open(path, "a") as fh:
        fh.write('{"v":1,"seq":3,"ts":"2026-')  # torn write

    recover_interrupted(core.config.trace_dir, now_fn=fixed_clock)
    report = verify_trace(path)
    assert report.chain_ok
    assert report.end_reason == "interrupted"
    assert not report.seq_gaps


def test_resume_existing_trace_continues_chain(make_core, tmp_path, fixed_clock):
    core = make_core()
    core.record("s10", contract.SESSION_META, {"phase": "start"})
    core.record("s10", contract.TOOL_CALL, {"call_id": "c-1", "tool": "t", "args": {}})
    # New core instance (daemon restart) appends to the same session file.
    core2 = make_core()
    core2.record("s10", contract.TOOL_RESULT, {"call_id": "c-1", "status": "ok", "result": {}})
    core2.end_session("s10")
    report = verify_trace(core.trace_path("s10"))
    assert report.chain_ok and not report.seq_gaps
    assert report.end_reason == "normal"


def test_resumed_core_can_write_end_record_as_first_event(make_core):
    core = make_core()
    core.record("s11", contract.SESSION_META, {"phase": "start"})
    core.record("s11", contract.TOOL_CALL, {"call_id": "c-1", "tool": "t", "args": {}})
    # Restarted core's very first event for the session is the end record.
    core2 = make_core()
    assert core2.end_session("s11") is not None
    assert core2.end_session("s11") is None  # idempotent across the restart too
    report = verify_trace(core.trace_path("s11"))
    assert report.chain_ok and report.end_reason == "normal"
    # Ending a session that never existed writes nothing.
    assert core2.end_session("never-existed") is None
    assert not core2.trace_path("never-existed").exists()


def test_mint_session_id_shape():
    sid = mint_session_id()
    date_part, hash_part = sid.rsplit("-", 1)
    assert len(hash_part) == 8
    assert len(date_part.split("-")) == 3
