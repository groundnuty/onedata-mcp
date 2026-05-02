"""Diagnostic report generator: per-model failure-mode analysis from artefacts.

Reads JSONL trial artefacts and emits two markdown reports:

  artefacts/<run_id>/REPORT_paper.md     — paper-facing summary table
  artefacts/<run_id>/REPORT_cyfronet.md  — diagnostic report for Cyfronet
                                            (per-model: pass rate, failure
                                            modes, API errors, recommended
                                            fixes per model)

Usage:
    uv run python -m benchmark.report                  # most recent run
    uv run python -m benchmark.report --run-id <id>    # specific run
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTEFACT_ROOT = REPO_ROOT / "artefacts"


@dataclass(frozen=True)
class TrialRecord:
    """One row from a JSONL artefact file. Tolerant to schema additions."""

    llm_name: str
    scenario_id: str
    outcome: str  # PASS | FAIL | RESET_FAIL | ADAPTER_ERROR
    rounds_used: int
    finish_reason: str | None
    usage_in_tokens: int
    usage_out_tokens: int
    wall_clock_seconds: float
    final_answer: str
    oracle_diagnosis: str
    error: str | None
    tool_calls: tuple

    @classmethod
    def from_dict(cls, d: dict) -> TrialRecord:
        return cls(
            llm_name=d["llm_name"],
            scenario_id=d["scenario_id"],
            outcome=d["outcome"],
            rounds_used=int(d.get("rounds_used") or 0),
            finish_reason=d.get("finish_reason"),
            usage_in_tokens=int(d.get("usage_in_tokens") or 0),
            usage_out_tokens=int(d.get("usage_out_tokens") or 0),
            wall_clock_seconds=float(d.get("wall_clock_seconds") or 0.0),
            final_answer=d.get("final_answer") or "",
            oracle_diagnosis=d.get("oracle_diagnosis") or "",
            error=d.get("error"),
            tool_calls=tuple(d.get("tool_calls") or ()),
        )


def load_run(run_dir: Path) -> list[TrialRecord]:
    out: list[TrialRecord] = []
    for f in sorted(run_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(TrialRecord.from_dict(json.loads(line)))
    return out


def latest_run(artefact_root: Path) -> Path:
    runs = sorted(artefact_root.glob("[0-9]*T[0-9]*"))
    if not runs:
        raise SystemExit(f"No run directories under {artefact_root}")
    return runs[-1]


# ---------------------------------------------------------------------------
# Paper report (compact comparison table)
# ---------------------------------------------------------------------------


def render_paper_report(records: list[TrialRecord], run_id: str) -> str:
    by_cell: dict[tuple[str, str], list[TrialRecord]] = defaultdict(list)
    for r in records:
        by_cell[(r.llm_name, r.scenario_id)].append(r)

    llms = sorted({r.llm_name for r in records})
    scenarios = sorted({r.scenario_id for r in records})

    out: list[str] = []
    out.append(f"# PPAM 2026 benchmark — paper summary  (run `{run_id}`)\n")
    out.append(f"**Panel:** {', '.join(llms)}\n")
    out.append(f"**Scenarios run:** {len(scenarios)} ({', '.join(scenarios)})\n")
    out.append("")

    # Per-cell PASS rate (P/K)
    out.append("## Per-cell pass-rate (PASS-count / trials)\n")
    header = "| Scenario | " + " | ".join(llms) + " |"
    sep = "|---" * (len(llms) + 1) + "|"
    out.append(header)
    out.append(sep)
    for sid in scenarios:
        row = [sid]
        for llm in llms:
            cell = by_cell.get((llm, sid), [])
            passes = sum(1 for r in cell if r.outcome == "PASS")
            row.append(f"{passes}/{len(cell)}" if cell else "—")
        out.append("| " + " | ".join(row) + " |")
    out.append("")

    # Per-LLM totals
    out.append("## Per-LLM totals\n")
    out.append("| LLM | PASS | FAIL | RESET_FAIL | ADAPTER_ERROR | Pass rate |")
    out.append("|---|---|---|---|---|---|")
    for llm in llms:
        rs = [r for r in records if r.llm_name == llm]
        c = Counter(r.outcome for r in rs)
        total = len(rs)
        passes = c.get("PASS", 0)
        rate = f"{passes / total * 100:.1f}%" if total else "—"
        out.append(
            f"| `{llm}` | {passes} | {c.get('FAIL', 0)} | "
            f"{c.get('RESET_FAIL', 0)} | {c.get('ADAPTER_ERROR', 0)} | {rate} |"
        )
    out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Cyfronet diagnostic report (per-model, deep)
# ---------------------------------------------------------------------------


def _classify_error(error: str | None) -> str:
    """Fuzzy classification of adapter / API errors into headline categories."""
    if not error:
        return "no error"
    e = error.lower()
    if "max_tokens" in e and ("forbidden" not in e):
        return "max_tokens budget exceeded (server-side cap)"
    if '"auto" tool choice' in e or "tool choice" in e or "tool_choice" in e:
        return "tool_choice=auto not supported (model not FC-tagged)"
    if "supported but currently inactive" in e:
        return "model inactive on Forge (not loaded for inference)"
    if "is not supported" in e:
        return "model not in Forge catalogue"
    if "not available for grant" in e:
        return "model not available in user grant"
    if "expired" in e and "key" in e:
        return "API key expired"
    if "401" in e or "unauthorized" in e:
        return "401 unauthorized"
    if "403" in e or "forbidden" in e:
        return "403 forbidden (often: passed name where ID expected)"
    if "max_turns" in e or "error_max_turns" in e:
        return "exceeded max_tool_rounds"
    if "timeout" in e or "timed out" in e:
        return "timeout"
    return "other"


def render_cyfronet_report(records: list[TrialRecord], run_id: str) -> str:
    by_llm: dict[str, list[TrialRecord]] = defaultdict(list)
    for r in records:
        by_llm[r.llm_name].append(r)

    out: list[str] = []
    out.append("# PPAM 2026 LLM panel — Cyfronet Forge diagnostic report\n")
    out.append(f"Run: `{run_id}`  •  Total trials: {len(records)}\n")
    out.append(
        "This report breaks down per-model behaviour observed against the "
        "PPAM 2026 benchmark (18-scenario set covering discovery / access / "
        "placement bands of Onedata federated data operations). Intended for "
        "Cyfronet operations: which Forge-hosted models can be reliably used "
        "for tool-using agent workloads, and what specific limitations each "
        "exhibits.\n"
    )
    out.append("")

    # Headline cross-model table
    out.append("## Headline cross-model summary\n")
    out.append(
        "| Model | PASS | FAIL | API/adapter errors | Avg rounds | Avg in-tokens | Avg out-tokens |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for llm in sorted(by_llm):
        rs = by_llm[llm]
        passes = sum(1 for r in rs if r.outcome == "PASS")
        fails = sum(1 for r in rs if r.outcome == "FAIL")
        errs = sum(1 for r in rs if r.outcome == "ADAPTER_ERROR")
        avg_rounds = sum(r.rounds_used for r in rs) / max(1, len(rs))
        avg_in = sum(r.usage_in_tokens for r in rs) / max(1, len(rs))
        avg_out = sum(r.usage_out_tokens for r in rs) / max(1, len(rs))
        out.append(
            f"| `{llm}` | {passes} | {fails} | {errs} | "
            f"{avg_rounds:.1f} | {avg_in:.0f} | {avg_out:.0f} |"
        )
    out.append("")

    # Per-model section
    for llm in sorted(by_llm):
        rs = by_llm[llm]
        passes = [r for r in rs if r.outcome == "PASS"]
        fails = [r for r in rs if r.outcome == "FAIL"]
        errs = [r for r in rs if r.outcome == "ADAPTER_ERROR"]

        out.append(f"## `{llm}`\n")
        out.append(
            f"- Pass rate: **{len(passes)}/{len(rs)}** "
            f"({len(passes) / max(1, len(rs)) * 100:.0f}%)\n"
        )
        if rs:
            avg_wall = sum(r.wall_clock_seconds for r in rs) / len(rs)
            out.append(f"- Average wall-clock per trial: {avg_wall:.1f}s\n")

        if errs:
            out.append("\n### API / adapter errors\n")
            cats = Counter(_classify_error(r.error) for r in errs)
            for cat, n in cats.most_common():
                out.append(f"- **{cat}** — {n} occurrence(s)\n")
            # Show one verbatim example per category
            seen: set[str] = set()
            out.append("\n*Examples:*\n")
            for r in errs:
                cat = _classify_error(r.error)
                if cat in seen:
                    continue
                seen.add(cat)
                example = (r.error or "")[:200]
                out.append(f"- `{r.scenario_id}`: `{example}`\n")
                if len(seen) >= 4:
                    break

        if fails:
            out.append("\n### Failure modes (oracle FAIL)\n")
            # Group by oracle_diagnosis prefix (first 60 chars) to bucket similar fails
            modes = Counter()
            for r in fails:
                key = (r.oracle_diagnosis or "(no diagnosis)")[:80]
                modes[key] += 1
            for mode, n in modes.most_common(8):
                out.append(f"- **{n}× scenarios:** {mode}\n")

            # Tool-use pattern on failures (which tools were called, how many times)
            tool_patterns: dict[str, int] = Counter()
            for r in fails:
                names = tuple(tc.get("tool_name") for tc in r.tool_calls)
                tool_patterns[" → ".join(names) or "(no tool calls)"] += 1
            out.append("\n*Tool-call patterns on failures:*\n")
            for pat, n in tool_patterns.most_common(5):
                out.append(f"  - {n}× `{pat[:160]}`\n")

        if passes:
            avg_in = sum(r.usage_in_tokens for r in passes) / len(passes)
            avg_out = sum(r.usage_out_tokens for r in passes) / len(passes)
            avg_rounds = sum(r.rounds_used for r in passes) / len(passes)
            out.append(
                f"\n### On passing trials\n"
                f"- Avg rounds: {avg_rounds:.1f}\n"
                f"- Avg input tokens: {avg_in:.0f}\n"
                f"- Avg output tokens: {avg_out:.0f}\n"
            )

        # Recommended action for Cyfronet
        out.append("\n### Recommendation\n")
        if errs:
            cats = Counter(_classify_error(r.error) for r in errs)
            top, _ = cats.most_common(1)[0]
            if "max_tokens" in top:
                out.append(
                    "- Server-side `max_tokens` cap is below the harness budget. "
                    "Either expose the per-model cap via API, or document it. "
                    "Currently agents must guess the budget by trial-and-error.\n"
                )
            elif "tool_choice" in top:
                out.append(
                    "- Model is **not FC-tagged** (no function-calling support). "
                    "Recommend exposing the `FC` tag via `/v1/models` so agent "
                    "tooling can filter automatically (currently FC tag is only "
                    "visible in the web UI).\n"
                )
            elif "inactive" in top:
                out.append(
                    "- Model listed as supported but not loaded. Forge could "
                    "either remove it from `/v1/models` or expose an "
                    "`active=true|false` flag so consumers can pre-filter.\n"
                )
            else:
                out.append(f"- Investigate root cause of `{top}` errors.\n")
        elif len(passes) == len(rs):
            out.append("- **No issues observed.** Suitable for tool-using agent workloads.\n")
        elif len(passes) >= len(rs) * 0.7:
            out.append(
                "- Generally suitable; isolated FAILs are domain-reasoning "
                "shortfalls, not Forge issues.\n"
            )
        else:
            out.append(
                "- Multiple FAILs without API errors suggests the model's "
                "tool-use reasoning is weaker than the benchmark requires. "
                "Not a Forge-side issue but worth flagging to users selecting "
                "a model for agent workloads.\n"
            )
        out.append("")

    # Forge-API recommendations summary
    out.append("## Forge-API observations\n")
    out.append(
        "Independent of per-model behaviour, the following Forge-side gaps "
        "surfaced during integration:\n\n"
        "1. **`/v1/models` does not expose tags.** Function-calling support "
        "(`FC`), embedding-vs-chat (`EMB`), grant availability are visible "
        "only in the web UI. Agent harnesses can't filter automatically; "
        "must enumerate empirically.\n"
        "2. **`max_tokens` server-side caps vary per model and are not "
        "exposed.** Setting a generous budget breaks some models with "
        "`BadRequestError`. Per-model headroom should either be discoverable "
        "via API or documented per model.\n"
        "3. **Inactive models still appear in `/v1/models`.** Returning a "
        "different shape (or filtering them) would let consumers pre-flight "
        "without exhausting trial-and-error.\n"
        "4. **Identical error message format across catalogue / inactive / "
        "grant-permission failures.** All three return HTTP 400 with a "
        "`detail` string; programmatic differentiation requires substring "
        "matching.\n"
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", default="", help="Run ID under artefacts/. Default: most recent."
    )
    parser.add_argument("--artefact-root", default=str(DEFAULT_ARTEFACT_ROOT))
    parser.add_argument(
        "--out-dir",
        default="",
        help="Where to write the reports. Default: same as the run dir.",
    )
    args = parser.parse_args()

    root = Path(args.artefact_root)
    run_dir = (root / args.run_id) if args.run_id else latest_run(root)
    if not run_dir.is_dir():
        sys.exit(f"Run dir not found: {run_dir}")

    records = load_run(run_dir)
    if not records:
        sys.exit(f"No artefact JSONL files in {run_dir}")

    out_dir = Path(args.out_dir) if args.out_dir else run_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    paper = render_paper_report(records, run_dir.name)
    cyfronet = render_cyfronet_report(records, run_dir.name)

    paper_path = out_dir / "REPORT_paper.md"
    cyfronet_path = out_dir / "REPORT_cyfronet.md"
    paper_path.write_text(paper, encoding="utf-8")
    cyfronet_path.write_text(cyfronet, encoding="utf-8")

    n_llms = len({r.llm_name for r in records})
    n_scenarios = len({r.scenario_id for r in records})
    print(f"Wrote {paper_path}")
    print(f"Wrote {cyfronet_path}")
    print(f"Records: {len(records)}  •  LLMs: {n_llms}  •  Scenarios: {n_scenarios}")


if __name__ == "__main__":
    main()
