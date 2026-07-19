"""Fixture downstream MCP server (stdlib-only, stdio, newline-delimited JSON-RPC).

Used by the recorder test suite as the "real" tool source behind the
recording proxy. Backed by an optional SQLite database so the end-to-end
test can exercise the renewal-tracking join scenario from the guidebook.

Tools:
    query      {sql}                -> rows from the sqlite db
    echo       {text}               -> echoes text
    big        {bytes}              -> a payload of roughly `bytes` size
    fail       {}                   -> tool-level error (isError result)
    login      {username, password} -> exercises argument redaction
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from typing import Any


def _reply(msg_id: Any, result: dict[str, Any]) -> None:
    _send({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


TOOLS = [
    {
        "name": "query",
        "description": "Run a read-only SQL query against the fixture database",
        "inputSchema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
        },
    },
    {
        "name": "echo",
        "description": "Echo text back",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "big",
        "description": "Return a large payload",
        "inputSchema": {
            "type": "object",
            "properties": {"bytes": {"type": "integer"}},
            "required": ["bytes"],
        },
    },
    {
        "name": "fail",
        "description": "Always fails",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "die",
        "description": "Exit the server process immediately (crash simulation)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "login",
        "description": "Pretend to authenticate",
        "inputSchema": {
            "type": "object",
            "properties": {"username": {"type": "string"}, "password": {"type": "string"}},
            "required": ["username", "password"],
        },
    },
]


def call_tool(name: str, args: dict[str, Any], db_path: str | None) -> dict[str, Any]:
    if name == "query":
        if not db_path:
            return {"content": [{"type": "text", "text": "no database configured"}], "isError": True}
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.execute(args["sql"])
            columns = [c[0] for c in cursor.description or []]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            conn.close()
        return {
            "content": [{"type": "text", "text": f"{len(rows)} row(s)"}],
            "structuredContent": {"columns": columns, "rows": rows},
        }
    if name == "echo":
        return {"content": [{"type": "text", "text": str(args.get("text", ""))}]}
    if name == "big":
        n = int(args.get("bytes", 1024))
        return {
            "content": [{"type": "text", "text": "big payload attached"}],
            "structuredContent": {"blob": "x" * n},
        }
    if name == "fail":
        return {"content": [{"type": "text", "text": "deliberate failure"}], "isError": True}
    if name == "die":
        import os

        os._exit(1)
    if name == "login":
        return {"content": [{"type": "text", "text": f"logged in as {args.get('username')}"}]}
    return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=None)
    parser.add_argument("--name", default="fake")
    opts = parser.parse_args()

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        msg_id = message.get("id")
        if method == "initialize":
            _reply(
                msg_id,
                {
                    "protocolVersion": (message.get("params") or {}).get(
                        "protocolVersion", "2025-06-18"
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": opts.name, "version": "1.0.0"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            params = message.get("params") or {}
            result = call_tool(
                str(params.get("name")), params.get("arguments") or {}, opts.db
            )
            _reply(msg_id, result)
        elif method == "ping":
            _reply(msg_id, {})
        elif msg_id is not None:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
