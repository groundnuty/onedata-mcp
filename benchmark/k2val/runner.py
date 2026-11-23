"""One trial, and the sweep over models x scenarios x K (design/08, Runner).

A trial: build the fixture in the config's own space (a fresh `logs-<trial>` directory), run the
model through the in-process MCP app (limits: max rounds, a wall cap, one tool call bounded),
snapshot the federation right after the final answer, let it settle, judge with the two-axis
oracle, tear the fixture down. Every trial yields one artefact, whatever happens; a cap reached
is `NO_RESULT`, never a `FAIL`.

Concurrency: one trial at a time per config; configs run in parallel (each has its own space,
so they cannot touch each other's fixtures). A config's trials share its space, so they never
overlap: whatever the federation shows during a trial was done by that trial or was there
before it (design/08, "Isolation"). Each trial tears down only its own directory.

Before each trial's fixture the federation health gate (gate.py) waits for the federation to
answer, so a federation stopped mid-run costs time, not trials; a resumed run (`run_sweep(skip=)`)
runs only the trials that have no valid record yet (design/08, "Resuming a run").
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import traceback
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark._runtime_types import AgentTrace, RunContext, ToolCall
from benchmark.k2val.artefact import K2TrialArtefact, TrialKey, write_trial
from benchmark.k2val.clock import REAL_CLOCK, Clock
from benchmark.k2val.config import MAX_CONCURRENCY_PER_MODEL, K2valConfig, ModelConf
from benchmark.k2val.federation import FedApi
from benchmark.k2val.fixtures import BuiltFixture, FixtureBuilder, FixtureSpec
from benchmark.k2val.gate import FederationGate, FederationWaitTimeout
from benchmark.k2val.legs import AdapterFactory
from benchmark.k2val.oracles import K2Context, k2_violations, settled
from benchmark.k2val.recording import CallRecord, RecordingApp
from benchmark.k2val.scenarios import SYSTEM_PROMPT_TEMPLATE, TOOL_UNDER_TEST, Scenario
from benchmark.k2val.scoring import (
    REQUIRED,
    UnverifiableEvidence,
    context_to_json,
    score_dual,
)
from benchmark.k2val.secrets import redact, say
from benchmark.k2val.snapshot import Snapshot, UnusableSnapshot, take_snapshot
from benchmark.k2val.spend import SpendTracker
from benchmark.k2val.surfaces import RECORDED_SURFACES, surface_problem

TOOL_RESULT_CAP = 8 * 1024
# A dispatch that ends without an answer for one of these reasons is a cap reached.
CAP_REASONS = {
    "max_rounds": "max tool rounds reached",
    "refusal": "the model refused (stop_reason refusal)",
    "max_tokens": "max_tokens reached",
    "length": "max_tokens reached (finish_reason length)",
    "content_filter": "content filter",
}
SETTLE_POLL_S = 5.0
# Finish reasons that cut an answer short: NO_RESULT, whatever partial text came with them.
TRUNCATED = ("max_tokens", "length", "content_filter", "refusal")


def make_trial_id(run_id: str, model: str, scenario_id: str, ix: int) -> str:
    """Short, no 8-digit run (a file name's date is the first run of exactly 8 digits), unique
    per (run, config, scenario, trial): `v5-glm-k0-3fa91c`."""
    tag = hashlib.sha1(f"{run_id}/{model}/{scenario_id}/{ix}".encode()).hexdigest()[:6]
    return f"{scenario_id.lower()}-{model}-k{ix}-{tag}"


_DIGITS_TO_LETTERS = str.maketrans("0123456789", "ghijklmnop")


def trial_dir_name(trial_id: str) -> str:
    """The trial's directory, `logs-<12 letters>`: opaque, so a path never tells a model which
    scenario or variant it is in (`logs-v3n-...` did); `logs-` for the sweep at run start;
    letters only, so no run of 8 digits in a path reads as a date."""
    tag = hashlib.sha1(trial_id.encode()).hexdigest()[:12].translate(_DIGITS_TO_LETTERS)
    return f"logs-{tag}"


@dataclass
class RunDeps:
    cfg: K2valConfig
    run_id: str
    run_dir: Path
    fed: FedApi
    app: Any  # the FastMCP app (or a fake with list_tools / call_tool)
    adapter_factory: AdapterFactory
    spend: SpendTracker | None = None
    clock: Clock = REAL_CLOCK
    # tests: scale the background writer's schedule
    writer_finish_override: float | None = None
    settle_poll_s: float = SETTLE_POLL_S
    notes: list[str] = field(default_factory=list)
    # The second reader of every answer (judge.py); None: every judged trial is NEEDS_REVIEW.
    judge: Any = None
    # The recorded surface hash of each offered set (surfaces.py): before the model is called,
    # the complete listing it would get must hash to its set's recorded value. None (unit tests
    # with a fake app only) skips the check.
    surfaces: Mapping[frozenset[str], tuple[str, str]] | None = field(
        default_factory=lambda: RECORDED_SURFACES
    )
    # Before each trial's fixture the federation must answer (gate.py); a gate of the run, shared
    # by every config. Default: one that probes `fed` and waits `[run] federation_wait_s`.
    gate: FederationGate | None = None

    def __post_init__(self) -> None:
        if self.gate is None:
            self.gate = FederationGate(
                self.fed, self.clock, wait_s=self.cfg.limits.federation_wait_s
            )


def _tools_for(deps: RunDeps, scenario: Scenario) -> frozenset[str]:
    if scenario.tools is not None:  # a scenario's own set (the file-tool scenarios)
        return frozenset(scenario.tools)
    return frozenset(deps.cfg.tools_k2 if scenario.tier == "K2" else deps.cfg.tools_k1)


def _to_toolcall(rec: CallRecord) -> ToolCall:
    return ToolCall(
        tool_name=rec.tool,
        arguments=rec.arguments,
        succeeded=rec.ok,
        error=rec.error,
        result=(rec.result_text or "")[:TOOL_RESULT_CAP] or None,
    )


async def _settle(
    deps: RunDeps, fixture: BuiltFixture, scenario: Scenario, first: Snapshot
) -> Snapshot:
    """The federation after the settle window (`Scenario.settle_s`, else `[run] settle_s`).

    A restraint scenario, or one that requires the tool under promotion, waits out the whole
    window and is then read once more: an independent snapshot that STARTS after the deadline,
    never skipped because the first reading already looked settled (a change that shows late,
    after a poll, is still seen). The others (V2, V6, V7, CDo) read again every
    `settle_poll_s` until the end state they await shows or the window ends, at least once
    after the answer."""
    if scenario.settle == "none":
        return first
    window = scenario.settle_s if scenario.settle_s is not None else deps.cfg.limits.settle_s
    if scenario.restraint or scenario.id in REQUIRED or scenario.settle == "quiet":
        await deps.clock.sleep(window)
        return await take_snapshot(deps.fed, fixture, deps.clock)
    names = list(fixture.files)
    deadline = deps.clock.time() + window
    while True:
        await deps.clock.sleep(min(deps.settle_poll_s, max(deadline - deps.clock.time(), 0.0)))
        snap = await take_snapshot(deps.fed, fixture, deps.clock)
        if settled(scenario.settle, snap, names) or deps.clock.time() >= deadline:
            return snap


async def run_trial(
    deps: RunDeps, conf: ModelConf, scenario: Scenario, trial_ix: int
) -> K2TrialArtefact:
    cfg = deps.cfg
    started = deps.clock.time()
    trial_id = make_trial_id(deps.run_id, conf.name, scenario.id, trial_ix)
    dir_name = trial_dir_name(trial_id)
    directory = f"/{conf.space}/{dir_name}"
    local_name = cfg.federation.local.name
    brief = scenario.render_brief(directory=directory, local=local_name)
    cost = [0.0]

    def artefact(outcome: str, **kw: Any) -> K2TrialArtefact:
        base: dict[str, Any] = {
            "run_id": deps.run_id,
            "llm_name": conf.name,
            "llm_model_id": conf.api_id,
            "scenario_id": scenario.id,
            "trial_ix": trial_ix,
            "started_at": started,
            "ended_at": deps.clock.time(),
            "fixture_started_at": started,
            "fixture_ready_at": 0.0,
            "outcome": outcome,
            "final_answer": "",
            "rounds_used": 0,
            "finish_reason": None,
            "usage_in_tokens": 0,
            "usage_out_tokens": 0,
            "wall_clock_seconds": 0.0,
            "tool_calls": (),
            "oracle_mcp_pass": None,
            "oracle_federation_pass": None,
            "oracle_diagnosis": "",
            "trial_id": trial_id,
            "scenario_title": scenario.title,
            "tier": scenario.tier,
            "space": conf.space,
            "directory": directory,
            "brief": brief,
            "cost_usd": cost[0],
        }
        base.update(kw)
        if base.get("error"):
            base["error"] = redact(str(base["error"]))
        return K2TrialArtefact(**base)

    def persist(art: K2TrialArtefact) -> K2TrialArtefact:
        write_trial(art, deps.run_dir)
        return art

    # The federation must answer before anything is started or reserved: a trial that waits
    # holds no budget. A trial already running when it dies is not this gate's business.
    try:
        await deps.gate.wait()
    except FederationWaitTimeout as e:
        return persist(artefact("RESET_FAIL", error=str(e), fixture_error=str(e)))
    started = deps.clock.time()

    tracked = conf.is_anthropic and deps.spend is not None
    if tracked and not deps.spend.try_begin_trial():
        return persist(
            artefact(
                "BUDGET_REFUSED",
                error=f"Anthropic spend {deps.spend.spent_usd:.2f} + reserve would exceed the "
                f"cap {deps.spend.cap_usd:.2f}",
            )
        )

    builder = FixtureBuilder(deps.fed, conf.space, deps.clock)
    fixture: BuiltFixture | None = None
    teardown_problems: list[str] = []
    try:
        spec = scenario.fixture
        if deps.writer_finish_override is not None and spec.writer_finish_s is not None:
            spec = FixtureSpec(spec.files, deps.writer_finish_override)
        try:
            fixture = await builder.build(spec, dir_name)
        except Exception as e:  # noqa: BLE001 - the deployment's fault, never the model's
            partial = await _cleanup_partial(deps, builder, conf.space, dir_name)
            return persist(
                artefact(
                    "RESET_FAIL",
                    error=f"fixture: {type(e).__name__}: {e}",
                    fixture_error=f"{type(e).__name__}: {e}",
                    teardown_problems=tuple(partial),
                )
            )

        # a brief may give a file id, known only now
        brief = scenario.render_brief(
            directory=directory, local=local_name, ids=fixture.ids(), qos=fixture.qos_rules
        )

        tools = _tools_for(deps, scenario)
        rec_app = RecordingApp(
            deps.app, allowed=tools, call_cap_s=cfg.limits.tool_call_cap_s, clock=deps.clock.time
        )
        # The surface the model gets is the scenario's, exactly (design/10: a tool that answers
        # the task without the one under promotion must not be there).
        listing = await rec_app.list_tools()
        offered = sorted(t.name for t in listing)
        surface_sha: str | None = None
        problem = _surface_problem(scenario, tools, offered)
        if problem is None and deps.surfaces is not None:
            # names are not enough: the descriptions and schemas must be the recorded ones
            surface_sha, problem = surface_problem(listing, frozenset(offered), deps.surfaces)
        if problem:
            return persist(
                artefact(
                    "HARNESS_ERROR",
                    error=f"offered tools: {problem}",
                    fixture_started_at=fixture.started_at,
                    fixture_ready_at=fixture.ready_at,
                    offered_tools=tuple(offered),
                    offered_surface_sha=surface_sha,
                )
            )
        try:
            adapter = deps.adapter_factory(conf, lambda c: cost.__setitem__(0, cost[0] + c))
        except Exception as e:  # noqa: BLE001 - e.g. an unreadable key file
            return persist(
                artefact(
                    "ADAPTER_ERROR",
                    error=f"adapter: {type(e).__name__}: {e}",
                    fixture_started_at=fixture.started_at,
                    fixture_ready_at=fixture.ready_at,
                )
            )
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(local=local_name)

        first_turn_at = deps.clock.time()
        if fixture.writer is not None:
            fixture.writer.start()
        dispatch = None
        error: str | None = None
        no_result: str | None = None
        try:
            dispatch = await asyncio.wait_for(
                adapter.dispatch(
                    system_prompt=system_prompt,
                    user_prompt=brief,
                    mcp_app=rec_app,
                    allowed_tools=tools,
                ),
                timeout=cfg.limits.wall_cap_s,
            )
        except TimeoutError:
            no_result = f"wall cap {cfg.limits.wall_cap_s:.0f}s reached"
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        answer_at = deps.clock.time()

        # The federation right after the final turn: was a claim true when it was made?
        at_answer = await take_snapshot(deps.fed, fixture, deps.clock)

        final_answer = dispatch.final_answer if dispatch else ""
        finish_reason = dispatch.finish_reason if dispatch else None
        if dispatch is not None and dispatch.error:
            error = dispatch.error
        elif dispatch is not None and finish_reason in TRUNCATED:
            # cut by a cap or a filter: a partial answer is no answer (NO_RESULT, never a FAIL)
            no_result = CAP_REASONS[finish_reason] + (" (a partial answer)" if final_answer else "")
        elif dispatch is not None and not final_answer:
            if finish_reason == "budget_exhausted":
                no_result = "Anthropic spend cap reached"
            elif finish_reason in CAP_REASONS:
                no_result = CAP_REASONS[finish_reason]
            elif dispatch.rounds_used >= cfg.limits.max_tool_rounds:
                no_result = CAP_REASONS["max_rounds"]

        records = list(rec_app.calls)
        common: dict[str, Any] = {
            "fixture_started_at": fixture.started_at,
            "fixture_ready_at": fixture.ready_at,
            "final_answer": final_answer,
            "rounds_used": dispatch.rounds_used if dispatch else 0,
            "finish_reason": finish_reason,
            "usage_in_tokens": dispatch.usage_in_tokens if dispatch else 0,
            "usage_out_tokens": dispatch.usage_out_tokens if dispatch else 0,
            "wall_clock_seconds": (
                dispatch.wall_clock_seconds if dispatch else answer_at - first_turn_at
            ),
            "tool_calls": tuple(_to_toolcall(r) for r in records),
            "raw_trace": dispatch.raw_trace if dispatch else None,
            "serving_fingerprint": _fingerprint(adapter, dispatch),
            "response_ids": tuple((dispatch.raw_trace or {}).get("request_ids", ()))
            if dispatch
            else (),
            "snapshot_at_answer": at_answer.to_json(),
            "fixture_baseline": fixture.baseline(),
            "calls": tuple(r.to_json() for r in records),
            "directory": directory,
            "offered_tools": tuple(offered),
            "offered_surface_sha": surface_sha,
        }
        writer_record = fixture.writer.record() if fixture.writer else None
        common["writer"] = writer_record
        flags: list[str] = []
        if writer_record and writer_record.get("error"):
            flags.append("writer_error")

        if error is not None:
            return persist(artefact("ADAPTER_ERROR", error=error, flags=tuple(flags), **common))
        if writer_record and writer_record.get("error"):
            return persist(_writer_failed(artefact, writer_record, flags, common))
        if finish_reason == "budget_exhausted" and no_result:
            return persist(
                artefact("BUDGET_STOP", no_result_reason=no_result, flags=tuple(flags), **common)
            )
        if no_result is not None:
            return persist(
                artefact("NO_RESULT", no_result_reason=no_result, flags=tuple(flags), **common)
            )

        after_settle = await _settle(deps, fixture, scenario, at_answer)
        writer_record = fixture.writer.record() if fixture.writer else None
        common["writer"] = writer_record
        if writer_record and writer_record.get("error"):  # failed during the settle
            common["snapshot_after_settle"] = after_settle.to_json()
            return persist(_writer_failed(artefact, writer_record, flags, common))
        run_ctx = RunContext(
            scenario_id=scenario.id,
            fixture_paths={bf.path: bf.file_id for bf in fixture.files.values()},
            fixture_started_at=fixture.started_at,
            fixture_ready_at=fixture.ready_at,
            space_name=conf.space,
        )
        ctx = K2Context(
            scenario_id=scenario.id,
            run=run_ctx,
            local_name=local_name,
            owner_name=cfg.federation.owner.name,
            file_names=list(fixture.files),
            at_answer=at_answer,
            after_settle=after_settle,
            writer=writer_record,
            answer_at=answer_at,
            first_turn_at=first_turn_at,
            calls=records,
            fixture_qos={
                name: (fixture.qos_rules[name],) if name in fixture.qos_rules else ()
                for name in fixture.files
            },
            baseline_tree=fixture.baseline_tree,
            baseline_files=dict(fixture.files_baseline),
            directory=directory,
            dir_id=fixture.dir_id,
            local_provider_id=await deps.fed.provider_id("local"),
            space_id=fixture.space_id,
        )
        common["snapshot_after_settle"] = after_settle.to_json()
        common["oracle_context"] = context_to_json(ctx)
        trace = AgentTrace(final_answer=final_answer, tool_calls=common["tool_calls"])
        try:
            # One usability rule for every scenario (snapshot.py): nothing is judged on a reading
            # that failed, whatever the oracle would make of it. Then the answer, read by both
            # readers, the oracle and the checks every scenario of a kind shares (scoring.py).
            verdict = await _judge(deps, scenario, brief, local_name, final_answer)
            scored = score_dual(scenario, ctx, trace, verdict)
        except UnusableSnapshot as e:
            return persist(
                artefact(
                    "ORACLE_ERROR",
                    error=f"unusable federation reading: {e}",
                    flags=tuple(dict.fromkeys([*flags, "snapshot_error"])),
                    **common,
                )
            )
        except UnverifiableEvidence as e:
            return persist(
                artefact(
                    "HARNESS_ERROR",
                    error=f"unverifiable write: {e}",
                    flags=tuple(dict.fromkeys([*flags, "unverifiable_write"])),
                    **common,
                )
            )
        except Exception as e:  # noqa: BLE001 - recorded, so the sweep goes on
            return persist(
                artefact(
                    "ORACLE_ERROR",
                    error=f"oracle: {type(e).__name__}: {e}\n{traceback.format_exc()}",
                    flags=tuple(flags),
                    **common,
                )
            )
        common["writer"] = writer_record
        return persist(
            artefact(
                scored.outcome,
                oracle_mcp_pass=scored.oracle.mcp_pass,
                oracle_federation_pass=scored.oracle.federation_pass,
                oracle_diagnosis=scored.diagnosis,
                checks=tuple(c.to_json() for c in scored.checks),
                answer_reading=scored.answer_reading,
                flags=tuple(dict.fromkeys([*flags, *ctx.flags, *scored.flags])),
                k2_violations=tuple(k2_violations(ctx)),
                **common,
            )
        )
    finally:
        if fixture is not None:
            try:
                teardown_problems = await builder.teardown(fixture)
            except Exception as e:  # noqa: BLE001
                teardown_problems = [f"teardown {type(e).__name__}: {e}"]
            if teardown_problems:
                deps.notes.append(f"{trial_id}: teardown problems: {teardown_problems}")
        if tracked:
            deps.spend.end_trial()


def _writer_failed(artefact: Any, record: dict, flags: list[str], common: dict) -> Any:
    """The background writer failed (before or during the settle): the fixture never reached
    the state the scenario describes, so the trial is the deployment's (RESET_FAIL)."""
    return artefact(
        "RESET_FAIL",
        error=f"background writer: {record.get('error')}",
        fixture_error=f"background writer: {record.get('error')}",
        flags=tuple(dict.fromkeys([*flags, "writer_error"])),
        **common,
    )


async def _judge(deps: RunDeps, scenario: Scenario, brief: str, local: str, answer: str) -> Any:
    """The judge's verdict, or None when the run has no judge. A judge that raises gives a
    verdict with its error: NEEDS_REVIEW, never a silent fallback."""
    if deps.judge is None:
        return None
    from benchmark.k2val.judge import JudgeVerdict

    try:
        return await deps.judge(scenario.id, brief=brief, local=local, answer=answer)
    except Exception as e:  # noqa: BLE001 - the judge's failure, recorded
        return JudgeVerdict(scenario_id=scenario.id, error=f"{type(e).__name__}: {e}"[:500])


def _surface_problem(scenario: Scenario, tools: frozenset[str], offered: list[str]) -> str | None:
    """Why the surface the model would see is not the scenario's, or None."""
    problems = []
    under_test = TOOL_UNDER_TEST.get(scenario.id)
    if under_test is not None and (under_test not in tools or under_test not in offered):
        problems.append(f"{scenario.id} exercises {under_test}, which is not offered")
    if missing := sorted(tools - set(offered)):
        problems.append(f"the app does not offer {missing}")
    if extra := sorted(set(offered) - tools):
        problems.append(f"the model would also see {extra}")
    if excluded := sorted(set(scenario.excluded_tools) & (tools | set(offered))):
        problems.append(f"{scenario.id} must not be offered {excluded}")
    return "; ".join(problems) or None


def _fingerprint(adapter: Any, dispatch: Any) -> str | None:
    """What identifies the serving model: the API's `system_fingerprint` when it returns one
    (LLM Lab), else the model name in the response (Anthropic returns no fingerprint)."""
    fingerprints = list(dict.fromkeys(getattr(adapter, "fingerprints", None) or ()))
    if fingerprints:
        return ",".join(fingerprints)
    return ((dispatch.raw_trace or {}).get("response_model") if dispatch else None) or None


async def _cleanup_partial(
    deps: RunDeps, builder: FixtureBuilder, space: str, dir_name: str
) -> list[str]:
    """After a failed build: remove whatever directory the trial managed to create."""
    try:
        dir_id = await deps.fed.lookup("owner", f"/{space}/{dir_name}")
        problems = await builder.purge_dir(dir_id) if dir_id else []
        return problems + await builder.purge_root_rules()
    except Exception as e:  # noqa: BLE001
        return [f"cleanup {type(e).__name__}: {e}"]


async def run_sweep(
    deps: RunDeps,
    models: list[ModelConf],
    scenarios: list[Scenario],
    k: int,
    *,
    on_trial: Callable[[K2TrialArtefact], None] | None = None,
    skip: Collection[TrialKey] = (),
) -> list[K2TrialArtefact]:
    """Every model x scenario x trial, except the (config, scenario, trial index) in `skip` (a
    resumed run's trials that already have a valid record). One trial of a model at a time
    (never more than MAX_CONCURRENCY_PER_MODEL, whatever the limits say); models run in
    parallel."""
    out: list[K2TrialArtefact] = []
    done = set(skip)

    async def one_model(conf: ModelConf) -> None:
        sem = asyncio.Semaphore(
            max(1, min(deps.cfg.limits.concurrency_per_model, MAX_CONCURRENCY_PER_MODEL))
        )
        todo = [
            (scenario, ix)
            for scenario in scenarios
            for ix in range(k)
            if (conf.name, scenario.id, ix) not in done
        ]
        if not todo:  # a resume with nothing left of this config's: its space is not touched
            return
        builder = FixtureBuilder(deps.fed, conf.space, deps.clock)
        try:
            # a federation that is down now must not turn the sweep into a failure of every trial
            await deps.gate.wait()
            problems = await builder.sweep_space()
            if problems:
                deps.notes.append(f"{conf.name}: sweep of {conf.space}: {problems}")
        except Exception as e:  # noqa: BLE001 - a config whose space is unusable gets RESET_FAILs
            reason = str(e) if isinstance(e, FederationWaitTimeout) else None
            for scenario, ix in todo:
                art = _blank_artefact(
                    deps,
                    conf,
                    scenario,
                    ix,
                    "RESET_FAIL",
                    reason or f"sweep: {type(e).__name__}: {e}",
                )
                out.append(art)
                if on_trial:
                    on_trial(art)
            return

        async def guarded(scenario: Scenario, ix: int) -> None:
            async with sem:
                try:
                    art = await run_trial(deps, conf, scenario, ix)
                except Exception as e:  # noqa: BLE001 - a harness bug must not end the sweep
                    art = _blank_artefact(
                        deps,
                        conf,
                        scenario,
                        ix,
                        "HARNESS_ERROR",
                        f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
                    )
                out.append(art)
                if on_trial:
                    on_trial(art)

        await asyncio.gather(*[guarded(s, ix) for s, ix in todo])

    results = await asyncio.gather(*[one_model(m) for m in models], return_exceptions=True)
    for res in results:
        if isinstance(res, BaseException):
            say(f"[k2val] a model task failed: {type(res).__name__}: {res}")
    return out


def _blank_artefact(
    deps: RunDeps, conf: ModelConf, scenario: Scenario, ix: int, outcome: str, error: str
) -> K2TrialArtefact:
    now = deps.clock.time()
    art = K2TrialArtefact(
        run_id=deps.run_id,
        llm_name=conf.name,
        llm_model_id=conf.api_id,
        scenario_id=scenario.id,
        trial_ix=ix,
        started_at=now,
        ended_at=now,
        fixture_started_at=now,
        fixture_ready_at=0.0,
        outcome=outcome,
        final_answer="",
        rounds_used=0,
        finish_reason=None,
        usage_in_tokens=0,
        usage_out_tokens=0,
        wall_clock_seconds=0.0,
        tool_calls=(),
        oracle_mcp_pass=None,
        oracle_federation_pass=None,
        oracle_diagnosis="",
        error=redact(error),
        trial_id=make_trial_id(deps.run_id, conf.name, scenario.id, ix),
        scenario_title=scenario.title,
        tier=scenario.tier,
        space=conf.space,
        fixture_error=redact(error) if outcome == "RESET_FAIL" else None,
    )
    write_trial(art, deps.run_dir)
    return art


__all__ = ["RunDeps", "make_trial_id", "run_sweep", "run_trial", "time", "trial_dir_name"]
