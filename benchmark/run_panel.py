"""CLI entry: run the multi-LLM panel against the scenario set.

Usage:
    uv run python -m benchmark.run_panel --trials 8 --scenarios D1,A1,P1
    uv run python -m benchmark.run_panel --trials 8                # all 18

Per (LLM, scenario) it runs `--trials` trials and writes one JSONL file
to `artefacts/<run_id>/<llm_name>__<scenario_id>.jsonl`. The pass^k
aggregator (#22) consumes these files.

The script logs a per-trial summary line to stdout so a human can watch
progress; no other output. Run-level summary at the end shows pass rate
per (LLM, scenario) and total wall-clock time.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

# Imports that touch onedata_mcp.config must come AFTER load_dotenv.
# Import all scenarios from the canonical module.
from benchmark import scenarios as _scenarios_module  # noqa: E402
from benchmark._scenario_types import Scenario  # noqa: E402
from benchmark.panel import build_panel  # noqa: E402
from benchmark.trial_runner import run_trial  # noqa: E402
from onedata_mcp.main import mcp  # noqa: E402

ALL_SCENARIO_IDS = (
    "D1",
    "D2",
    "D3",
    "D4",
    "D5",
    "D6",
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
    "A6",
    "P1",
    "P2",
    "P3",
    "P4",
    "P5",
    "P6",
)


def _resolve_scenarios(ids: tuple[str, ...]) -> tuple[Scenario, ...]:
    out: list[Scenario] = []
    for sid in ids:
        scenario = getattr(_scenarios_module, sid, None)
        if scenario is None:
            raise SystemExit(f"Unknown scenario id: {sid}")
        out.append(scenario)
    return tuple(out)


def _make_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


async def _main_async(args: argparse.Namespace) -> int:
    requested_ids = (
        tuple(s.strip() for s in args.scenarios.split(",") if s.strip())
        if args.scenarios
        else ALL_SCENARIO_IDS
    )
    scenarios = _resolve_scenarios(requested_ids)

    panel, skipped = build_panel()
    if skipped:
        print("[panel] Skipped legs:")
        for reason in skipped:
            print(f"  - {reason}")
    if not panel:
        print("[panel] No LLMs activated; nothing to run.", file=sys.stderr)
        return 2

    run_id = args.run_id or _make_run_id()
    artefact_dir = Path(args.artefact_root) / run_id
    print(f"[run_panel] run_id={run_id}  artefacts → {artefact_dir}")
    print(
        f"[run_panel] panel: {[p.name for p in panel]}  "
        f"scenarios: {[s.id for s in scenarios]}  trials={args.trials}"
    )

    overall_t0 = time.time()
    summary: dict[tuple[str, str], dict[str, int]] = {}

    for entry in panel:
        adapter = entry.build()
        for scenario in scenarios:
            counts = {"PASS": 0, "FAIL": 0, "RESET_FAIL": 0, "ADAPTER_ERROR": 0}
            for trial_ix in range(args.trials):
                t0 = time.time()
                artefact = await run_trial(
                    adapter=adapter,
                    scenario=scenario,
                    mcp_app=mcp,
                    run_id=run_id,
                    trial_ix=trial_ix,
                    artefact_dir=artefact_dir,
                )
                counts[artefact.outcome] += 1
                elapsed = time.time() - t0
                print(
                    f"  [{entry.name} / {scenario.id} / trial {trial_ix}] "
                    f"{artefact.outcome}  ({elapsed:.1f}s, "
                    f"rounds={artefact.rounds_used}, "
                    f"in={artefact.usage_in_tokens}, out={artefact.usage_out_tokens})"
                    + (f"  err={artefact.error[:60]}" if artefact.error else "")
                )
            summary[(entry.name, scenario.id)] = counts

    total_secs = time.time() - overall_t0
    print(f"\n[run_panel] DONE in {total_secs:.0f}s")
    print(f"[run_panel] Summary (PASS/FAIL/RESET_FAIL/ADAPTER_ERROR per cell, K={args.trials}):")
    for (llm, sid), counts in summary.items():
        print(
            f"  {llm:24s}  {sid:3s}  "
            f"P={counts['PASS']}  F={counts['FAIL']}  "
            f"R={counts['RESET_FAIL']}  A={counts['ADAPTER_ERROR']}"
        )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PPAM 2026 multi-LLM benchmark panel.")
    parser.add_argument(
        "--trials",
        type=int,
        default=8,
        help="K = trials per (LLM, scenario) cell. Paper headline uses K=8.",
    )
    parser.add_argument(
        "--scenarios",
        type=str,
        default="",
        help="Comma-separated scenario ids (e.g. 'D1,A1,P1'); empty = all 18.",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default="",
        help="Override the run-id (default: %%Y%%m%%dT%%H%%M%%S).",
    )
    parser.add_argument(
        "--artefact-root",
        type=str,
        default=str(REPO_ROOT / "artefacts"),
        help="Root dir for per-run artefact subdirs.",
    )
    args = parser.parse_args()

    rc = asyncio.run(_main_async(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
