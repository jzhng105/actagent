"""Chaos durability: kill -9 the writer mid-session, repeatedly.

Every surviving trace must verify to its last complete line and receive
an interrupted end record on restart.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from trace_recorder.core import recover_interrupted
from trace_recorder.verify import verify_trace

WRITER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, sys.argv[2])
    from pathlib import Path
    from trace_recorder import contract
    from trace_recorder.core import CoreConfig, RecorderCore

    core = RecorderCore(CoreConfig(trace_dir=Path(sys.argv[1]), fsync=True))
    core.record("chaos", contract.SESSION_META, {"phase": "start"})
    i = 0
    while True:
        i += 1
        core.record("chaos", contract.TOOL_CALL,
                    {"call_id": f"c-{i}", "tool": "shell.run", "args": {"n": i}})
        core.record("chaos", contract.TOOL_RESULT,
                    {"call_id": f"c-{i}", "status": "ok", "result": {"n": i}})
    """
)


def test_kill9_writer_then_recover(tmp_path):
    package_root = str(Path(__file__).resolve().parents[1])
    for round_no in range(3):
        trace_dir = tmp_path / f"round-{round_no}"
        proc = subprocess.Popen(
            [sys.executable, "-c", WRITER, str(trace_dir), package_root],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.time() + 10
        trace_path = trace_dir / "chaos.jsonl"
        while time.time() < deadline:
            if trace_path.exists() and trace_path.stat().st_size > 2000:
                break
            time.sleep(0.02)
        assert trace_path.exists(), proc.stderr.read().decode()
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

        closed = recover_interrupted(trace_dir)
        assert closed == ["chaos"]
        report = verify_trace(trace_path)
        assert report.chain_ok, f"round {round_no}: {report.summary_lines()}"
        assert not report.seq_gaps
        assert report.end_reason == "interrupted"
        assert report.events > 3
