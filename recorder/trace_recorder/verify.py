"""`trace-recorder verify`: recompute the hash chain and report divergence.

Tamper evidence does not prevent tampering — nothing on the same disk can
— but makes it detectable and makes honest verification cheap. Beyond the
chain, this module also runs the structural checks the compiler's Phase 1
performs (seq gaps, call/result pairing, definitive end record) so a
trace can be pre-flighted before compilation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import contract


@dataclass
class VerifyReport:
    path: str
    session_id: str | None = None
    events: int = 0
    chain_ok: bool = True
    first_divergence_seq: int | None = None
    seq_gaps: list[tuple[int, int]] = field(default_factory=list)
    unpaired_call_ids: list[str] = field(default_factory=list)
    orphan_result_ids: list[str] = field(default_factory=list)
    starts_with_session_meta: bool = False
    end_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Chain intact and structurally compile-ready (Phase 1)."""
        return (
            self.chain_ok
            and not self.errors
            and not self.seq_gaps
            and not self.unpaired_call_ids
            and not self.orphan_result_ids
            and self.starts_with_session_meta
            and self.end_reason is not None
        )

    def summary_lines(self) -> list[str]:
        lines = [f"trace: {self.path}", f"session: {self.session_id}", f"events: {self.events}"]
        lines.append(
            "hash chain: OK"
            if self.chain_ok
            else f"hash chain: DIVERGED at seq {self.first_divergence_seq}"
        )
        lines.append("seq gaps: none" if not self.seq_gaps else f"seq gaps: {self.seq_gaps}")
        if self.unpaired_call_ids:
            lines.append(f"tool_call without tool_result: {self.unpaired_call_ids}")
        if self.orphan_result_ids:
            lines.append(f"tool_result without tool_call: {self.orphan_result_ids}")
        if not self.starts_with_session_meta:
            lines.append("first line is not session_meta")
        lines.append(
            f"end record: {self.end_reason}" if self.end_reason else "end record: MISSING"
        )
        for err in self.errors:
            lines.append(f"error: {err}")
        lines.append("verdict: " + ("OK" if self.ok else "FAIL"))
        return lines


def verify_trace(path: Path) -> VerifyReport:
    report = VerifyReport(path=str(path))
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        report.chain_ok = False
        report.errors.append(f"unreadable: {exc}")
        return report

    prev_hash: str | None = None
    prev_seq: int | None = None
    open_calls: dict[str, int] = {}
    seen_calls: set[str] = set()

    for lineno, line in enumerate(raw_lines, start=1):
        if not line.strip():
            continue
        try:
            event: dict[str, Any] = json.loads(line)
        except ValueError:
            report.errors.append(f"line {lineno}: not valid JSON")
            report.chain_ok = False
            if report.first_divergence_seq is None:
                report.first_divergence_seq = prev_seq
            continue
        report.events += 1
        seq = event.get("seq")
        session_id = event.get("session_id")
        if report.session_id is None:
            report.session_id = session_id
            prev_hash = contract.genesis_hash(str(session_id))
            report.starts_with_session_meta = event.get("type") == contract.SESSION_META and (
                (event.get("body") or {}).get("phase") == "start"
            )
        elif session_id != report.session_id:
            report.errors.append(f"line {lineno}: session_id changed to {session_id!r}")

        # Chain: prev_hash linkage + own-content hash.
        if report.chain_ok:
            if event.get("prev_hash") != prev_hash or not contract.check_event_hash(event):
                report.chain_ok = False
                report.first_divergence_seq = seq if isinstance(seq, int) else prev_seq
        prev_hash = event.get("hash")

        # Sequencing.
        if isinstance(seq, int):
            if prev_seq is not None and seq != prev_seq + 1:
                report.seq_gaps.append((prev_seq, seq))
            prev_seq = seq
        else:
            report.errors.append(f"line {lineno}: missing seq")

        # Structure.
        body = event.get("body") or {}
        etype = event.get("type")
        if etype == contract.TOOL_CALL:
            call_id = str(body.get("call_id"))
            open_calls[call_id] = lineno
            seen_calls.add(call_id)
        elif etype == contract.TOOL_RESULT:
            call_id = str(body.get("call_id"))
            if call_id in open_calls:
                del open_calls[call_id]
            elif call_id not in seen_calls:
                report.orphan_result_ids.append(call_id)
        elif etype == contract.SESSION_META and body.get("phase") == "end":
            report.end_reason = body.get("end_reason")

    report.unpaired_call_ids = sorted(open_calls)
    if report.events == 0:
        report.errors.append("empty trace")
        report.chain_ok = False
    return report
