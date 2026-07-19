"""Contract tests are the spine: synthetic adapter input → golden trace.

The golden file pins the exact wire format — envelope fields, hash chain
values, redaction output, spillover markers. Any change that alters these
bytes is a versioned contract change and must be made deliberately: if
this test fails, either revert the change or bump the trace schema
version AND coordinate with the workflow-compiler skill, then regenerate
with REGEN_GOLDEN=1 pytest tests/test_contract_golden.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from trace_recorder import contract

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "synthetic-session.jsonl"

SESSION = "golden-session"

# Synthetic adapter input: the event stream an adapter would emit for a
# small two-call task, including a secret argument and a reasoning note.
SCRIPT = [
    (
        contract.SESSION_META,
        {
            "phase": "start",
            "producer": {
                "adapter": "mcp-recording-proxy",
                "adapter_version": "0.2.0",
                "host": "test-harness",
                "host_version": "0",
                "model": "none",
            },
            "cwd": "/work/renewal-tracking",
            "user_prompt_sha256": "0" * 64,
            "tool_inventory": ["mcp__legacydb__query", "mcp__gw__quote_lookup"],
        },
    ),
    (
        contract.TOOL_CALL,
        {
            "call_id": "c-1",
            "tool": "mcp__legacydb__query",
            "args": {
                "sql": "SELECT policy_no, expiry FROM policies WHERE expiry BETWEEN :s AND :e",
                "bind": {"s": "2026-06-01", "e": "2026-06-30"},
                "api_key": "sk-abcdefghijklmnop1234",
            },
            "args_redactions": [],
        },
    ),
    (
        contract.TOOL_RESULT,
        {
            "call_id": "c-1",
            "status": "ok",
            "duration_ms": 412,
            "result": {
                "columns": ["policy_no", "expiry"],
                "rows": [["P-100", "2026-06-11"], ["P-101", "2026-06-28"]],
            },
            "error": None,
        },
    ),
    (
        contract.REASONING_NOTE,
        {
            "near_call_id": "c-2",
            "text": "Legacy DB uses fiscal expiry; widening by the ±7-day tolerance",
        },
    ),
    (
        contract.TOOL_CALL,
        {
            "call_id": "c-2",
            "tool": "mcp__gw__quote_lookup",
            "args": {"policy_no": "P-100"},
            "args_redactions": [],
        },
    ),
    (
        contract.TOOL_RESULT,
        {
            "call_id": "c-2",
            "status": "error",
            "duration_ms": 88,
            "result": None,
            "error": {"code": -32000, "message": "quote system timeout"},
        },
    ),
    (contract.SESSION_META, {"phase": "end", "end_reason": "normal"}),
]


def produce(make_core) -> str:
    core = make_core()
    for etype, body in SCRIPT:
        core.record(SESSION, etype, body)
    return core.trace_path(SESSION).read_text()


def test_golden_trace(make_core):
    produced = produce(make_core)
    if os.environ.get("REGEN_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(produced)
    assert GOLDEN.exists(), "golden missing; run with REGEN_GOLDEN=1 to create"
    assert produced == GOLDEN.read_text()


def test_golden_trace_properties(make_core):
    """Redundant with the byte comparison, but explains the contract."""
    events = [json.loads(l) for l in produce(make_core).splitlines()]
    assert [e["type"] for e in events] == [
        "session_meta",
        "tool_call",
        "tool_result",
        "reasoning_note",
        "tool_call",
        "tool_result",
        "session_meta",
    ]
    # The secret was redacted and its path logged.
    call1 = events[1]["body"]
    assert call1["args"]["api_key"] == "__REDACTED__:api_key"
    assert call1["args_redactions"] == ["$.api_key"]
    # Error results keep the raw error payload.
    assert events[5]["body"]["error"]["message"] == "quote system timeout"
    # Chain verifies.
    for e in events:
        assert contract.check_event_hash(e)
