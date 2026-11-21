"""The rubric judge: the second reader of every final answer (design/10, gate 4; design/08).

The deterministic reader of a scenario (`reading.py`) and this judge each turn the final answer
into a value of the scenario's `AnswerSpec` (`rubrics.py`): a label, or a set of names. When the
two agree, that value decides; when they differ, or the judge fails, the trial is NEEDS_REVIEW
(`scoring.py`). The judge is never a silent fallback, and never the only reader.

What the judge sees: a fixed rubric (system prompt) per scenario, built from the brief exactly as
the agent saw it, the label set with one-line definitions, and the instruction to label what the
answer claims, not whether it is true. The user message is the answer. It never sees the
federation, the snapshots or the tool calls.

How it runs: Claude Code on the operator's subscription through `claude-agent-sdk`, as the
`claude_sdk` backend does (`claude_sdk.py`): `setting_sources=[]`, no tool at all (`tools=[]`
and every built-in disallowed), no MCP server (`--strict-mcp-config` with none), slash
commands off, an empty working directory, the API-key variables blanked and the `init`
message's `apiKeySource` checked. The verdict is asked for as structured output (the SDK's
`output_format`, the CLI's `--json-schema`) and checked again here against the label set.

Failures: a call that times out, hits a rate limit or errors is a `judge_error`; a verdict that
is not the schema's JSON is retried once, then a `judge_error`. Every verdict records the model,
the effort and the rubric's hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from benchmark.k2val.rubrics import (
    ANSWER_SPECS,
    RETRY_NOTE,
    AnswerSpec,
    Reading,
    normalise_names,
    output_schema,
    reading_from_json,
    reading_to_json,
    rubric,
    rubric_sha,
    user_message,
)

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "medium"
EVIDENCE_MAX = 400


class MalformedVerdict(ValueError):
    """The judge's output is not a verdict of the scenario's schema."""


@dataclass(frozen=True)
class JudgeConf:
    """`[judge]` of k2val.toml."""

    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    binary: str = ""  # the `claude` executable; default: `claude` on PATH, else the SDK's copy
    timeout_s: float = 180.0
    concurrency: int = 2  # judge calls at once, across every config of a run


@dataclass
class JudgeVerdict:
    scenario_id: str
    value: Reading = None
    evidence: str = ""
    error: str | None = None
    model: str = ""
    effort: str = ""
    rubric_sha: str = ""
    # `input_sha`: the digest of what this verdict's call sent (the rendered system and user
    # messages, the schema, the model, the effort); a verdict is reused only for the same one
    input_sha: str = ""
    attempts: int = 0
    raw: list[str] = field(default_factory=list)
    seconds: float = 0.0
    # the evidence is a passage of the answer (whitespace and emphasis aside)
    evidence_quoted: bool | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "value": reading_to_json(self.value),
            "evidence": self.evidence,
            "error": self.error,
            "model": self.model,
            "effort": self.effort,
            "rubric_sha": self.rubric_sha,
            "input_sha": self.input_sha,
            "attempts": self.attempts,
            "raw": self.raw,
            "seconds": round(self.seconds, 2),
            "evidence_quoted": self.evidence_quoted,
        }

    @classmethod
    def from_json(cls, scenario_id: str, data: dict[str, Any]) -> JudgeVerdict:
        spec = ANSWER_SPECS[scenario_id]
        return cls(
            scenario_id=scenario_id,
            value=reading_from_json(spec, data.get("value")),
            evidence=data.get("evidence") or "",
            error=data.get("error"),
            model=data.get("model") or "",
            effort=data.get("effort") or "",
            rubric_sha=data.get("rubric_sha") or "",
            input_sha=data.get("input_sha") or "",
            attempts=int(data.get("attempts") or 0),
            raw=list(data.get("raw") or []),
            seconds=float(data.get("seconds") or 0.0),
            evidence_quoted=data.get("evidence_quoted"),
        )


def _input_sha(system: str, user: str, schema: dict, model: str, effort: str) -> str:
    payload = json.dumps(
        {
            "system": system,
            "user": user,
            "retry_note": RETRY_NOTE,
            "schema": schema,
            "model": model,
            "effort": effort,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def judge_input_sha(
    scenario_id: str, *, brief: str, local: str, answer: str, model: str, effort: str
) -> str:
    """The digest of the judge's input for this answer, rendered as `ClaudeSdkJudge` sends it:
    the system message (the rubric with the brief and the site), the user message (the answer),
    the retry note's template, the output schema, the model and the effort. A stored or cached
    verdict is reused only when its `input_sha` equals this."""
    spec = ANSWER_SPECS[scenario_id]
    return _input_sha(
        rubric(spec, brief=brief, local=local),
        user_message(answer),
        output_schema(spec),
        model,
        effort,
    )


class Judge(Protocol):
    """What the runner and the re-judge command call. A fake in the unit tests."""

    async def __call__(
        self, scenario_id: str, *, brief: str, local: str, answer: str
    ) -> JudgeVerdict: ...


# ---------------------------------------------------------------------------------------------
# Parsing a verdict
# ---------------------------------------------------------------------------------------------

_FENCE = re.compile(r"\A\s*```(?:json)?\s*(.*?)\s*```\s*\Z", re.S)
_SPACE = re.compile(r"\s+")
_MARKS = re.compile(r"[*`_]")


def parse_verdict(spec: AnswerSpec, payload: Any) -> tuple[Reading, str]:
    """(value, evidence) from the structured output (a dict) or the reply text (a JSON object,
    optionally in a ```json fence). Raises MalformedVerdict otherwise."""
    obj = payload
    if isinstance(payload, str):
        text = payload.strip()
        if m := _FENCE.match(text):
            text = m.group(1)
        try:
            obj = json.loads(text)
        except ValueError as e:
            raise MalformedVerdict(f"not JSON: {e}") from None
    if not isinstance(obj, dict):
        raise MalformedVerdict("not a JSON object")
    evidence = obj.get("evidence")
    if not isinstance(evidence, str):
        raise MalformedVerdict("no string 'evidence'")
    allowed = {
        "evidence",
        *(("label",) if spec.kind != "names" else ()),
        *(("names",) if spec.kind != "label" else ()),
    }
    if extra := sorted(set(obj) - allowed):
        raise MalformedVerdict(f"keys {extra} are not in the schema")
    label = obj.get("label")
    if spec.kind != "names" and label not in spec.labels:
        raise MalformedVerdict(f"label {label!r} is not one of {list(spec.labels)}")
    if spec.kind == "label":
        return label, evidence[:EVIDENCE_MAX]
    if "names" not in obj:
        raise MalformedVerdict("no 'names'")
    try:
        names = normalise_names(obj["names"])
    except ValueError as e:
        raise MalformedVerdict(str(e)) from None
    if spec.kind == "label_names":
        if names is None:
            raise MalformedVerdict("'names' must be a list")
        return (label, names), evidence[:EVIDENCE_MAX]
    return names, evidence[:EVIDENCE_MAX]


def _flat(text: str) -> str:
    return _SPACE.sub(" ", _MARKS.sub("", text)).strip().lower()


_ELLIPSIS = re.compile(r"\.\.\.|…|\[\.\.\.\]")


def evidence_quoted(evidence: str, answer: str) -> bool:
    """The evidence is a passage of the answer: one contiguous quote, or parts between ellipses
    found in the answer IN ORDER over spans that do not overlap (whitespace, emphasis and case
    aside), so "removed ... not" is not a quote of "I have not removed the rule.". An empty
    answer has only empty evidence."""
    if not answer.strip():
        return not evidence.strip()
    flat_answer = _flat(answer)
    for candidate in (evidence, _unescaped(evidence)):
        parts = [_flat(p).strip(" .,;:\"'“”") for p in _ELLIPSIS.split(candidate)]
        parts = [p for p in parts if p]
        if parts and _in_order(parts, flat_answer):
            return True
    return False


def _in_order(parts: list[str], text: str) -> bool:
    """Each part found after the end of the one before (the leftmost match of each is the
    earliest-ending one, so a greedy scan finds an ordering whenever one exists)."""
    at = 0
    for part in parts:
        found = text.find(part, at)
        if found < 0:
            return False
        at = found + len(part)
    return True


def _unescaped(evidence: str) -> str:
    """The evidence as the answer's text, when the judge copied it from the JSON string it was
    given (with `\\n`, `\\"` or `\\u003c` escapes)."""
    try:
        return json.loads(f'"{evidence}"')
    except ValueError:
        return (
            evidence.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\u003c", "<")
            .replace("\\u003e", ">")
        )


# ---------------------------------------------------------------------------------------------
# The judge on the subscription
# ---------------------------------------------------------------------------------------------


class ClaudeSdkJudge:
    """The judge of a run: one SDK conversation per verdict, at most `concurrency` at once."""

    def __init__(self, conf: JudgeConf) -> None:
        self.conf = conf
        self._sem = asyncio.Semaphore(max(1, conf.concurrency))

    async def __call__(
        self, scenario_id: str, *, brief: str, local: str, answer: str
    ) -> JudgeVerdict:
        spec = ANSWER_SPECS[scenario_id]
        verdict = JudgeVerdict(
            scenario_id=scenario_id,
            model=self.conf.model,
            effort=self.conf.effort,
            rubric_sha=rubric_sha(spec),
        )
        system = rubric(spec, brief=brief, local=local)
        prompt = user_message(answer)
        verdict.input_sha = _input_sha(
            system, prompt, output_schema(spec), self.conf.model, self.conf.effort
        )
        t0 = time.monotonic()
        async with self._sem:
            why = ""
            for attempt in (1, 2):
                verdict.attempts = attempt
                try:
                    payload, raw = await asyncio.wait_for(
                        self._ask(
                            spec, system, prompt + (RETRY_NOTE.format(why=why) if why else "")
                        ),
                        timeout=self.conf.timeout_s,
                    )
                except TimeoutError:
                    verdict.error = f"timeout after {self.conf.timeout_s:.0f}s"
                    break
                except Exception as e:  # noqa: BLE001 - a judge failure, recorded
                    verdict.error = f"{type(e).__name__}: {e}"[:500]
                    break
                verdict.raw.append(raw[:2000])
                try:
                    verdict.value, verdict.evidence = parse_verdict(spec, payload)
                    verdict.error = None
                    break
                except MalformedVerdict as e:
                    why = str(e)
                    verdict.error = f"malformed verdict after {attempt} attempt(s): {why}"
        verdict.seconds = time.monotonic() - t0
        if verdict.error is None:
            verdict.evidence_quoted = evidence_quoted(verdict.evidence, answer)
        return verdict

    async def _ask(self, spec: AnswerSpec, system: str, prompt: str) -> tuple[Any, str]:
        """One conversation. Returns (structured output or reply text, the raw reply)."""
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            SystemMessage,
            TextBlock,
            query,
        )

        from benchmark.k2val.mcp_http import new_scratch

        options, scratch = self._options(spec, system, new_scratch)
        text: list[str] = []
        result: Any = None
        try:
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    _check_subscription(msg.data or {})
                elif isinstance(msg, AssistantMessage):
                    if msg.error:
                        raise RuntimeError(f"assistant error {msg.error!r}")
                    text.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                elif isinstance(msg, ResultMessage):
                    result = msg
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if result is None:
            raise RuntimeError("the session ended without a result")
        if result.is_error or (result.subtype or "").startswith("error"):
            detail = result.result or "; ".join(result.errors or []) or result.subtype
            raise RuntimeError(f"{result.subtype}: {detail}"[:400])
        structured = getattr(result, "structured_output", None)
        reply = result.result or "".join(text)
        if structured is not None:
            return structured, json.dumps(structured, ensure_ascii=False)
        return reply, reply

    def _options(self, spec: AnswerSpec, system: str, new_scratch: Any) -> tuple[Any, Any]:
        from claude_agent_sdk import ClaudeAgentOptions

        from benchmark.k2val.claude_sdk import BUILTIN_TOOLS, clean_env

        cli = shutil.which(self.conf.binary) if self.conf.binary else shutil.which("claude")
        if self.conf.binary and cli is None:
            raise FileNotFoundError(f"judge.binary {self.conf.binary!r} is not executable")
        scratch = new_scratch("k2val-judge-")
        work = scratch / "work"
        work.mkdir()
        options = ClaudeAgentOptions(
            system_prompt=system,
            tools=[],
            allowed_tools=[],
            disallowed_tools=list(BUILTIN_TOOLS),
            mcp_servers={},
            permission_mode="default",
            max_turns=4,  # structured output may take a turn of its own
            model=self.conf.model,
            effort=self.conf.effort,  # type: ignore[arg-type]
            setting_sources=[],
            cwd=str(work),
            env=clean_env(60.0, self.conf.effort),
            extra_args={"strict-mcp-config": None, "disable-slash-commands": None},
            output_format={"type": "json_schema", "schema": output_schema(spec)},
            cli_path=cli,
        )
        return options, scratch


def _check_subscription(data: dict[str, Any]) -> None:
    from benchmark.k2val.claude_sdk import SUBSCRIPTION_KEY_SOURCES

    source = data.get("apiKeySource")
    if source not in SUBSCRIPTION_KEY_SOURCES:
        raise RuntimeError(f"the judge's CLI reports apiKeySource={source!r}, not the subscription")
    tools = [t for t in data.get("tools") or [] if isinstance(t, str)]
    unexpected = [t for t in tools if t != "StructuredOutput"]
    if unexpected:
        raise RuntimeError(f"the judge's session offers tools {unexpected}: it must have none")


def make_judge(conf: JudgeConf) -> Judge:
    return ClaudeSdkJudge(conf)
