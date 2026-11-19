"""Anthropic spend tracking for the panel (the Haiku configs and the Sonnet control).

Every call's cost is recorded in `spend.jsonl` in the run directory, with a running total.
The cap is a config value (`[spend] cap_usd`, default $50). A trial is refused to start once
the run's spend plus a reserve (default $0.50) would exceed the cap; with other trials in
flight, their reserves count too (so two trials starting at once cannot both slip under).
A call is never made once the spend has reached the cap.

A resumed run (same run id) reloads its spend from the ledger.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from benchmark.k2val.config import PriceTiers
from benchmark.k2val.secrets import redact
from benchmark.llm_adapters.anthropic_api import AdapterStop

LEDGER = "spend.jsonl"


class SpendTracker:
    def __init__(self, cap_usd: float, reserve_usd: float, run_dir: Path | None = None) -> None:
        self.cap_usd = cap_usd
        self.reserve_usd = reserve_usd
        self._ledger = (run_dir / LEDGER) if run_dir else None
        self._lock = threading.Lock()
        self.spent_usd = 0.0
        self.per_model: dict[str, float] = {}
        self.calls = 0
        self.in_flight = 0
        if self._ledger and self._ledger.exists():
            for line in self._ledger.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.spent_usd += row["cost_usd"]
                    self.per_model[row["model"]] = (
                        self.per_model.get(row["model"], 0.0) + row["cost_usd"]
                    )
                    self.calls += 1

    def try_begin_trial(self) -> bool:
        """Reserve for a trial about to start; False (nothing reserved) if it would not fit."""
        with self._lock:
            if self.spent_usd + self.reserve_usd * (self.in_flight + 1) > self.cap_usd:
                return False
            self.in_flight += 1
            return True

    def end_trial(self) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)

    def before_call(self) -> None:
        """For the adapter: stop the trial rather than make a call past the cap."""
        if self.spent_usd >= self.cap_usd:
            raise AdapterStop("budget_exhausted")

    def record(
        self,
        model: str,
        response_id: str,
        prompt_tokens: int,
        output_tokens: int,
        price: PriceTiers,
    ) -> float:
        cost = price.cost(prompt_tokens, output_tokens)
        with self._lock:
            self.spent_usd += cost
            self.per_model[model] = self.per_model.get(model, 0.0) + cost
            self.calls += 1
            row = {
                "t": time.time(),
                "model": model,
                "response_id": response_id,
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost,
                "run_total_usd": self.spent_usd,
            }
            if self._ledger:
                self._ledger.parent.mkdir(parents=True, exist_ok=True)
                with self._ledger.open("a", encoding="utf-8") as f:
                    f.write(redact(json.dumps(row)) + "\n")
        return cost

    def summary(self) -> dict:
        return {
            "cap_usd": self.cap_usd,
            "reserve_usd": self.reserve_usd,
            "spent_usd": round(self.spent_usd, 6),
            "per_model_usd": {k: round(v, 6) for k, v in sorted(self.per_model.items())},
            "calls": self.calls,
        }
