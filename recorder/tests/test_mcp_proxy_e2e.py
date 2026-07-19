"""End-to-end: a real (fixture) MCP client session through the recording
proxy, exercising the renewal-tracking join scenario from the guidebook —
loops, a large result, a secret argument, a tool failure, a downstream
crash — then the compiler's Phase 1 validation gate on the produced trace.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from trace_recorder.verify import verify_trace

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVER = Path(__file__).resolve().parent / "fixtures" / "fake_mcp_server.py"
SESSION_ID = "e2e-session"


def _make_dbs(tmp_path: Path) -> tuple[Path, Path]:
    legacy = tmp_path / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE policies (policy_no TEXT, expiry TEXT, premium REAL)")
    conn.executemany(
        "INSERT INTO policies VALUES (?,?,?)",
        [("P-100", "2026-06-11", 1200.0), ("P-101", "2026-06-28", 890.0)],
    )
    conn.commit()
    conn.close()

    gw = tmp_path / "gw.db"
    conn = sqlite3.connect(gw)
    conn.execute("CREATE TABLE quotes (policy_no TEXT, status TEXT)")
    conn.executemany(
        "INSERT INTO quotes VALUES (?,?)", [("P-100", "quoted"), ("P-101", "inforce")]
    )
    conn.commit()
    conn.close()
    return legacy, gw


class ProxyClient:
    """Drives the proxy subprocess as an upstream MCP client."""

    def __init__(self, config_path: Path):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "trace_recorder.cli", "mcp-proxy", "--config", str(config_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=PACKAGE_ROOT,
            text=True,
        )
        self._messages: "queue.Queue[dict]" = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._next_id = 0

    def _read_loop(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self._messages.put(json.loads(line))

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        message: dict = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        while True:
            response = self._messages.get(timeout=timeout)
            if response.get("id") == msg_id:
                return response

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        return self.proc.wait(timeout=30)

    def stderr_text(self) -> str:
        assert self.proc.stderr is not None
        return self.proc.stderr.read()


@pytest.fixture
def proxy(tmp_path):
    legacy_db, gw_db = _make_dbs(tmp_path)
    trace_dir = tmp_path / ".traces"
    config = {
        "listen": "stdio",
        "session_id": SESSION_ID,
        "trace_dir": str(trace_dir),
        "downstream": [
            {
                "name": "legacydb",
                "transport": "stdio",
                "command": [sys.executable, str(FAKE_SERVER), "--db", str(legacy_db), "--name", "legacydb"],
            },
            {
                "name": "gw",
                "transport": "stdio",
                "command": [sys.executable, str(FAKE_SERVER), "--db", str(gw_db), "--name", "gw"],
            },
        ],
    }
    config_path = tmp_path / "proxy.json"
    config_path.write_text(json.dumps(config))
    client = ProxyClient(config_path)
    yield client, trace_dir
    if client.proc.poll() is None:
        client.proc.kill()


def test_full_session_records_compile_ready_trace(proxy):
    client, trace_dir = proxy

    init = client.request(
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "e2e-client", "version": "0"},
        },
    )
    assert init["result"]["serverInfo"]["name"] == "trace-recorder-mcp-proxy"
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # Aggregated, canonically-namespaced inventory from both downstreams.
    tools = client.request("tools/list", {})["result"]["tools"]
    names = {t["name"] for t in tools}
    assert "mcp__legacydb__query" in names
    assert "mcp__gw__query" in names

    # Step 1: load expiring policies.
    rows = client.request(
        "tools/call",
        {
            "name": "mcp__legacydb__query",
            "arguments": {"sql": "SELECT policy_no, expiry, premium FROM policies"},
        },
    )["result"]["structuredContent"]["rows"]
    assert len(rows) == 2

    # Step 2: the loop — look up quote status per policy.
    statuses = {}
    for row in rows:
        result = client.request(
            "tools/call",
            {
                "name": "mcp__gw__query",
                "arguments": {
                    "sql": f"SELECT status FROM quotes WHERE policy_no = '{row['policy_no']}'"
                },
            },
        )["result"]
        statuses[row["policy_no"]] = result["structuredContent"]["rows"][0]["status"]
    assert statuses == {"P-100": "quoted", "P-101": "inforce"}

    # A large result (spillover), a secret argument (redaction), a failure.
    big = client.request(
        "tools/call", {"name": "mcp__legacydb__big", "arguments": {"bytes": 100_000}}
    )
    assert "result" in big
    client.request(
        "tools/call",
        {"name": "mcp__gw__login", "arguments": {"username": "svc", "password": "hunter2"}},
    )
    failed = client.request("tools/call", {"name": "mcp__legacydb__fail", "arguments": {}})
    assert failed["result"]["isError"] is True

    # Unknown tool → actionable error, nothing recorded for it.
    unknown = client.request("tools/call", {"name": "mcp__nope__x", "arguments": {}})
    assert unknown["error"]["code"] == -32602
    assert "available tools" in unknown["error"]["message"]

    assert client.close() == 0

    # ---- The compile gate: Phase 1 validation on the produced trace ----
    trace_path = trace_dir / f"{SESSION_ID}.jsonl"
    report = verify_trace(trace_path)
    assert report.ok, report.summary_lines()
    assert report.end_reason == "normal"

    events = [json.loads(l) for l in trace_path.read_text().splitlines()]
    meta = events[0]
    assert meta["type"] == "session_meta"
    assert meta["body"]["producer"]["adapter"] == "mcp-recording-proxy"
    assert "mcp__legacydb__query" in meta["body"]["tool_inventory"]

    calls = [e for e in events if e["type"] == "tool_call"]
    results = {e["body"]["call_id"]: e for e in events if e["type"] == "tool_result"}
    # 1 policies query + 2 loop lookups + big + login + fail = 6 recorded calls.
    assert len(calls) == 6
    assert all(c["body"]["call_id"] in results for c in calls)
    assert all(c["body"]["tool"].startswith("mcp__") for c in calls)

    # Wire-level timing was captured.
    assert all(isinstance(r["body"]["duration_ms"], int) for r in results.values())

    # Redaction: the password never reached disk.
    raw = trace_path.read_text()
    assert "hunter2" not in raw
    login_call = next(c for c in calls if c["body"]["tool"] == "mcp__gw__login")
    assert login_call["body"]["args"]["password"] == "__REDACTED__:password"
    assert login_call["body"]["args_redactions"] == ["$.password"]

    # Spillover: big result truncated inline, full payload on disk, hash matches.
    big_call = next(c for c in calls if c["body"]["tool"] == "mcp__legacydb__big")
    big_result = results[big_call["body"]["call_id"]]["body"]
    assert big_result["result_truncated"] is True
    spill = trace_dir / "spillover" / SESSION_ID / f"{big_call['body']['call_id']}.json"
    assert spill.exists()

    # The failed tool call was recorded as an error with the raw payload.
    fail_call = next(c for c in calls if c["body"]["tool"] == "mcp__legacydb__fail")
    fail_result = results[fail_call["body"]["call_id"]]["body"]
    assert fail_result["status"] == "error"
    assert fail_result["error"]["isError"] is True


def test_downstream_crash_fails_open(proxy):
    client, trace_dir = proxy
    client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "x", "version": "0"}})
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    crashed = client.request("tools/call", {"name": "mcp__gw__die", "arguments": {}})
    assert "error" in crashed  # downstream exited; upstream got a JSON-RPC error

    # The proxy survives and the other downstream still works.
    ok = client.request(
        "tools/call",
        {"name": "mcp__legacydb__query", "arguments": {"sql": "SELECT 1 AS one"}},
    )
    assert ok["result"]["structuredContent"]["rows"] == [{"one": 1}]

    assert client.close() == 0
    report = verify_trace(trace_dir / f"{SESSION_ID}.jsonl")
    assert report.ok, report.summary_lines()
    events = [json.loads(l) for l in (trace_dir / f"{SESSION_ID}.jsonl").read_text().splitlines()]
    die_result = next(
        e for e in events
        if e["type"] == "tool_result"
        and e["body"]["call_id"] == next(
            c["body"]["call_id"] for c in events
            if c["type"] == "tool_call" and c["body"]["tool"] == "mcp__gw__die"
        )
    )
    assert die_result["body"]["status"] == "error"
