"""The report's section on the stable tools' smoke re-run (design/16).

For each owed re-run (`stable_scenarios.SMOKE`): how many trials of its DIRECT scenarios ran,
and how many trials of its SUPPORTING scenarios made a target-matched successful call of the
tool (`stable_oracles.smoke_evidence`): a supporting trial without one is no evidence for the
tool. Then the two rows that need more than a pass: get_file_qos_summary's result shape (the
`summary_shape` check of ST3 and ST5) and get_qos_requirement's id-guard hint, which is verified
only by trials in which it was SHOWN (`stable_oracles.hint_exposures`, from every trial of the
run, whatever its scenario): with none, its wording is NOT VERIFIED.

Only the stored trials are read, so the section can be written again offline.
"""

from __future__ import annotations

from benchmark.k2val.stable_oracles import hint_exposures, smoke_evidence
from benchmark.k2val.stable_scenarios import SMOKE, SMOKE_SCENARIOS

SCORED = ("PASS", "FAIL", "NEEDS_REVIEW")


def _who(row: dict) -> str:
    return f"{row.get('llm_name')} / {row.get('scenario_id')} / trial {row.get('trial_ix')}"


def smoke_section(rows: list[dict]) -> list[str]:
    """The section's lines; none when the run holds no trial of a smoke scenario."""
    smoke_rows = [r for r in rows if r.get("scenario_id") in SMOKE_SCENARIOS]
    if not smoke_rows:
        return []
    out = [
        "## The stable tools' smoke re-run (design/16)\n",
        "Direct: the scored trials of the scenarios that require the tool. Supporting: trials of "
        "the scenarios where it is only natural (or the oracle asks for it) that made a "
        "target-matched successful call of it, of their scored trials. Every non-pass is "
        "classified by tool and cause before the pass rule is applied (design/16).\n",
        "| tool | change | direct (scored trials) | supporting (with a target-matched call) |",
        "|---|---|---|---|",
    ]
    for entry in SMOKE:
        direct = [r for r in smoke_rows if r["scenario_id"] in entry.direct]
        scored = [r for r in direct if r.get("outcome") in SCORED]
        support = [
            r
            for r in smoke_rows
            if r["scenario_id"] in entry.supporting and r.get("outcome") in SCORED
        ]
        evidence = [r for r in support if entry.tool in smoke_evidence(r)]
        direct_cell = f"{len(scored)} of {len(direct)} ({', '.join(entry.direct) or '-'})"
        support_cell = (
            f"{len(evidence)} of {len(support)} ({', '.join(entry.supporting)})"
            if entry.supporting
            else "-"
        )
        out.append(f"| {entry.tool} | {entry.change} | {direct_cell} | {support_cell} |")
    out.append("")

    shape = [
        (r, c)
        for r in smoke_rows
        for c in r.get("checks") or ()
        if isinstance(c, dict) and c.get("name") == "summary_shape"
    ]
    checked = [(r, c) for r, c in shape if c.get("passed") is not None]
    failed = [(r, c) for r, c in checked if c.get("passed") is False]
    out.append(
        f"- get_file_qos_summary's result shape (`replicas_num`, ST3 and ST5): checked in "
        f"{len(checked)} trial(s), failed in {len(failed)}."
    )
    out.extend(f"  - {_who(r)}: {c.get('detail')}" for r, c in failed)

    shown = [(r, e) for r in rows for e in hint_exposures(r.get("calls") or ())]
    if not shown:
        out.append(
            "- get_qos_requirement's id-guard hint: no trial was shown it, so its wording is "
            "NOT VERIFIED (an open item, design/16)."
        )
    else:
        recovered = sum(1 for _, e in shown if e["recovered"])
        out.append(
            f"- get_qos_requirement's id-guard hint: shown {len(shown)} time(s), followed by a "
            f"recovery {recovered} time(s):"
        )
        out.extend(
            f"  - {_who(r)}: call {e['call']}, "
            + (f"recovered (call {e['next_call']})" if e["recovered"] else "NOT recovered")
            for r, e in shown
        )
    out.append("")
    return out
