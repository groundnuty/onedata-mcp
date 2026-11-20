"""The committed report `research/k2val-<run_id>.md`, built from a run's artefacts.

It does not count anything yet: every figure is raw and unclassified, and each failure carries
an empty line for the manual three-layer sort (harness bug / deployment artefact / model
failure). Layout, in reading order:

1. the control (`sonnet-medium`): scenarios where it did not pass are DESIGN SUSPECT, to be
   settled (a wrong tool, scenario or oracle) before any weak-model result is read;
2. a K2 violation, if any, in large letters (a tool bug, not a model failure);
2b. NEEDS_REVIEW: every trial whose two answer readings disagree (or whose judge failed), with
   both readings, the judge's evidence and the outcome each reading would give;
3. the raw pass^k table, control column first, the three Haiku efforts side by side, with a
   NEEDS_REVIEW count of its own in each cell;
4. the Haiku effort comparison;
5. every non-pass with its diagnosis and the trace lines that show why;
6. premature claims; tool-call sequences per scenario; wrong arguments; errors tools returned;
   harness and deployment problems; what the providers reported (size field, view lag);
7. recorded changes (each with its triggering trial and a before/after).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

from benchmark.k2val.artefact import JUDGED, read_trials
from benchmark.k2val.config import K2valConfig
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.secrets import redact

VALIDATION_HINTS = ("validation", "input should", "field required", "unexpected", "not offered")


def ordered_models(cfg: K2valConfig, present: set[str]) -> list[str]:
    """Control first; the Haiku configs together, at the place of the first one."""
    names = [n for n in cfg.models if n in present]
    control = [n for n in names if cfg.models[n].control]
    rest = [n for n in names if n not in control]
    haiku = [n for n in rest if n.startswith("haiku")]
    out = list(control)
    placed = False
    for n in rest:
        if n in haiku:
            if not placed:
                out.extend(haiku)
                placed = True
            continue
        out.append(n)
    return out


def _cell(rows: list[dict]) -> str:
    """`passes/judged`, pass^k when every trial was judged, then the other outcomes; a
    NEEDS_REVIEW is never counted as a pass or a fail until it is classified."""
    if not rows:
        return "-"
    judged = [r for r in rows if r["outcome"] in JUDGED]
    passes = sum(r["outcome"] == "PASS" for r in judged)
    review = sum(r["outcome"] == "NEEDS_REVIEW" for r in rows)
    extra = Counter(r["outcome"] for r in rows if r["outcome"] not in (*JUDGED, "NEEDS_REVIEW"))
    text = f"{passes}/{len(judged)}" if judged else "0/0"
    if judged and len(judged) == len(rows):
        text += f" (pass^{len(rows)}={int(passes == len(rows))})"
    if review:
        text += f" **+{review} NEEDS_REVIEW**"
    if extra:
        text += " +" + ",".join(f"{n} {k}" for k, n in sorted(extra.items()))
    return text


def _review_lines(r: dict) -> list[str]:
    reading = r.get("answer_reading") or {}
    verdict = reading.get("judge_verdict") or {}
    lines = [
        f"### {r['scenario_id']} / {r['llm_name']} / trial {r['trial_ix']}: NEEDS_REVIEW",
        f"- deterministic reader: `{reading.get('deterministic')}`",
        f"- judge ({verdict.get('model')} {verdict.get('effort')}, rubric "
        f"{verdict.get('rubric_sha')}): `{reading.get('judge')}`"
        + (f"; judge_error: {reading.get('judge_error')}" if reading.get("judge_error") else ""),
        f"- judge's evidence: {_short(verdict.get('evidence') or '', 220)!r}"
        + ("" if verdict.get("evidence_quoted") in (True, None) else " (NOT a quote)"),
        f"- outcome if each reading decided: {reading.get('outcome_if')}",
        f"- final answer: {_short(r.get('final_answer') or '(none)', 400)}",
        "- Classification (which reading is right; then harness bug / deployment artefact / "
        "tool-attributable / model capability): ",
        "",
    ]
    return lines


def _sequence(calls: list[dict]) -> str:
    out: list[str] = []
    for c in calls:
        label = c["tool"] + ("!" if not c.get("ok") else "")
        if out and out[-1][0] == label:
            out[-1][1] += 1
        else:
            out.append([label, 1])
    return " > ".join(f"{n}" + (f" x{k}" if k > 1 else "") for n, k in out) or "(no calls)"


def _short(value: object, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _trace_lines(row: dict) -> list[str]:
    lines = []
    for c in (row.get("calls") or [])[-3:]:
        status = "ok" if c.get("ok") else "ERR"
        lines.append(
            f"  - call {c['index']}: `{c['tool']}({_short(c['arguments'], 100)})` -> {status} "
            f"{_short(c.get('result_text') or c.get('error') or '', 140)}"
        )
    answer = _short(row.get("final_answer") or "(no final answer)", 200)
    lines.append(f"  - final answer: {answer}")
    return lines


def _cost_cell(rows: list[dict]) -> str:
    """USD billed to an API key; "subscription" for a config that ran on the subscription (its
    trials carry cost 0, which would read as free)."""
    if rows and all((r.get("raw_trace") or {}).get("billing") == "subscription" for r in rows):
        return "subscription"
    return f"{sum(r.get('cost_usd') or 0 for r in rows):.4f}"


def build_report(run_dir: Path, cfg: K2valConfig, *, run_id: str, meta: dict | None = None) -> str:
    rows = read_trials(run_dir)
    meta = meta or {}
    models = ordered_models(cfg, {r["llm_name"] for r in rows})
    scenarios = [s for s in SCENARIOS if any(r["scenario_id"] == s for r in rows)]
    by = defaultdict(list)
    for r in rows:
        by[(r["llm_name"], r["scenario_id"])].append(r)
    out: list[str] = []
    w = out.append

    w(f"# K2 validation panel: run `{run_id}`\n")
    w(
        "Raw and unclassified: nothing below is counted yet. Failures are sorted into harness "
        "bug / deployment artefact / model failure by hand (the `Classification` lines) "
        "before any figure is used.\n"
    )
    if meta:
        w(f"- K = {meta.get('k')}; scenarios {', '.join(scenarios)}; configs {', '.join(models)}")
    spend = meta.get("spend") or {}
    if spend:
        w(
            f"- Anthropic spend: ${spend.get('spent_usd', 0):.4f} of a "
            f"${spend.get('cap_usd', 0):.2f} cap (per config: {spend.get('per_model_usd')})"
        )
    for n in meta.get("skipped", []):
        w(f"- SKIPPED: {n}")
    w("")

    # 1. control
    controls = [m for m in models if cfg.models[m].control]
    for control in controls:
        suspects = []
        for sid in scenarios:
            cell = by.get((control, sid), [])
            if cell and any(r["outcome"] != "PASS" for r in cell):
                suspects.append(sid)
        w(f"## Control: {control}\n")
        if suspects:
            w(
                "**DESIGN SUSPECT: "
                + ", ".join(suspects)
                + ".** The control did not pass these. Settle a wrong tool, scenario or oracle "
                "before reading any weak-model result for them.\n"
            )
        else:
            w("The control passed every scenario it ran.\n")

    # 2. K2 violations
    violations = [r for r in rows if r.get("k2_violations")]
    if violations:
        w("# K2 VIOLATION (a TOOL BUG, not a model failure)\n")
        w(
            "A readiness tool answered `ready: true` before the background writer had written the "
            "last part of a file.\n"
        )
        for r in violations:
            w(
                f"- **{r['llm_name']} / {r['scenario_id']} / trial {r['trial_ix']}**: "
                f"{json.dumps(r['k2_violations'])}"
            )
        w("")

    # 2b. NEEDS_REVIEW
    review = [r for r in rows if r["outcome"] == "NEEDS_REVIEW"]
    w("## NEEDS_REVIEW (the two answer readings disagree, or the judge failed)\n")
    if review:
        w(
            f"**{len(review)} trial(s) to classify by hand before any figure is read.** No record "
            "is complete while one remains (design/10, gate 4).\n"
        )
        for r in sorted(review, key=lambda r: (r["scenario_id"], r["llm_name"], r["trial_ix"])):
            out.extend(_review_lines(r))
    else:
        w("None.\n")

    # 3. pass^k table
    w("## Raw pass^k table (judged trials; other outcomes after the `+`)\n")
    w("| scenario | " + " | ".join(models) + " |")
    w("|---|" + "---|" * len(models))
    for sid in scenarios:
        suspect = any(
            by.get((c, sid)) and any(r["outcome"] != "PASS" for r in by[(c, sid)]) for c in controls
        )
        label = f"{sid} {SCENARIOS[sid].title}" + (" **DESIGN SUSPECT**" if suspect else "")
        w(f"| {label} | " + " | ".join(_cell(by.get((m, sid), [])) for m in models) + " |")
    w("")

    # 4. Haiku effort comparison
    haiku = [m for m in models if m.startswith("haiku")]
    if len(haiku) > 1:
        w("## Haiku effort comparison (same model, three efforts)\n")
        w("| scenario | " + " | ".join(haiku) + " |")
        w("|---|" + "---|" * len(haiku))
        for sid in scenarios:
            w(f"| {sid} | " + " | ".join(_cell(by.get((m, sid), [])) for m in haiku) + " |")
        w("")
        w("| config | trials | mean rounds | mean output tokens | mean wall s | cost USD |")
        w("|---|---|---|---|---|---|")
        for m in haiku:
            mine = [r for r in rows if r["llm_name"] == m]
            if mine:
                w(
                    f"| {m} | {len(mine)} | {mean(r['rounds_used'] for r in mine):.1f} | "
                    f"{mean(r['usage_out_tokens'] for r in mine):.0f} | "
                    f"{mean(r['wall_clock_seconds'] for r in mine):.0f} | "
                    f"{_cost_cell(mine)} |"
                )
        w("")

    # 5. every non-pass
    w("## Every non-pass\n")
    bad = [r for r in rows if r["outcome"] not in ("PASS", "NEEDS_REVIEW")]
    if not bad:
        w("None.\n")
    for r in sorted(bad, key=lambda r: (r["scenario_id"], r["llm_name"], r["trial_ix"])):
        w(
            f"### {r['scenario_id']} / {r['llm_name']} / trial {r['trial_ix']}: {r['outcome']}"
            + (f" ({','.join(r['flags'])})" if r.get("flags") else "")
        )
        diagnosis = r.get("oracle_diagnosis") or r.get("no_result_reason") or r.get("error") or ""
        w(f"- Diagnosis: {_short(diagnosis, 400)}")
        w(f"- Axes: mcp={r.get('oracle_mcp_pass')} federation={r.get('oracle_federation_pass')}")
        w("- Trace:")
        out.extend(_trace_lines(r))
        w("- Classification (harness bug / deployment artefact / model failure): ")
        w("")

    # 6a. premature claims
    premature = [r for r in rows if "premature_claim" in (r.get("flags") or [])]
    w("## Premature claims\n")
    w(
        "\n".join(
            f"- {r['scenario_id']} / {r['llm_name']} / trial {r['trial_ix']}" for r in premature
        )
        or "None."
    )
    w("")

    # 6b. tool-call sequences
    w("## Tool-call sequences per scenario (`!` = the call returned an error)\n")
    for sid in scenarios:
        w(f"### {sid} {SCENARIOS[sid].title}\n")
        w("| config | trial | outcome | sequence |")
        w("|---|---|---|---|")
        for m in models:
            for r in sorted(by.get((m, sid), []), key=lambda r: r["trial_ix"]):
                w(f"| {m} | {r['trial_ix']} | {r['outcome']} | {_sequence(r.get('calls') or [])} |")
        w("")

    # 6c. wrong arguments, 6d. tool errors
    errors: Counter = Counter()
    wrong: list[str] = []
    for r in rows:
        for c in r.get("calls") or []:
            if c.get("ok"):
                continue
            msg = _short(c.get("error") or "", 120)
            errors[(c["tool"], msg)] += 1
            if any(h in msg.lower() for h in VALIDATION_HINTS):
                wrong.append(
                    f"- {r['llm_name']} / {r['scenario_id']} / trial {r['trial_ix']}: "
                    f"`{c['tool']}({_short(c['arguments'], 120)})` -> {msg}"
                )
    w("## Wrong arguments (calls the tool rejected as malformed)\n")
    w("\n".join(wrong) or "None.")
    w("")
    w("## Errors returned by tools\n")
    if errors:
        w("| tool | error | count |")
        w("|---|---|---|")
        for (tool, msg), n in errors.most_common():
            w(f"| {tool} | {msg} | {n} |")
    else:
        w("None.")
    w("")

    # 6e. harness / deployment
    w("## Harness and deployment problems\n")
    problems = [
        r
        for r in rows
        if r["outcome"] in ("RESET_FAIL", "ADAPTER_ERROR", "ORACLE_ERROR", "HARNESS_ERROR")
        or r.get("teardown_problems")
        or "writer_error" in (r.get("flags") or [])
    ]
    for r in problems:
        w(
            f"- {r['llm_name']} / {r['scenario_id']} / trial {r['trial_ix']}: {r['outcome']} "
            f"{_short(r.get('error') or r.get('teardown_problems') or '', 300)}"
        )
    if not problems:
        w("None.")
    w("")

    # 6f. what the providers reported
    fields: Counter = Counter()
    lag = 0
    seen = 0
    for r in rows:
        snap = r.get("snapshot_at_answer") or {}
        for f in (snap.get("files") or {}).values():
            seen += 1
            fields[f.get("size_field") or "(none)"] += 1
            if f.get("local_bytes") != f.get("local_bytes_via_owner"):
                lag += 1
    w("## What the providers reported (live checks)\n")
    w(f"- size field in the distribution answers: {dict(fields) or 'n/a'}")
    w(
        f"- the owner's view of the local provider's blocks differed from the local provider's own "
        f"view in {lag} of {seen} file snapshots at the answer"
    )
    w("")

    # 7. changes
    changes = run_dir / "changes.jsonl"
    w("## Recorded changes (each with its triggering trial; before / after)\n")
    if changes.exists() and changes.read_text().strip():
        for line in changes.read_text().splitlines():
            c = json.loads(line)
            w(f"- **{c['target']}** (trigger: {c['trigger_trial']}): {c['reason']}")
            w(f"  - before: {_short(c['before'], 300)}")
            w(f"  - after: {_short(c['after'], 300)}")
    else:
        w("None.")
    w("")
    return redact("\n".join(out))


def write_report(text: str, research_dir: Path, run_id: str) -> Path:
    research_dir.mkdir(parents=True, exist_ok=True)
    path = research_dir / f"k2val-{run_id}.md"
    path.write_text(text, encoding="utf-8")
    return path
