"""Rescore saved trial artefacts against the *current* oracle code.

Use case: a parser bug in `benchmark/oracles/_helpers.py` is fixed
post-sweep. The trials' raw data (final_answer, tool_calls,
oracle_diagnosis from the original run) is still on disk in JSONL
form. Re-running the LLM trials against the federation is expensive
($ + hours + state-mutation); but the **mcp_pass** axis of the
oracle is a pure function of the trial trace, so we can rescore it
locally with the corrected oracle.

Layered, audit-trail-preserving design (per user decision 2026-05-03):

  - Original artefacts (`<llm>__<scenario>.jsonl`) are NEVER modified.
  - Per (LLM, scenario) we write a SIDECAR file
    `<llm>__<scenario>.rescored.jsonl` with the rescored mcp_pass +
    diagnosis under new fields:

       oracle_mcp_pass_v2              (replaces oracle_mcp_pass on rescore)
       oracle_mcp_pass_original        (preserves original)
       oracle_diagnosis_v2             (replaces oracle_diagnosis on rescore)
       oracle_diagnosis_original       (preserves original)
       outcome_v2                      (PASS/FAIL based on v2 mcp_pass)
       outcome_original                (preserves original)
       rescore_version                 ("2026-05-03-helpers-v2")
       rescore_changed                 (bool — did v2 differ from original)

  - All other fields are passed through unchanged.

`oracle_federation_pass` is COPIED from the original — federation
state is gone post-sweep, so re-querying would be wrong. We
preserve what was true at trial time.

Runs the SAME oracle module the live trial used. The bug fixes are
applied transparently because they live in the parsing helpers
(`extract_int`, `extract_kv_lines`) those oracles call. The oracle's
federation-side checks are bypassed via a stub — we re-evaluate ONLY
the mcp_pass logic.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext, ToolCall
from benchmark._scenario_specialise import specialise_for_space
from benchmark.oracles import get_oracle
from benchmark.scenarios import SCENARIOS

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTEFACT_ROOT = REPO_ROOT / "artefacts"

# Bumped whenever the rescore semantics meaningfully change. Stored in
# every rescored record so a future archaeologist can tell which fix
# wave produced a given record.
RESCORE_VERSION = "2026-05-03-helpers-v2"

# Map LLM-name → space-name (the per-LLM-space architecture this
# benchmark uses). Imported lazily so this module's import cost stays
# low when only the rescore tool is needed.
from benchmark._per_llm_spaces import PER_LLM_SPACE  # noqa: E402

SCENARIOS_BY_ID = {s.id: s for s in SCENARIOS}


def _reconstruct_ctx(rec: dict) -> RunContext:
    """Rebuild a RunContext from a saved trial record sufficient for
    mcp_pass replay.

    Caveats:
      - `fixture_paths` keys are reconstructed from the scenario's
        canonical fixture spec (specialised for the LLM's space).
        VALUES (file_ids) are placeholders — that's fine for
        mcp_pass logic, which uses keys for path-shape checks but
        doesn't dereference the file_ids (those are federation
        addresses; only federation_pass needs them).
      - `captured_transfer_id` is reconstructed from the saved
        record IF P4 captured one. Pre-K=8 artefacts may not have
        this field; in that case rescore for P4 is best-effort.
      - `spaces_snapshot` is empty — D1 oracle falls back to a live
        federation query. We monkey-patch it to a no-op (returns the
        empty list) since rescore is offline. D1 PASS comparisons
        rely on `contains_token` + `extract_int` over `final_answer`
        — both pure.
    """
    scenario_id = rec["scenario_id"]
    llm_name = rec["llm_name"]
    space_name = PER_LLM_SPACE.get(llm_name, "ppam_2026_mcp_tests")

    scenario = SCENARIOS_BY_ID[scenario_id]
    specialised = specialise_for_space(scenario, space_name)
    fixture_paths = {f.path: f"replay-placeholder-{i}" for i, f in enumerate(specialised.fixture.files)}

    return RunContext(
        scenario_id=scenario_id,
        fixture_paths=fixture_paths,
        captured_transfer_id=rec.get("captured_transfer_id"),
        space_name=space_name,
        spaces_snapshot=(),
    )


def _reconstruct_trace(rec: dict) -> AgentTrace:
    """Rebuild AgentTrace from saved record. Tool-call args + results
    are persisted in the JSONL so this is a straight pass-through."""
    tool_calls = tuple(
        ToolCall(
            tool_name=tc["tool_name"],
            arguments=tc.get("arguments") or {},
            succeeded=tc.get("succeeded", True),
            error=tc.get("error"),
            result=tc.get("result"),
        )
        for tc in rec.get("tool_calls") or ()
    )
    return AgentTrace(
        final_answer=rec.get("final_answer") or "",
        tool_calls=tool_calls,
    )


async def _rescore_one(rec: dict) -> OracleResult:
    """Re-run the oracle's mcp_pass logic on a saved trial. Federation-
    side checks are bypassed via patches to the federation API
    helpers — they return harmless empty/default values and the
    oracle still computes mcp_pass correctly because that axis is
    pure-text.

    Returns OracleResult where `federation_pass` is the ORIGINAL
    value from the trial record (we don't replay federation state)."""
    scenario_id = rec["scenario_id"]
    ctx = _reconstruct_ctx(rec)
    trace = _reconstruct_trace(rec)
    oracle = get_oracle(scenario_id)

    # Federation-side stubs. The various oracles call these helpers
    # to verify post-state; we make them return harmless empty/default
    # values. The oracle's mcp_pass branch (text-parsing only) runs
    # to completion; the federation_pass branch will compute against
    # empty data — but we OVERRIDE the federation_pass field at the
    # end with the original value, so the offline computation doesn't
    # leak into the result.
    async def _empty_dict(*_a: object, **_k: object) -> dict:
        return {}

    async def _empty_list(*_a: object, **_k: object) -> list:
        return []

    async def _none(*_a: object, **_k: object) -> Any:  # noqa: ANN401
        return None

    async def _empty_str(*_a: object, **_k: object) -> str:
        return ""

    # Patch the federation-API surface used by oracles.
    # Also short-circuit the dynamic-tier polling loop (P3, P4):
    # placement.py runs `while time.time() < deadline: ... time.sleep(N)`
    # for up to 60s. With federation stubs returning empty data the loop
    # completes the full 60s wait before declaring federation_pass=False.
    # We override DYNAMIC_DEADLINE_SECONDS to 0.001 so the deadline is
    # past on the first check and the loop body runs at most once. The
    # federation_pass output is overridden later (preserved from the
    # original record), so the exact mechanism here doesn't matter.
    patches = [
        patch("benchmark.oracles.access.files_api.get_file_metadata", new=_empty_dict),
        patch("benchmark.oracles.access.files_api.get_file_id", new=_empty_str),
        patch("benchmark.oracles.access.files_api.list_files_recursively", new=_empty_dict),
        patch("benchmark.oracles.discovery.spaces_api.list_user_spaces", new=_empty_list),
        patch("benchmark.oracles.placement.qos_api.get_file_qos_summary", new=_empty_dict),
        patch("benchmark.oracles.placement.qos_api.get_qos_requirement", new=_empty_dict),
        patch("benchmark.oracles.placement.transfers_api.get_transfer", new=_empty_dict),
        patch("benchmark.oracles.placement._space_id", new=_empty_str),
        patch("benchmark.oracles.placement._any_transfer_for_file", new=_none),
        patch("benchmark.oracles.placement.DYNAMIC_DEADLINE_SECONDS", 0.001),
        patch("benchmark.oracles.placement.DYNAMIC_POLL_INTERVAL", 0.0),
    ]
    for p in patches:
        p.start()
    try:
        result = await oracle(ctx, trace)
    finally:
        for p in patches:
            p.stop()

    # Preserve the trial-time federation_pass (offline replay can't
    # observe federation state).
    return OracleResult(
        mcp_pass=result.mcp_pass,
        federation_pass=rec.get("oracle_federation_pass"),
        diagnosis=result.diagnosis,
    )


def _classify_failure(rec: dict) -> str | None:
    """For a non-PASS trial, classify the failure into one of three
    standard data-cleaning categories:

      - 'deployment-L3-granite-tool-call'  Granite emitted `<tool_call>`
        markup as text instead of structured tool_calls (vLLM
        --tool-call-parser granite4 covers early rounds, breaks on
        round 3+; pending vLLM-devops fix). See L-3 in
        research/llm-output-stability-findings.md.
      - 'deployment-L2-gemma-channel'  Gemma emitted harmony-channel
        `<|channel>` markup before vLLM --reasoning-parser gemma4
        was applied. Should be empty in K=8 since the parser flag
        was on; included for forensic completeness.
      - 'model'  genuine model reasoning gap (default for non-PASS
        trials with no deployment-marker signature in the answer).

    Returns None for PASS trials.

    Detection rules are conservative — we ONLY classify as deployment
    when there's an unambiguous textual signature in the final_answer.
    Anything else is 'model'. This avoids over-attributing to
    deployment.
    """
    if rec.get("outcome") == "PASS":
        return None
    answer = (rec.get("final_answer") or "").lower()
    llm = rec.get("llm_name", "")

    # L-3: literal `<tool_call>` or `</tool_call>` markup in answer
    # (the parser failed to route it to the structured field).
    if "<tool_call>" in answer or "</tool_call>" in answer:
        if "granite" in llm:
            return "deployment-L3-granite-tool-call"
        # Other LLMs leaking tool-call markup — same class but not L-3
        # specifically. Generic deployment label.
        return "deployment-tool-call-leak"

    # L-2 / Gemma channel-marker leak (should be 0 in this K=8 run
    # since the --reasoning-parser flag was applied; this branch is
    # forensic insurance).
    if "<|channel>" in answer or "<channel|>" in answer:
        return "deployment-channel-marker-leak"

    return "model"


def _rescore_record(rec: dict, oracle_result: OracleResult) -> dict:
    """Layer the rescore onto the original record and return a new
    record dict suitable for sidecar JSONL persistence.

    Original fields are preserved verbatim. New fields are added
    with `_v2` suffix. `outcome_v2` reflects what the rescored
    mcp_pass + (preserved) federation_pass would produce.

    `failure_category` classifies non-PASS trials (after rescore)
    into model vs deployment buckets — used by --cleaned-only
    reporting modes downstream.
    """
    out = dict(rec)  # shallow copy
    original_pass = bool(rec.get("oracle_mcp_pass"))
    rescored_pass = bool(oracle_result.mcp_pass)

    # KEY DESIGN CHOICE: rescore is LIFT-ONLY.
    # - Original PASS, rescored PASS  → final PASS (no change)
    # - Original PASS, rescored FAIL  → final PASS (DEMOTION REJECTED)
    # - Original FAIL, rescored PASS  → final PASS (LIFT applied — this
    #                                   is the bug fix purpose)
    # - Original FAIL, rescored FAIL  → final FAIL (no change)
    #
    # Rationale: rescore runs offline with empty federation state.
    # Several oracles (P3 with L-1 loosening, P4 with captured_transfer_id,
    # cross-trial federation reads) use the federation-side state to
    # compute mcp_pass. With the federation stubbed to empty, those
    # oracles can spuriously demote a PASS to FAIL. We don't want that
    # — the original oracle ran AT TRIAL TIME with real federation state,
    # so its PASS verdict is more authoritative for those cases.
    # Conversely, if the rescore says PASS, the new parser logic found
    # something the old parser missed — that's a genuine lift.
    final_pass = original_pass or rescored_pass

    out["oracle_mcp_pass_original"] = original_pass
    out["oracle_mcp_pass_v2"] = final_pass
    out["oracle_mcp_pass_rescore_raw"] = rescored_pass  # what the rescore alone said
    out["oracle_diagnosis_original"] = rec.get("oracle_diagnosis", "")
    out["oracle_diagnosis_v2"] = oracle_result.diagnosis if not final_pass else ""
    out["outcome_original"] = rec.get("outcome", "")
    out["outcome_v2"] = "PASS" if final_pass else "FAIL"
    out["rescore_version"] = RESCORE_VERSION
    out["rescore_changed"] = original_pass != final_pass  # only lift counts as a change
    out["rescore_demotion_rejected"] = original_pass and not rescored_pass
    # Classify the v2 outcome (post-rescore). Trials that were lifted
    # from FAIL→PASS by the parser fix are now PASS so they get None;
    # only stable-FAIL trials get classified.
    classification_record = dict(rec)
    classification_record["outcome"] = out["outcome_v2"]
    out["failure_category"] = _classify_failure(classification_record)
    return out


async def rescore_run(run_dir: Path) -> tuple[int, int, int]:
    """Iterate every <llm>__<scenario>.jsonl in run_dir, rescore each
    trial, write sidecar <llm>__<scenario>.rescored.jsonl files.

    Returns (total_trials, changed_count, lifted_count) where:
      changed_count = trials whose mcp_pass differs in v2
      lifted_count = subset where v2 PASSes but original didn't
    """
    total = 0
    changed = 0
    lifted = 0

    for f in sorted(run_dir.glob("*.jsonl")):
        # Skip our own sidecars from prior rescore runs.
        if f.name.endswith(".rescored.jsonl"):
            continue
        out_path = f.parent / f.name.replace(".jsonl", ".rescored.jsonl")
        rescored: list[dict] = []
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            try:
                result = await _rescore_one(rec)
            except Exception as e:  # noqa: BLE001
                # Don't crash the whole rescore on one bad record. Mark
                # the rescore as failed; original outcome stays the
                # source of truth.
                result = OracleResult(
                    mcp_pass=rec.get("oracle_mcp_pass") or False,
                    federation_pass=rec.get("oracle_federation_pass"),
                    diagnosis=f"rescore-error: {type(e).__name__}: {e}",
                )
            new_rec = _rescore_record(rec, result)
            rescored.append(new_rec)
            total += 1
            if new_rec["rescore_changed"]:
                changed += 1
                if not new_rec["oracle_mcp_pass_original"] and new_rec["oracle_mcp_pass_v2"]:
                    lifted += 1
        out_path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rescored) + "\n",
            encoding="utf-8",
        )
    return total, changed, lifted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", required=True, help="Run ID under artefacts/."
    )
    parser.add_argument("--artefact-root", default=str(DEFAULT_ARTEFACT_ROOT))
    args = parser.parse_args()

    root = Path(args.artefact_root)
    run_dir = root / args.run_id
    if not run_dir.is_dir():
        sys.exit(f"Run dir not found: {run_dir}")

    total, changed, lifted = asyncio.run(rescore_run(run_dir))
    pct = (changed / total * 100) if total else 0.0
    print(f"Rescored {total} trials in {run_dir}")
    print(f"  changed:  {changed}  ({pct:.1f}%)")
    print(f"  lifted:   {lifted}  (FAIL → PASS via oracle fix)")
    print(f"  version:  {RESCORE_VERSION}")
    print(f"  sidecars: {run_dir}/*.rescored.jsonl")

    # Failure-category tally across the rescored data — useful for
    # paper-grade "cleaned" data reporting.
    by_category: dict[str, int] = {}
    by_llm: dict[str, dict[str, int]] = {}
    for f in sorted(run_dir.glob("*.rescored.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            cat = r.get("failure_category")
            if cat is None:
                continue  # PASS trial
            by_category[cat] = by_category.get(cat, 0) + 1
            llm = r.get("llm_name", "?")
            by_llm.setdefault(llm, {})
            by_llm[llm][cat] = by_llm[llm].get(cat, 0) + 1

    print()
    print("Failure-category tally (post-rescore):")
    for cat, n in sorted(by_category.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {cat}")
    print()
    print("Per-LLM failure breakdown (post-rescore):")
    for llm in sorted(by_llm.keys()):
        cats = by_llm[llm]
        total_fail = sum(cats.values())
        breakdown = ", ".join(
            f"{cat.split('-', 1)[0]}={n}" for cat, n in sorted(cats.items())
        )
        print(f"  {llm:24s}  fail={total_fail}  ({breakdown})")


if __name__ == "__main__":
    main()
