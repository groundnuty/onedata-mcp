"""Re-read stored answers offline: `make k2val-rejudge RID=<run id>` (design/08, "Reading the
answer").

For every stored trial with a final answer: the deterministic reader of the CURRENT code reads
it again, the judge reads it (or its cached verdict is reused), and, when the artefact holds
what scoring needs (both snapshots, the baseline, the calls, `oracle_context`), the trial is
scored again exactly as the runner scores it (`scoring.score_trial`). Nothing re-runs a trial
or touches a federation; the run's own artefacts are never rewritten.

Verdicts are cached in a JSONL file keyed by what the judge saw and how (scenario, brief, local
site, answer, rubric hash, model, effort), appended as each one lands, so an interrupted
re-judge resumes and a reader fixed later costs no new call.

Output: `<out dir>/rejudge.jsonl` (one row per trial) and `<out dir>/rejudge.md` (the
agree/disagree counts per scenario and every disagreement).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmark.k2val.artefact import latest_per_trial
from benchmark.k2val.judge import Judge, JudgeConf, JudgeVerdict, judge_input_sha
from benchmark.k2val.rubrics import ANSWER_SPECS, rubric_sha

DEFAULT_LOCAL = "vistula-dc"
# Outcomes whose trials reached the oracle: their answers, empty ones included, were read.
SCORED_OUTCOMES = ("PASS", "FAIL", "NEEDS_REVIEW")
_LOCAL_IN_BRIEF = re.compile(r"your site (?:the provider )?`?([a-z][\w.-]*)`?", re.I)


@dataclass
class StoredTrial:
    source: str  # the run directory's name
    file: str  # the artefact file's name
    line: int
    row: dict[str, Any]

    @property
    def key(self) -> str:
        return f"{self.source}/{self.file}#{self.line}"

    @property
    def scenario_id(self) -> str:
        return self.row["scenario_id"]

    @property
    def answer(self) -> str:
        return self.row.get("final_answer") or ""


def iter_trials(run_dirs: Iterable[Path], *, all_records: bool = False) -> Iterator[StoredTrial]:
    """The stored trials of each run directory. A trial that has several records (a resumed
    run appends) is read once: its last valid record, else its last (`latest_per_trial`),
    within its own run directory. A record keeps its own file and line, so its key (and a
    cached verdict) stays the same. `all_records=True`: every line."""
    for run_dir in run_dirs:
        stored: list[StoredTrial] = []
        for path in sorted(run_dir.glob("*__*.jsonl")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                row = json.loads(line)
                if isinstance(row, dict) and "scenario_id" in row:
                    stored.append(StoredTrial(run_dir.name, path.name, i, row))
        yield from stored if all_records else latest_per_trial(stored, lambda t: t.row)


def local_site(trial: StoredTrial, by_source: dict[str, str]) -> str:
    """The agent's site: recorded by newer artefacts, else read from a brief of the same run that
    names it (older V4-V7 briefs say only "your site")."""
    ctx = trial.row.get("oracle_context") or {}
    if ctx.get("local_name"):
        return ctx["local_name"]
    return by_source.get(trial.source, DEFAULT_LOCAL)


def locals_by_source(trials: list[StoredTrial]) -> dict[str, str]:
    out: dict[str, str] = {}
    for t in trials:
        m = _LOCAL_IN_BRIEF.search(t.row.get("brief") or "")
        if m and t.source not in out:
            out[t.source] = m.group(1).rstrip(".:?")
    return out


def cache_key(sid: str, brief: str, local: str, answer: str, conf: JudgeConf) -> str:
    payload = json.dumps(
        [sid, brief, local, answer, rubric_sha(ANSWER_SPECS[sid]), conf.model, conf.effort]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class VerdictCache:
    """Judge verdicts by `cache_key`, kept in a JSONL file. Failed verdicts are not reused."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._rows: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    if not row["verdict"].get("error"):
                        self._rows[row["key"]] = row

    def get(self, key: str) -> JudgeVerdict | None:
        row = self._rows.get(key)
        return JudgeVerdict.from_json(row["scenario_id"], row["verdict"]) if row else None

    def put(self, key: str, trial_key: str, verdict: JudgeVerdict) -> None:
        row = {
            "key": key,
            "trial": trial_key,
            "scenario_id": verdict.scenario_id,
            "verdict": verdict.to_json(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if not verdict.error:
            self._rows[key] = row


def _requoted(verdict: JudgeVerdict, answer: str) -> JudgeVerdict:
    """A reused verdict, its evidence checked against the answer by the current rule."""
    from benchmark.k2val.judge import evidence_quoted

    if verdict.error is None:
        verdict.evidence_quoted = evidence_quoted(verdict.evidence, answer)
    return verdict


async def judge_trials(
    trials: list[StoredTrial],
    judge: Judge | None,
    conf: JudgeConf,
    cache: VerdictCache,
    *,
    on_verdict: Any = None,
) -> dict[str, JudgeVerdict]:
    """A verdict per scored trial (one that reached the oracle: PASS, FAIL or NEEDS_REVIEW, an
    empty final answer included): the trial's own stored verdict when it was given by the same
    model and effort under the same judge input (`scoring.stored_verdict`), else the cache, else
    `judge` (None: no call; a trial without a verdict gets one with `error="not judged"`)."""
    from benchmark.k2val.scoring import stored_verdict

    by_source = locals_by_source(trials)
    out: dict[str, JudgeVerdict] = {}

    async def one(t: StoredTrial) -> None:
        sid = t.scenario_id
        brief = t.row.get("brief") or ""
        local = local_site(t, by_source)
        own = stored_verdict(t.row)
        if own is not None and (own.model, own.effort) == (conf.model, conf.effort):
            out[t.key] = _requoted(own, t.answer)
            return
        key = cache_key(sid, brief, local, t.answer, conf)
        cached = cache.get(key)
        expected = judge_input_sha(
            sid, brief=brief, local=local, answer=t.answer, model=conf.model, effort=conf.effort
        )
        if cached is not None and cached.input_sha == expected:
            out[t.key] = _requoted(cached, t.answer)
            return
        if judge is None:
            out[t.key] = JudgeVerdict(scenario_id=sid, error="not judged (no cached verdict)")
            return
        verdict = await judge(sid, brief=brief, local=local, answer=t.answer)
        cache.put(key, t.key, verdict)
        out[t.key] = verdict
        if on_verdict:
            on_verdict(t, verdict)

    todo = [
        t
        for t in trials
        if t.scenario_id in ANSWER_SPECS and t.row.get("outcome") in SCORED_OUTCOMES
    ]
    await asyncio.gather(*[one(t) for t in todo])
    return out


# ---------------------------------------------------------------------------------------------
# The comparison (offline, with the current readers)
# ---------------------------------------------------------------------------------------------


def compare(trials: list[StoredTrial], verdicts: dict[str, JudgeVerdict]) -> list[dict[str, Any]]:
    """One row per judged trial: the deterministic reading, the judge's, whether they agree,
    the stored outcome, and the new outcome where the artefact can be scored again."""
    from benchmark.k2val.reading import is_right, read_answer, right_reading
    from benchmark.k2val.rubrics import reading_to_json, same_reading
    from benchmark.k2val.scoring import rescore_stored

    by_source = locals_by_source(trials)
    rows = []
    for t in trials:
        verdict = verdicts.get(t.key)
        if verdict is None:
            continue
        local = local_site(t, by_source)
        det = read_answer(t.scenario_id, t.answer, t.row, local=local)
        agree = verdict.error is None and same_reading(det, verdict.value)
        decides = agree and verdict.evidence_quoted is True
        right = right_reading(t.scenario_id)
        row = {
            "trial": t.key,
            "scenario_id": t.scenario_id,
            "llm_name": t.row.get("llm_name"),
            "stored_outcome": t.row.get("outcome"),
            "answer": t.answer,
            "deterministic": reading_to_json(det),
            "judge": reading_to_json(verdict.value),
            "judge_error": verdict.error,
            "evidence": verdict.evidence,
            "evidence_quoted": verdict.evidence_quoted,
            "agree": agree,
            # what the scorer requires to decide without review: agreement AND a quoted evidence
            "decides": decides,
            # the reading a right answer has (None where it depends on more than the label)
            "right_reading": reading_to_json(right) if right is not None else None,
            "judge_says_right": None if verdict.error else is_right(t.scenario_id, verdict.value),
            "deterministic_says_right": is_right(t.scenario_id, det),
        }
        row["rescored"] = rescore_stored(t.row, verdict)
        rows.append(row)
    return rows


def summarise(rows: list[dict[str, Any]]) -> str:
    out = ["# Re-judge: deterministic reader vs judge\n"]
    per: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        c = per[r["scenario_id"]]
        c["trials"] += 1
        if r["judge_error"]:
            c["judge_error"] += 1
        elif not r["agree"]:
            c["disagree"] += 1
        elif not r.get("decides", True):
            c["unquoted"] += 1
        else:
            c["agree"] += 1
    out.append("| scenario | trials | agree | disagree | evidence not quoted | judge error |")
    out.append("|---|---|---|---|---|---|")
    for sid in sorted(per):
        c = per[sid]
        out.append(
            f"| {sid} | {c['trials']} | {c['agree']} | {c['disagree']} | {c['unquoted']} | "
            f"{c['judge_error']} |"
        )
    total = Counter()
    for c in per.values():
        total.update(c)
    out.append(
        f"| all | {total['trials']} | {total['agree']} | {total['disagree']} | "
        f"{total['unquoted']} | {total['judge_error']} |\n"
    )

    doubt = [
        r
        for r in rows
        if r["stored_outcome"] == "PASS" and r["judge_says_right"] is False and not r["judge_error"]
    ]
    out.append("## Stored PASSes the judge reads as a wrong claim\n")
    out.extend(_block(r) for r in doubt) if doubt else out.append("None.\n")

    out.append("## Every disagreement (and every agreement whose evidence is not a quote)\n")
    bad = [r for r in rows if not r.get("decides", r["agree"])]
    out.extend(_block(r) for r in bad) if bad else out.append("None.\n")
    return "\n".join(out)


def _block(r: dict[str, Any]) -> str:
    quoted = "" if r["evidence_quoted"] in (True, None) else " (NOT a quote of the answer)"
    rescored = r.get("rescored") or {}
    lines = [
        f"### {r['trial']} ({r['scenario_id']}, {r['llm_name']}): stored {r['stored_outcome']}",
        f"- deterministic: `{r['deterministic']}`; judge: `{r['judge']}`"
        + (f"; judge error: {r['judge_error']}" if r["judge_error"] else ""),
        f"- right reading: `{r['right_reading']}`",
        f"- judge's evidence{quoted}: {r['evidence']!r}",
    ]
    if rescored:
        lines.append(f"- rescored now: {rescored}")
    lines.append("- answer:")
    lines.extend("  > " + ln for ln in (r["answer"] or "").splitlines() or [""])
    return "\n".join(lines) + "\n"


def write_outputs(rows: list[dict[str, Any]], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / "rejudge.jsonl"
    jsonl.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    md = out_dir / "rejudge.md"
    md.write_text(summarise(rows), encoding="utf-8")
    return jsonl, md
