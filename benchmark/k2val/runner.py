"""One trial, and the sweep over models x scenarios x K (design/08, Runner).

A trial: build the fixture in the config's own space (a fresh `logs-<trial>` directory), run the
model through the in-process MCP app (limits: max rounds, a wall cap, one tool call bounded),
snapshot the federation right after the final answer, let it settle, judge with the two-axis
oracle, tear the fixture down. Every trial yields one artefact, whatever happens; a cap reached
is `NO_RESULT`, never a `FAIL`.

Concurrency: at most two trials at once per config; configs run in parallel (each has its own
space, so they cannot touch each other's fixtures). Two trials of one config share the space
but not a directory, and each tears down only its own.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark._runtime_types import AgentTrace, RunContext, ToolCall
from benchmark.k2val.artefact import K2TrialArtefact, write_trial
from benchmark.k2val.clock import REAL_CLOCK, Clock
from benchmark.k2val.config import K2valConfig, ModelConf
from benchmark.k2val.federation import FedApi
from benchmark.k2val.fixtures import BuiltFixture, FixtureBuilder, FixtureSpec
from benchmark.k2val.legs import AdapterFactory
from benchmark.k2val.oracles import K2Context, k2_violations, settled
from benchmark.k2val.recording import CallRecord, RecordingApp
from benchmark.k2val.scenarios import SYSTEM_PROMPT_TEMPLATE, Scenario
from benchmark.k2val.scoring import context_to_json, score
from benchmark.k2val.secrets import redact, say
from benchmark.k2val.snapshot import Snapshot, UnusableSnapshot, take_snapshot
from benchmark.k2val.spend import SpendTracker

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
    The federation is read every `settle_poll_s` until the awaited end state shows or the window
    ends. "quiet" (every restraint scenario) awaits nothing, so the whole window is waited out
    and the last reading, taken at its end, is the one returned, even if the first reading
    already matched."""
    if scenario.settle == "none":
        return first
    window = scenario.settle_s if scenario.settle_s is not None else deps.cfg.limits.settle_s
    names = list(fixture.files)
    snap = first
    deadline = deps.clock.time() + window
    while not settled(scenario.settle, snap, names) and deps.clock.time() < deadline:
        await deps.clock.sleep(deps.settle_poll_s)
        snap = await take_snapshot(deps.fed, fixture, deps.clock)
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
        brief = scenario.render_brief(directory=directory, local=local_name, ids=fixture.ids())

        tools = _tools_for(deps, scenario)
        rec_app = RecordingApp(
            deps.app, allowed=tools, call_cap_s=cfg.limits.tool_call_cap_s, clock=deps.clock.time
        )
        # The surface the model gets is the scenario's, exactly (design/10: a tool that answers
        # the task without the one under promotion must not be there).
        offered = sorted(t.name for t in await rec_app.list_tools())
        if problem := _surface_problem(scenario, tools, offered):
            return persist(
                artefact(
                    "HARNESS_ERROR",
                    error=f"offered tools: {problem}",
                    fixture_started_at=fixture.started_at,
                    fixture_ready_at=fixture.ready_at,
                    offered_tools=tuple(offered),
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
        }
        writer_record = fixture.writer.record() if fixture.writer else None
        common["writer"] = writer_record
        flags: list[str] = []
        if writer_record and writer_record.get("error"):
            flags.append("writer_error")

        if error is not None:
            return persist(artefact("ADAPTER_ERROR", error=error, flags=tuple(flags), **common))
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
        )
        common["snapshot_after_settle"] = after_settle.to_json()
        common["oracle_context"] = context_to_json(ctx)
        trace = AgentTrace(final_answer=final_answer, tool_calls=common["tool_calls"])
        try:
            # One usability rule for every scenario (snapshot.py): nothing is judged on a reading
            # that failed, whatever the oracle would make of it. Then the oracle and the checks
            # every scenario of a kind shares (scoring.py).
            scored = score(scenario, ctx, trace)
        except UnusableSnapshot as e:
            return persist(
                artefact(
                    "ORACLE_ERROR",
                    error=f"unusable federation reading: {e}",
                    flags=tuple(dict.fromkeys([*flags, "snapshot_error"])),
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


def _surface_problem(scenario: Scenario, tools: frozenset[str], offered: list[str]) -> str | None:
    """Why the surface the model would see is not the scenario's, or None."""
    problems = []
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
        return await builder.purge_dir(dir_id) if dir_id else []
    except Exception as e:  # noqa: BLE001
        return [f"cleanup {type(e).__name__}: {e}"]


async def run_sweep(
    deps: RunDeps,
    models: list[ModelConf],
    scenarios: list[Scenario],
    k: int,
    *,
    on_trial: Callable[[K2TrialArtefact], None] | None = None,
) -> list[K2TrialArtefact]:
    """Every model x scenario x trial. At most `concurrency_per_model` trials of one model
    at once; models run in parallel."""
    out: list[K2TrialArtefact] = []

    async def one_model(conf: ModelConf) -> None:
        sem = asyncio.Semaphore(deps.cfg.limits.concurrency_per_model)
        builder = FixtureBuilder(deps.fed, conf.space, deps.clock)
        try:
            problems = await builder.sweep_space()
            if problems:
                deps.notes.append(f"{conf.name}: sweep of {conf.space}: {problems}")
        except Exception as e:  # noqa: BLE001 - a config whose space is unusable gets RESET_FAILs
            for scenario in scenarios:
                for ix in range(k):
                    art = _blank_artefact(
                        deps, conf, scenario, ix, "RESET_FAIL", f"sweep: {type(e).__name__}: {e}"
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

        await asyncio.gather(*[guarded(s, ix) for s in scenarios for ix in range(k)])

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
