"""The trace contract (the IR).

This module is normative: recorder core and workflow-compiler must agree
exactly on everything here, so any change is a versioned contract change.

Each trace line is one JSON object with a common envelope:

    {
      "v": 1,
      "seq": 42,
      "ts": "2026-07-18T15:04:22.117-04:00",
      "session_id": "8f3a9c2e",
      "type": "tool_call",
      "prev_hash": "sha256:...",
      "hash": "sha256:...",
      "body": { ... }
    }

`seq` is strictly increasing within a session — gaps signal dropped
events. `prev_hash`/`hash` form the per-session tamper-evidence chain.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

TRACE_SCHEMA_VERSION = 1

# Event types
SESSION_META = "session_meta"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
REASONING_NOTE = "reasoning_note"

EVENT_TYPES = frozenset({SESSION_META, TOOL_CALL, TOOL_RESULT, REASONING_NOTE})

# session_meta end reasons
END_NORMAL = "normal"
END_INTERRUPTED = "interrupted"
END_ERROR = "error"
END_REASONS = frozenset({END_NORMAL, END_INTERRUPTED, END_ERROR})


def canonical_json(obj: Any) -> str:
    """Deterministic serialization used for hashing and byte counts."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_tag(data: bytes) -> str:
    return "sha256:" + sha256_hex(data)


def genesis_hash(session_id: str) -> str:
    """The chain's genesis marker: derived from the session id alone so any
    verifier can recompute it from the trace file's own first line."""
    return sha256_tag(f"genesis:{session_id}".encode("utf-8"))


def event_hash(event_without_hash: dict[str, Any]) -> str:
    """Hash covering the event's own content including `prev_hash`.

    `event_without_hash` must be the full envelope minus the `hash` key.
    """
    return sha256_tag(canonical_json(event_without_hash).encode("utf-8"))


def make_event(
    *,
    seq: int,
    ts: str,
    session_id: str,
    type: str,
    prev_hash: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Build a fully-enveloped, hash-chained trace event."""
    if type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {type!r}")
    event: dict[str, Any] = {
        "v": TRACE_SCHEMA_VERSION,
        "seq": seq,
        "ts": ts,
        "session_id": session_id,
        "type": type,
        "prev_hash": prev_hash,
        "body": body,
    }
    event["hash"] = event_hash(event)
    return event


def check_event_hash(event: dict[str, Any]) -> bool:
    """Recompute an event's hash and compare with the recorded one."""
    recorded = event.get("hash")
    stripped = {k: v for k, v in event.items() if k != "hash"}
    return recorded == event_hash(stripped)
