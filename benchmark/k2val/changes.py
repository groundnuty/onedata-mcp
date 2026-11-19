"""Guard 3: every change made during the panel is recorded with its triggering trial and a
before/after of the description or answer it changed (`changes.jsonl` in the run directory)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from benchmark.k2val.secrets import redact


def record_change(
    run_dir: Path, *, target: str, trigger_trial: str, reason: str, before: str, after: str
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "changes.jsonl"
    row = {
        "t": time.time(),
        "target": target,
        "trigger_trial": trigger_trial,
        "reason": reason,
        "before": before,
        "after": after,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(redact(json.dumps(row, ensure_ascii=False)) + "\n")
    return path
