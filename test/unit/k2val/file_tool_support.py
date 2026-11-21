"""Helpers of the file-tool scenarios' tests (test_scenarios_{fa,lc,cd}.py): oracle contexts
and snapshots, and a full trial with a scripted model on the fake federation."""

from __future__ import annotations

import json
import re

from benchmark._runtime_types import AgentTrace, RunContext
from benchmark.k2val.oracles import ORACLES, K2Context
from benchmark.k2val.runner import run_trial
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.snapshot import Snapshot
from benchmark.k2val.surfaces import RECORDED_SURFACES
from test.unit.k2val.test_runner import make_deps

MODEL = "glm"


def snap(tree: dict | None, error: str | None = None) -> Snapshot:
    return Snapshot(taken_at=1.0, tree=tree, tree_error=error)


def ctx(sid: str, at: Snapshot, after: Snapshot | None = None, baseline=None) -> K2Context:
    return K2Context(
        scenario_id=sid,
        run=RunContext(scenario_id=sid),
        local_name="vistula-dc",
        owner_name="nordic-marine-dc",
        file_names=[],
        at_answer=at,
        after_settle=after if after is not None else at,
        baseline_tree=baseline,
    )


def verdict(sid: str, c: K2Context, answer: str):
    return ORACLES[sid](c, AgentTrace(final_answer=answer))


async def trial(cfg, api, clock, tmp_path, app, sid: str, model):
    """One trial of `sid`; `model(app, brief)` calls tools and returns the final answer."""

    def script_for(conf, on_cost):
        return model

    # the real app: the recorded surface hashes are checked before every trial, as in a run
    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=app, surfaces=RECORDED_SURFACES)
    art = await run_trial(deps, cfg.models[MODEL], SCENARIOS[sid], 0)
    assert art.outcome != "HARNESS_ERROR" or "offered tools" not in (art.error or ""), art.error
    return art


def directory_of(brief: str) -> str:
    """The trial directory a brief names (without the path it asks to create)."""
    return re.search(r"`(/[^`]+?)(?:/archive|/2020/01|/reports/summary\.txt)?`", brief).group(1)


async def result_of(app, tool: str, arguments: dict):
    result = await app.call_tool(tool, arguments)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)
