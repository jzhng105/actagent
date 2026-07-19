"""Fail-open event emitter used by adapters to reach the recorder core.

Recording must never break the session. Every emit is wrapped: on any
internal error the client logs one line to stderr, drops the event, and
lets the adapter's traffic proceed. A disabled recorder is the worst
outcome; a crashed agent because of the recorder is worse than that.

Two modes:

- **endpoint mode** — POST events to a running `trace-recorder daemon`
  (``core: { endpoint: "http://127.0.0.1:7717" }`` in adapter config).
  Preferred when multiple adapters observe the same logical session.
- **embedded mode** — no endpoint configured; the adapter process hosts
  the core library in-process. Same single audited write path, suitable
  for single-adapter local postures.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

from ..core import CoreConfig, RecorderCore


class CoreClient:
    def __init__(
        self,
        session_id: str,
        *,
        endpoint: str | None = None,
        core_config: CoreConfig | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.session_id = session_id
        self._endpoint = endpoint.rstrip("/") if endpoint else None
        self._timeout = timeout
        self._core: RecorderCore | None = None
        if self._endpoint is None:
            try:
                self._core = RecorderCore(core_config)
            except Exception as exc:
                self._warn(f"embedded core unavailable: {exc}")

    def emit(self, type: str, body: dict[str, Any]) -> None:
        """Record one event; never raises."""
        try:
            if self._core is not None:
                self._core.record(self.session_id, type, body)
            elif self._endpoint is not None:
                data = json.dumps(
                    {"session_id": self.session_id, "type": type, "body": body}
                ).encode("utf-8")
                req = urllib.request.Request(
                    f"{self._endpoint}/v1/events",
                    data=data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    resp.read()
        except Exception as exc:
            self._warn(f"dropped {type} event: {exc}")

    def end(self, end_reason: str = "normal") -> None:
        self.emit("session_meta", {"phase": "end", "end_reason": end_reason})

    @staticmethod
    def _warn(message: str) -> None:
        print(f"trace-recorder: {message}", file=sys.stderr)
