from __future__ import annotations

import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixed_clock():
    """Deterministic timestamp source so traces (and their hashes) are stable."""

    class Clock:
        def __init__(self) -> None:
            self.n = 0

        def __call__(self) -> str:
            self.n += 1
            return f"2026-07-18T15:04:{self.n:02d}.000-04:00"

    return Clock()


@pytest.fixture
def make_core(tmp_path, fixed_clock):
    from trace_recorder.core import CoreConfig, RecorderCore

    def factory(**config_kwargs):
        config_kwargs.setdefault("trace_dir", tmp_path / ".traces")
        config_kwargs.setdefault("fsync", False)
        return RecorderCore(CoreConfig(**config_kwargs), now_fn=fixed_clock)

    return factory
