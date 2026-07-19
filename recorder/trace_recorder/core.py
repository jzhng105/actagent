"""Recorder core: the single writer that owns every contract guarantee.

Adapters never write trace files directly; they emit contract-shaped
events to this core (in-process, or via the daemon's ``POST /v1/events``)
and the core serializes them into append-only JSONL, one write plus flush
per event, with sequencing, hash chaining, redaction, spillover, and
session lifecycle handled in exactly one audited codebase.

Recording must never break the session: callers that cannot tolerate
failure should go through ``adapters.core_client.CoreClient``, which
fails open. The core itself raises on internal errors so that tests and
the daemon can see them; adapters are the fail-open boundary.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets as _secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import contract
from .redaction import Redactor

DEFAULT_TRACE_DIR = Path(".traces")
DEFAULT_INLINE_RESULT_BYTES = 32 * 1024
HEAD_SAMPLE_BYTES = 2 * 1024


def _default_now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def mint_session_id(now: _dt.date | None = None) -> str:
    date = (now or _dt.date.today()).isoformat()
    return f"{date}-{_secrets.token_hex(4)}"


@dataclass
class CoreConfig:
    trace_dir: Path = DEFAULT_TRACE_DIR
    inline_result_bytes: int = DEFAULT_INLINE_RESULT_BYTES
    # Site-specific (name, regex) value patterns added to the built-ins.
    extra_redaction_patterns: list[tuple[str, str]] = field(default_factory=list)
    # fsync after every event. Durable by default; tests may disable.
    fsync: bool = True


@dataclass
class _SessionState:
    seq: int
    prev_hash: str
    ended: bool = False


class RecorderCore:
    """Single-writer trace sink for one trace directory."""

    def __init__(
        self,
        config: CoreConfig | None = None,
        *,
        now_fn: Callable[[], str] | None = None,
    ) -> None:
        self.config = config or CoreConfig()
        self._now = now_fn or _default_now
        self._lock = threading.Lock()
        self._sessions: dict[str, _SessionState] = {}
        self._redactor = Redactor(self.config.extra_redaction_patterns)
        self.config.trace_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths

    def trace_path(self, session_id: str) -> Path:
        return self.config.trace_dir / f"{session_id}.jsonl"

    def spillover_path(self, session_id: str, call_id: str) -> Path:
        return self.config.trace_dir / "spillover" / session_id / f"{call_id}.json"

    # ------------------------------------------------------------- public API

    def record(self, session_id: str, type: str, body: dict[str, Any]) -> dict[str, Any]:
        """Append one event. Applies redaction and spillover per type.

        Returns the enveloped event as written (post-redaction).
        """
        if type not in contract.EVENT_TYPES:
            raise ValueError(f"unknown event type: {type!r}")
        with self._lock:
            state = self._sessions.get(session_id)
            if state is None:
                # For a brand-new trace whose first event is not session_meta,
                # a minimal start record is synthesized so the contract's
                # "first line is session_meta" invariant holds.
                state = self._open_session_locked(
                    session_id, synthesize_start=(type != contract.SESSION_META)
                )
            if state.ended:
                raise RuntimeError(f"session {session_id} already ended")
            body = self._prepare_body_locked(session_id, type, body)
            event = self._append_locked(session_id, state, type, body)
            if type == contract.SESSION_META and body.get("phase") == "end":
                state.ended = True
            return event

    def end_session(self, session_id: str, end_reason: str = contract.END_NORMAL) -> dict[str, Any] | None:
        """Append the definitive end record. Idempotent per session."""
        if end_reason not in contract.END_REASONS:
            raise ValueError(f"unknown end_reason: {end_reason!r}")
        with self._lock:
            state = self._sessions.get(session_id)
            if state is not None and state.ended:
                return None
            if state is None:
                # Only end sessions that exist (in memory or on disk).
                path = self.trace_path(session_id)
                if not (path.exists() and path.stat().st_size > 0):
                    return None
        try:
            return self.record(
                session_id, contract.SESSION_META, {"phase": "end", "end_reason": end_reason}
            )
        except RuntimeError:
            # The on-disk trace already carries an end record.
            return None

    def active_sessions(self) -> list[str]:
        with self._lock:
            return [sid for sid, st in self._sessions.items() if not st.ended]

    def close(self, end_reason: str = contract.END_NORMAL) -> None:
        """End every session this core instance still has open."""
        for sid in self.active_sessions():
            self.end_session(sid, end_reason)

    # -------------------------------------------------------------- internals

    def _open_session_locked(self, session_id: str, *, synthesize_start: bool) -> _SessionState:
        path = self.trace_path(session_id)
        if path.exists() and path.stat().st_size > 0:
            # Resuming a trace this process didn't open (e.g. daemon restart
            # mid-session): continue the chain from the last complete line.
            seq, prev_hash, ended = _tail_state(path)
            state = _SessionState(seq=seq, prev_hash=prev_hash, ended=ended)
            self._sessions[session_id] = state
            return state
        state = _SessionState(seq=0, prev_hash=contract.genesis_hash(session_id))
        self._sessions[session_id] = state
        if synthesize_start:
            self._append_locked(
                session_id,
                state,
                contract.SESSION_META,
                {
                    "phase": "start",
                    "producer": {"adapter": "unknown", "adapter_version": "unknown"},
                    "cwd": os.getcwd(),
                },
            )
        return state

    def _prepare_body_locked(self, session_id: str, type: str, body: dict[str, Any]) -> dict[str, Any]:
        if type == contract.TOOL_CALL:
            args, paths = self._redactor.redact_args(body.get("args"))
            body = dict(body)
            body["args"] = args
            body["args_redactions"] = paths + list(body.get("args_redactions") or [])
        elif type == contract.TOOL_RESULT:
            body = self._prepare_result_locked(session_id, body)
        return body

    def _prepare_result_locked(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        body = dict(body)
        result, paths = self._redactor.redact_result(body.get("result"))
        if paths:
            body["result_redactions"] = paths + list(body.get("result_redactions") or [])
        serialized = contract.canonical_json(result).encode("utf-8")
        body["result_sha256"] = contract.sha256_hex(serialized)
        if len(serialized) <= self.config.inline_result_bytes:
            body["result"] = result
            body.setdefault("result_truncated", False)
        else:
            call_id = str(body.get("call_id") or "unknown-call")
            spill = self.spillover_path(session_id, call_id)
            spill.parent.mkdir(parents=True, exist_ok=True)
            spill.write_bytes(serialized)
            body["result"] = {
                "head_sample": serialized[:HEAD_SAMPLE_BYTES].decode("utf-8", errors="replace"),
                "spillover_path": str(spill.relative_to(self.config.trace_dir)),
            }
            body["result_truncated"] = True
            body["result_bytes"] = len(serialized)
        return body

    def _append_locked(
        self, session_id: str, state: _SessionState, type: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        event = contract.make_event(
            seq=state.seq + 1,
            ts=self._now(),
            session_id=session_id,
            type=type,
            prev_hash=state.prev_hash,
            body=body,
        )
        line = json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n"
        path = self.trace_path(session_id)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            if self.config.fsync:
                os.fsync(fh.fileno())
        state.seq = event["seq"]
        state.prev_hash = event["hash"]
        return event


# ------------------------------------------------------------------ recovery


def _tail_state(path: Path) -> tuple[int, str, bool]:
    """Return (last_seq, last_hash, ended) from the last complete, parseable
    line of a trace, truncating any trailing partial line left by a crash."""
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    # Walk backwards to the last line that parses as a complete event.
    for i in range(len(lines) - 1, -1, -1):
        candidate = lines[i]
        if not candidate.strip():
            continue
        try:
            event = json.loads(candidate)
            seq = int(event["seq"])
            last_hash = str(event["hash"])
        except (ValueError, KeyError, TypeError):
            continue
        # Truncate anything after this complete line (partial write). If the
        # complete line itself lost its trailing newline, restore it.
        end_of_line = min(sum(len(l) + 1 for l in lines[: i + 1]), len(raw))
        if end_of_line < len(raw):
            with open(path, "r+b") as fh:
                fh.truncate(end_of_line)
        if not raw[:end_of_line].endswith(b"\n"):
            with open(path, "ab") as fh:
                fh.write(b"\n")
        ended = (
            event.get("type") == contract.SESSION_META
            and isinstance(event.get("body"), dict)
            and event["body"].get("phase") == "end"
        )
        return seq, last_hash, ended
    raise ValueError(f"no complete event line in {path}")


def recover_interrupted(trace_dir: Path, *, now_fn: Callable[[], str] | None = None) -> list[str]:
    """Append an interrupted end record to every trace lacking an end record.

    Run at core startup: if a process dies without an end record, the
    compiler needs a definitive signal, and "the file just stops" is not
    one. Returns the session ids that were closed.
    """
    now = now_fn or _default_now
    closed: list[str] = []
    if not trace_dir.exists():
        return closed
    for path in sorted(trace_dir.glob("*.jsonl")):
        try:
            seq, prev_hash, ended = _tail_state(path)
        except ValueError:
            continue
        if ended:
            continue
        session_id = path.stem
        event = contract.make_event(
            seq=seq + 1,
            ts=now(),
            session_id=session_id,
            type=contract.SESSION_META,
            prev_hash=prev_hash,
            body={"phase": "end", "end_reason": contract.END_INTERRUPTED},
        )
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        closed.append(session_id)
    return closed
