"""The daemon: loopback HTTP sink for adapter events.

Adapters emit contract-shaped events to ``POST /v1/events``; the core is
the single writer behind it. Startup runs interrupted-session recovery so
traces from crashed processes get their definitive end record.

Request body (single event per POST):

    { "session_id": "...", "type": "tool_call", "body": { ... } }

`session_id` may be omitted; the daemon then uses (and returns) a minted
one — but adapters observing the same logical session must share the id
(propagate the host's, or set TRACE_SESSION_ID in the launching
environment).
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import CoreConfig, RecorderCore, mint_session_id, recover_interrupted

DEFAULT_PORT = 7717
MAX_BODY_BYTES = 64 * 1024 * 1024


class _Handler(BaseHTTPRequestHandler):
    core: RecorderCore  # set by serve()

    # Silence default request logging to stderr noise; log errors only.
    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _reply(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._reply(200, {"ok": True, "active_sessions": self.core.active_sessions()})
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/events":
            self._reply(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_BODY_BYTES:
                self._reply(400, {"ok": False, "error": "bad content length"})
                return
            payload = json.loads(self.rfile.read(length))
            session_id = payload.get("session_id") or mint_session_id()
            event = self.core.record(
                session_id=str(session_id),
                type=str(payload["type"]),
                body=payload.get("body") or {},
            )
            self._reply(200, {"ok": True, "session_id": session_id, "seq": event["seq"]})
        except Exception as exc:  # fail open at the boundary: report, never crash
            print(f"trace-recorder daemon: event rejected: {exc}", file=sys.stderr)
            self._reply(500, {"ok": False, "error": str(exc)})


def serve(
    port: int = DEFAULT_PORT,
    config: CoreConfig | None = None,
    *,
    ready: threading.Event | None = None,
) -> None:
    config = config or CoreConfig()
    closed = recover_interrupted(config.trace_dir)
    for session_id in closed:
        print(f"trace-recorder daemon: closed interrupted session {session_id}", file=sys.stderr)
    core = RecorderCore(config)

    class Handler(_Handler):
        pass

    Handler.core = core
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"trace-recorder daemon: listening on http://127.0.0.1:{port}", file=sys.stderr)
    if ready is not None:
        ready.set()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        core.close(end_reason="interrupted")
