"""`trace-recorder status`: sessions and uncompiled traces (the nag).

The compile nag is framework-neutral: any trace newer than the newest
compiled workflow artifact is reported as uncompiled and the command
exits non-zero, so a pre-commit hook, CI job, or session-end hook can all
enforce "no session ends without compilation being at least offered".
Host-specific nags (a Claude Code Stop hook printing a reminder) are
optional sugar on top of this check.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import contract

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}


@dataclass
class SessionStatus:
    session_id: str
    path: str
    events: int
    ended: bool
    end_reason: str | None
    mtime: float
    uncompiled: bool = False


@dataclass
class StatusReport:
    trace_dir: str
    sessions: list[SessionStatus] = field(default_factory=list)
    newest_workflow: str | None = None

    @property
    def uncompiled(self) -> list[SessionStatus]:
        return [s for s in self.sessions if s.uncompiled]

    @property
    def active(self) -> list[SessionStatus]:
        return [s for s in self.sessions if not s.ended]

    def summary_lines(self) -> list[str]:
        lines = [f"trace dir: {self.trace_dir}"]
        if not self.sessions:
            lines.append("no traces recorded")
            return lines
        for s in self.sessions:
            state = f"ended ({s.end_reason})" if s.ended else "ACTIVE"
            nag = "  [UNCOMPILED]" if s.uncompiled else ""
            lines.append(f"  {s.session_id}  {s.events} events  {state}{nag}")
        lines.append(f"newest workflow artifact: {self.newest_workflow or 'none found'}")
        n = len(self.uncompiled)
        if n:
            lines.append(
                f"{n} uncompiled trace(s). Run the workflow-compiler skill on them "
                f"(or delete traces that should not become workflows)."
            )
        else:
            lines.append("all traces compiled")
        return lines


def _newest_workflow(workflow_root: Path) -> tuple[Path | None, float]:
    newest: Path | None = None
    newest_mtime = 0.0
    stack = [workflow_root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            elif entry.name.endswith(".workflow.yaml"):
                mtime = entry.stat().st_mtime
                if mtime > newest_mtime:
                    newest, newest_mtime = entry, mtime
    return newest, newest_mtime


def scan_status(trace_dir: Path, workflow_root: Path | None = None) -> StatusReport:
    report = StatusReport(trace_dir=str(trace_dir))
    workflow_root = workflow_root or Path.cwd()
    newest, newest_mtime = _newest_workflow(workflow_root)
    report.newest_workflow = str(newest) if newest else None
    if not trace_dir.exists():
        return report
    for path in sorted(trace_dir.glob("*.jsonl")):
        events = 0
        ended = False
        end_reason: str | None = None
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    events += 1
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    body = event.get("body") or {}
                    if event.get("type") == contract.SESSION_META and body.get("phase") == "end":
                        ended = True
                        end_reason = body.get("end_reason")
        except OSError:
            continue
        mtime = path.stat().st_mtime
        report.sessions.append(
            SessionStatus(
                session_id=path.stem,
                path=str(path),
                events=events,
                ended=ended,
                end_reason=end_reason,
                mtime=mtime,
                uncompiled=mtime > newest_mtime,
            )
        )
    return report
