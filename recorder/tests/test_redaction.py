"""Secrets must never reach disk — detector coverage and path logging."""

from __future__ import annotations

import json

from trace_recorder import contract
from trace_recorder.redaction import Redactor


def test_key_name_detectors():
    redactor = Redactor()
    args = {
        "api_key": "abc123",
        "Authorization": "Bearer xyz",
        "nested": {"db_password": "hunter2"},
        "items": [{"token": "t0k3n"}],
        "safe": "value",
    }
    redacted, paths = redactor.redact_args(args)
    assert redacted["api_key"] == "__REDACTED__:api_key"
    assert redacted["Authorization"] == "__REDACTED__:Authorization"
    assert redacted["nested"]["db_password"] == "__REDACTED__:db_password"
    assert redacted["items"][0]["token"] == "__REDACTED__:token"
    assert redacted["safe"] == "value"
    assert set(paths) == {
        "$.api_key",
        "$.Authorization",
        "$.nested.db_password",
        "$.items[0].token",
    }


def test_value_shape_detectors():
    redactor = Redactor()
    args = {
        "note": "use sk-abcdefghijklmnop1234 for the call",
        "gh": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
        "aws": "AKIAIOSFODNN7EXAMPLE",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    }
    redacted, paths = redactor.redact_args(args)
    assert "sk-" not in json.dumps(redacted)
    assert "ghp_" not in json.dumps(redacted)
    assert "AKIA" not in json.dumps(redacted)
    assert redacted["jwt"] == "__REDACTED__:jwt"
    assert len(paths) == 4


def test_connection_string_password_only():
    redactor = Redactor()
    redacted, paths = redactor.redact_args(
        {"dsn": "postgres://svc_user:s3cr3t@db.internal:5432/policies"}
    )
    assert redacted["dsn"] == "postgres://svc_user:__REDACTED__:password@db.internal:5432/policies"
    assert paths == ["$.dsn"]


def test_results_get_key_name_detectors_only():
    redactor = Redactor()
    result = {
        "api_key": "leaked",
        "data": "sk-abcdefghijklmnop1234",  # entropic-looking value: kept in results
    }
    redacted, paths = redactor.redact_result(result)
    assert redacted["api_key"] == "__REDACTED__:api_key"
    assert redacted["data"] == "sk-abcdefghijklmnop1234"
    assert paths == ["$.api_key"]


def test_site_specific_patterns():
    redactor = Redactor(extra_value_patterns=[("acme_badge", r"ACME-[0-9]{6}")])
    redacted, paths = redactor.redact_args({"badge": "ACME-123456"})
    assert redacted["badge"] == "__REDACTED__:acme_badge"
    assert paths == ["$.badge"]


def test_core_applies_redaction_before_write(make_core):
    core = make_core()
    core.record("r1", contract.SESSION_META, {"phase": "start"})
    core.record(
        "r1",
        contract.TOOL_CALL,
        {"call_id": "c-1", "tool": "http__gw__login", "args": {"user": "a", "password": "p"}},
    )
    core.record(
        "r1",
        contract.TOOL_RESULT,
        {"call_id": "c-1", "status": "ok", "result": {"session_token": "tok"}},
    )
    raw = core.trace_path("r1").read_text()
    assert "hunter" not in raw and '"p"' not in raw and '"tok"' not in raw
    events = [json.loads(l) for l in raw.splitlines()]
    assert events[1]["body"]["args_redactions"] == ["$.password"]
    assert events[2]["body"]["result_redactions"] == ["$.session_token"]
