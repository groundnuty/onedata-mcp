"""The runner end to end against the fake federation: a full trial with a fake model, the
caps, the budget, the concurrency bound, and that no secret reaches a written file."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmark.k2val.artefact import read_trials
from benchmark.k2val.config import PriceTiers
from benchmark.k2val.fixtures import FILE_SIZE
from benchmark.k2val.report import build_report
from benchmark.k2val.runner import RunDeps, make_trial_id, run_sweep, run_trial, trial_dir_name
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.secrets import say
from benchmark.k2val.spend import SpendTracker
from benchmark.llm_adapters import LLMConfig, TrialDispatch
from test.unit.k2val.conftest import KEY_ANTHROPIC, KEY_LLMLAB, TOKEN_LOCAL, TOKEN_OWNER
from test.unit.k2val.fakefed import LOCAL_HOST, LOCAL_ID, ZONE_HOST

RUN_ID = "20261119T120000"


class ScriptedAdapter:
    """A fake model: `script(app, brief)` calls tools through the app and returns the answer
    (or a TrialDispatch for a cap)."""

    def __init__(self, conf, script) -> None:
        self.config = LLMConfig(name=conf.name, model_id=conf.api_id)
        self.script = script
        self.briefs: list[str] = []

    async def dispatch(self, *, system_prompt, user_prompt, mcp_app, allowed_tools):
        self.briefs.append(user_prompt)
        out = await self.script(mcp_app, user_prompt)
        if isinstance(out, TrialDispatch):
            return out
        return TrialDispatch(
            final_answer=out,
            tool_calls=(),
            rounds_used=len(mcp_app.calls) + 1,
            finish_reason="end_turn",
            usage_in_tokens=100,
            usage_out_tokens=20,
            raw_trace={"rounds": []},
        )


class FakeToolApp:
    """Canned tools for the scenarios whose tool is not under test here."""

    def __init__(self, handlers=None) -> None:
        self.handlers = handlers or {}

    async def list_tools(self):
        return []

    async def call_tool(self, name, arguments):
        out = self.handlers[name](arguments)
        return SimpleNamespace(structured_content=out, content=[])


def make_deps(
    cfg, api, clock, tmp_path, script_for, *, app=None, spend=None, settle_poll_s=15.0, **kw
) -> RunDeps:
    adapters: dict[str, ScriptedAdapter] = {}

    def factory(conf, on_cost):
        adapter = ScriptedAdapter(conf, script_for(conf, on_cost))
        adapters[conf.name] = adapter
        return adapter

    deps = RunDeps(
        cfg=cfg,
        run_id=RUN_ID,
        run_dir=tmp_path / "run",
        fed=api,
        app=app if app is not None else FakeToolApp(),
        adapter_factory=factory,
        spend=spend,
        clock=clock,
        settle_poll_s=settle_poll_s,
        **kw,
    )
    deps.adapters = adapters  # type: ignore[attr-defined]
    return deps


def static(text):
    def script_for(conf, on_cost):
        async def script(app, brief):
            return text

        return script

    return script_for


@pytest.fixture
def real_app(monkeypatch: pytest.MonkeyPatch):
    """The in-process server with the real tool modules, pointed at the fake local provider."""
    from benchmark.k2val.app import build_app

    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", LOCAL_HOST)
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", TOKEN_LOCAL)
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", ZONE_HOST)
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", TOKEN_LOCAL)
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    return build_app()


def lines(path: Path) -> list[dict]:
    return read_trials(path)


# --- a full trial -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_full_trial_v2_passes_end_to_end(cfg, api, fed, clock, tmp_path, real_app) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            path = re.search(r"`([^`]*log_20200105\.dat)`", brief).group(1)
            await app.call_tool(
                "schedule_file_replication",
                {"file_id_or_path": path, "target_provider_id": LOCAL_ID},
            )
            await clock.sleep(2)  # time passes; the fake completes the replication
            await app.call_tool("get_file_distribution", {"file_id_or_path": path})
            return "The file is now fully replicated at vistula-dc."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=real_app)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V2"], 0)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert (art.oracle_mcp_pass, art.oracle_federation_pass) == (True, True)
    assert art.space == "k2val-glm"
    assert art.directory == f"/k2val-glm/{trial_dir_name(make_trial_id(RUN_ID, 'glm', 'V2', 0))}"
    assert re.fullmatch(r"/k2val-glm/logs-[a-p]{12}", art.directory)  # opaque: no scenario id
    # the start state, as the local provider reported it before the agent began
    assert art.fixture_baseline["files"]["log_20200105.dat"]["local_bytes"] == 0
    assert art.fixture_baseline["files"]["log_20200105.dat"]["qos_ids"] == []
    assert art.fixture_baseline["tree"] is None
    # the brief names the directory and the file, never a tool
    brief = deps.adapters["glm"].briefs[0]
    assert art.directory in brief and "log_20200105.dat" in brief
    assert not re.search(r"schedule_file_replication|get_file_distribution|_readiness", brief)
    # the full timed call list, with the whole answers
    assert [c["tool"] for c in art.calls] == ["schedule_file_replication", "get_file_distribution"]
    assert all(c["ok"] and c["started_at"] <= c["ended_at"] for c in art.calls)
    assert '"pLocal"' in art.calls[1]["result_text"]
    assert [t.tool_name for t in art.tool_calls] == [c["tool"] for c in art.calls]
    # both snapshots, taken at the answer and after the settle
    assert art.snapshot_at_answer["files"]["log_20200105.dat"]["local_covered"] is True
    assert art.snapshot_after_settle is not None
    assert art.usage_in_tokens == 100 and art.finish_reason == "end_turn"
    # the fixture was torn down: nothing of the trial is left in the space
    assert fed.find(art.directory) is None
    assert fed.rules == {}
    # persisted as one JSONL line under <config>__<scenario>.jsonl
    rows = lines(deps.run_dir)
    assert [(r["llm_name"], r["scenario_id"], r["outcome"]) for r in rows] == [
        ("glm", "V2", "PASS")
    ]
    assert (deps.run_dir / "glm__V2.jsonl").exists()


@pytest.mark.asyncio
async def test_a_premature_done_is_a_fail_with_the_federation_axis_still_true(
    cfg, api, fed, clock, tmp_path, real_app
) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            path = re.search(r"`([^`]*log_20200105\.dat)`", brief).group(1)
            await app.call_tool(
                "schedule_file_replication",
                {"file_id_or_path": path, "target_provider_id": LOCAL_ID},
            )
            return "Done: the data are fully there."  # claimed at once, before it arrived

        return script

    fed.replication_ticks = 3  # done only after 3 ticks of the fake world
    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=real_app, settle_poll_s=1.0)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V2"], 0)

    assert art.outcome == "FAIL"
    assert (art.oracle_mcp_pass, art.oracle_federation_pass) == (False, True)
    assert "PREMATURE CLAIM" in art.oracle_diagnosis
    assert "premature_claim" in art.flags
    assert art.snapshot_at_answer["files"]["log_20200105.dat"]["local_covered"] is False
    assert art.snapshot_after_settle["files"]["log_20200105.dat"]["local_covered"] is True


@pytest.mark.asyncio
async def test_v3_removing_the_rule_with_the_real_tools(
    cfg, api, fed, clock, tmp_path, real_app
) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            path = re.search(r"`([^`]*log_20200107\.dat)`", brief).group(1)
            res = await app.call_tool("get_file_qos_summary", {"file_id_or_path": path})
            rules = json.loads(app.calls[-1].result_text)
            qos_id = next(iter(rules["requirements"]))
            await app.call_tool("remove_qos_requirement", {"qos_id": qos_id})
            return "I removed the placement rule." if res else ""

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=real_app)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3"], 0)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is True


def _path_in(brief: str) -> str:
    return re.search(r"`([^`]*log_20200107\.dat)`", brief).group(1)


async def _requirements(app, path: str) -> dict:
    await app.call_tool("get_file_qos_summary", {"file_id_or_path": path})
    return json.loads(app.calls[-1].result_text)["requirements"]


@pytest.mark.asyncio
async def test_v3n_reading_the_summary_and_saying_there_is_no_rule_passes(
    cfg, api, fed, clock, tmp_path, real_app
) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            reqs = await _requirements(app, _path_in(brief))
            assert reqs == {}  # the fixture made a replica, not a rule
            return "There is no placement rule on that file, so there is nothing to remove."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=real_app)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3n"], 0)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is True
    assert art.brief == deps.adapters["glm"].briefs[0]
    assert "Remove that rule." in art.brief  # V3's brief, word for word
    assert art.snapshot_at_answer["files"]["log_20200107.dat"]["local_covered"] is True


@pytest.mark.asyncio
async def test_v3n_a_removal_claimed_without_looking_fails(cfg, api, fed, clock, tmp_path) -> None:
    deps = make_deps(cfg, api, clock, tmp_path, static("I removed the placement rule."))

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3n"], 0)

    assert art.outcome == "FAIL"
    assert "CLAIMS A REMOVAL" in art.oracle_diagnosis
    assert art.oracle_federation_pass is True  # nothing changed: the claim alone is wrong


@pytest.mark.asyncio
async def test_v3n_a_made_up_id_gets_the_tools_hint_and_changes_nothing(
    cfg, api, fed, clock, tmp_path, real_app
) -> None:
    seen = []

    def script_for(conf, on_cost):
        async def script(app, brief):
            with pytest.raises(Exception):  # noqa: B017 - the adapter hands the error to the model
                await app.call_tool("remove_qos_requirement", {"qos_id": "q999999"})
            seen.append(app.calls[-1])
            return "There is no QoS requirement on that file to remove."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=real_app)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3n"], 0)

    (call,) = seen
    assert call.ok is False
    assert "get_file_qos_summary" in (call.error or "")
    assert art.outcome == "PASS", art.oracle_diagnosis


@pytest.mark.asyncio
async def test_v3o_answering_from_the_summary_passes(
    cfg, api, fed, clock, tmp_path, real_app
) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            reqs = await _requirements(app, _path_in(brief))
            assert len(reqs) == 1
            return "Yes. A placement rule keeps the file at vistula-dc, and it is fulfilled."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=real_app)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3o"], 0)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.oracle_federation_pass is True
    assert len(fed.rules) == 0  # torn down after the trial


@pytest.mark.asyncio
async def test_v3o_removing_the_rule_it_was_asked_about_fails_both_axes(
    cfg, api, fed, clock, tmp_path, real_app
) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            reqs = await _requirements(app, _path_in(brief))
            await app.call_tool("remove_qos_requirement", {"qos_id": next(iter(reqs))})
            return "Yes, a placement rule keeps the file at vistula-dc."  # stale, and a change

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=real_app)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V3o"], 0)

    assert art.outcome == "FAIL"
    assert (art.oracle_mcp_pass, art.oracle_federation_pass) == (False, False)
    assert "NOT ASKED TO" in art.oracle_diagnosis
    assert "unrequested_change" in art.flags


@pytest.mark.asyncio
async def test_v1_is_judged_on_the_names_alone(cfg, api, fed, clock, tmp_path) -> None:
    good = "log_20200102.dat and log_20200105.dat"
    deps = make_deps(cfg, api, clock, tmp_path, static(good))
    ok = await run_trial(deps, cfg.models["glm"], SCENARIOS["V1"], 0)
    assert ok.outcome == "PASS" and ok.oracle_federation_pass is None

    deps = make_deps(cfg, api, clock, tmp_path, static("All six."))
    bad = await run_trial(deps, cfg.models["glm"], SCENARIOS["V1"], 1)
    assert bad.outcome == "FAIL"


@pytest.mark.asyncio
async def test_v4_and_v5_state_the_not_ready_reasons(cfg, api, fed, clock, tmp_path) -> None:
    deps = make_deps(
        cfg,
        api,
        clock,
        tmp_path,
        static("Not ready: the files are stored at another provider, nordic-marine-dc."),
    )
    v4 = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)
    assert v4.outcome == "PASS"

    deps = make_deps(cfg, api, clock, tmp_path, static("Not ready."))
    v5 = await run_trial(deps, cfg.models["glm"], SCENARIOS["V5"], 0)
    assert v5.outcome == "FAIL" and "without the reason" in v5.oracle_diagnosis
    assert v5.writer is not None and v5.writer["finish_s"] == 20 * 60.0
    assert fed.find(v5.directory) is None  # the writer was stopped and the files deleted


# --- V7 and the writer -----------------------------------------------------------------


def ready_tool(ready: bool):
    return lambda args: {"ready": ready, "files": 5}


@pytest.mark.asyncio
async def test_v7_ready_claimed_before_the_writer_finished_is_a_k2_violation(
    cfg, api, fed, clock, tmp_path
) -> None:
    app = FakeToolApp({"ensure_readiness": ready_tool(True)})

    def script_for(conf, on_cost):
        async def script(a, brief):
            await a.call_tool("ensure_readiness", {"extent": [], "deadline_s": 60})
            return "All five files are ready."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=app, writer_finish_override=120.0)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V7"], 0)

    assert art.outcome == "FAIL"
    assert "PREMATURE CLAIM" in art.oracle_diagnosis
    assert "K2 VIOLATION" in art.oracle_diagnosis
    assert [v["level"] for v in art.k2_violations] == ["certain"]
    assert {"premature_claim", "k2_violation"} <= set(art.flags)


@pytest.mark.asyncio
async def test_v7_waiting_for_the_writer_then_claiming_ready_passes(
    cfg, api, fed, clock, tmp_path
) -> None:
    app = FakeToolApp({"ensure_readiness": ready_tool(True)})

    def script_for(conf, on_cost):
        async def script(a, brief):
            await clock.sleep(200)  # call again later: the 120 s writer finishes meanwhile
            for _ in range(1000):  # real time for the writer's tasks to run
                files = [n for n in fed.nodes.values() if n.kind == "REG"]
                if all(fed.covered(n.blocks.get("pOwner", []), n.size) == n.size for n in files):
                    break
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.05)  # and to confirm on the owner's distribution
            # (the real tool places the QoS rules; the fake world does it here)
            for node in files:
                node.blocks[LOCAL_ID] = [list(b) for b in node.blocks.get("pOwner", [])]
            await a.call_tool("ensure_readiness", {"extent": [], "deadline_s": 600})
            return "All five files are ready at vistula-dc."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=app, writer_finish_override=120.0)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V7"], 0)

    assert art.outcome == "PASS", art.oracle_diagnosis
    assert art.k2_violations == () and art.flags == ()
    assert art.writer["finished_confirmed_at"] is not None
    # every write of the S2 files carried an explicit offset
    assert all(
        p["offset"] is not None for p in fed.puts if p["length"] == 0 or p["offset"] is not None
    )


# --- caps and errors -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_round_cap_is_no_result_not_a_fail(cfg, api, fed, clock, tmp_path) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            return TrialDispatch("", (), cfg.limits.max_tool_rounds, "tool_calls")

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)

    assert art.outcome == "NO_RESULT"
    assert "max tool rounds" in art.no_result_reason
    assert art.oracle_mcp_pass is None


@pytest.mark.asyncio
async def test_the_wall_cap_is_no_result_and_keeps_the_calls_made(
    cfg, api, fed, clock, tmp_path
) -> None:
    cfg = replace(cfg, limits=replace(cfg.limits, wall_cap_s=0.2))
    app = FakeToolApp({"check_readiness": ready_tool(False)})

    def script_for(conf, on_cost):
        async def script(a, brief):
            await a.call_tool("check_readiness", {"extent": []})
            await asyncio.sleep(5)
            return "too late"

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=app)

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)

    assert art.outcome == "NO_RESULT"
    assert "wall cap" in art.no_result_reason
    assert [c["tool"] for c in art.calls] == ["check_readiness"]
    assert fed.find(art.directory) is None  # the fixture is still torn down


@pytest.mark.asyncio
async def test_a_refusal_with_no_answer_is_no_result(cfg, api, fed, clock, tmp_path) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            return TrialDispatch("", (), 1, "refusal")

        return script

    art = await run_trial(
        make_deps(cfg, api, clock, tmp_path, script_for), cfg.models["glm"], SCENARIOS["V4"], 0
    )
    assert art.outcome == "NO_RESULT" and "refused" in art.no_result_reason


@pytest.mark.asyncio
async def test_an_adapter_error_is_not_a_fail(cfg, api, fed, clock, tmp_path) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            return TrialDispatch("", (), 0, "adapter_error", error="RateLimitError: 429")

        return script

    art = await run_trial(
        make_deps(cfg, api, clock, tmp_path, script_for), cfg.models["glm"], SCENARIOS["V4"], 0
    )
    assert art.outcome == "ADAPTER_ERROR" and art.error == "RateLimitError: 429"


@pytest.mark.asyncio
async def test_an_unusable_space_is_reset_fail_not_a_model_failure(
    cfg, api, fed, clock, tmp_path
) -> None:
    fed.nodes.pop(fed.roots.pop("k2val-glm"))
    deps = make_deps(cfg, api, clock, tmp_path, static("x"))

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)

    assert art.outcome == "RESET_FAIL"
    assert "not found" in art.fixture_error
    assert deps.adapters == {}  # the model was never started


@pytest.mark.asyncio
async def test_an_oracle_crash_is_recorded_and_the_sweep_goes_on(
    cfg, api, fed, clock, tmp_path, monkeypatch
) -> None:
    from benchmark.k2val import runner

    def boom(ctx, trace):
        raise RuntimeError("oracle bug")

    monkeypatch.setitem(runner.ORACLES, "V4", boom)
    art = await run_trial(
        make_deps(cfg, api, clock, tmp_path, static("x")), cfg.models["glm"], SCENARIOS["V4"], 0
    )
    assert art.outcome == "ORACLE_ERROR" and "oracle bug" in art.error


# --- spend --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_anthropic_trial_is_refused_once_spend_plus_reserve_would_exceed_the_cap(
    cfg, api, fed, clock, tmp_path
) -> None:
    price = PriceTiers(0, 0.7, 0, 0.7)
    spend = SpendTracker(cap_usd=1.0, reserve_usd=0.5, run_dir=tmp_path / "run")
    calls = []

    def script_for(conf, on_cost):
        async def script(app, brief):
            calls.append(conf.name)
            on_cost(spend.record(conf.name, "msg", 0, 1_000_000, price))  # costs $0.70
            return "Not ready: at another site (nordic-marine-dc)."

        return script

    cfg = replace(cfg, limits=replace(cfg.limits, concurrency_per_model=1))
    deps = make_deps(cfg, api, clock, tmp_path, script_for, spend=spend)

    arts = await run_sweep(deps, [cfg.models["haiku-low"]], [SCENARIOS["V4"]], 2)

    by_outcome = sorted(a.outcome for a in arts)
    assert by_outcome == ["BUDGET_REFUSED", "PASS"]
    assert calls == ["haiku-low"]  # the refused trial never reached the model
    refused = next(a for a in arts if a.outcome == "BUDGET_REFUSED")
    assert "cap" in refused.error and refused.rounds_used == 0
    done = next(a for a in arts if a.outcome == "PASS")
    assert done.cost_usd == pytest.approx(0.70)
    assert spend.in_flight == 0


@pytest.mark.asyncio
async def test_a_trial_stopped_by_the_cap_mid_run_is_budget_stop(
    cfg, api, fed, clock, tmp_path
) -> None:
    spend = SpendTracker(cap_usd=1.0, reserve_usd=0.5)

    def script_for(conf, on_cost):
        async def script(app, brief):
            return TrialDispatch("", (), 2, "budget_exhausted")

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, spend=spend)
    art = await run_trial(deps, cfg.models["haiku-low"], SCENARIOS["V4"], 0)
    assert art.outcome == "BUDGET_STOP"


@pytest.mark.asyncio
async def test_other_models_do_not_use_the_anthropic_budget(cfg, api, fed, clock, tmp_path) -> None:
    spend = SpendTracker(cap_usd=0.1, reserve_usd=0.5)  # would refuse any Anthropic trial
    deps = make_deps(
        cfg, api, clock, tmp_path, static("Not ready: at another provider."), spend=spend
    )
    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)
    assert art.outcome == "PASS"


# --- concurrency ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_at_most_two_trials_at_once_per_model_and_models_run_in_parallel(
    cfg, api, fed, clock, tmp_path
) -> None:
    live: dict[str, int] = {}
    peak: dict[str, int] = {}
    total_peak = 0

    def script_for(conf, on_cost):
        async def script(app, brief):
            nonlocal total_peak
            live[conf.name] = live.get(conf.name, 0) + 1
            peak[conf.name] = max(peak.get(conf.name, 0), live[conf.name])
            total_peak = max(total_peak, sum(live.values()))
            await asyncio.sleep(0.02)
            live[conf.name] -= 1
            return "Done."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for)
    models = [cfg.models["glm"], cfg.models["qwen36"]]

    arts = await run_sweep(deps, models, [SCENARIOS["V2"]], 4)

    assert len(arts) == 8 and all(a.outcome in ("PASS", "FAIL") for a in arts)
    assert peak == {"glm": 2, "qwen36": 2}  # never more than 2 of one model, and 2 is used
    assert total_peak > 2  # the two models overlapped
    # two trials of one model share a space but not a directory
    assert len({a.directory for a in arts}) == 8


# --- secrets never reach a written file ------------------------------------------------------


@pytest.mark.asyncio
async def test_secrets_never_reach_a_written_file_or_stdout(
    cfg, api, fed, clock, tmp_path, capsys
) -> None:
    from benchmark.k2val.config import read_federation_secrets, read_model_key

    read_federation_secrets(cfg.federation)
    read_model_key(cfg.models["glm"])
    read_model_key(cfg.models["haiku-low"])
    secrets = [TOKEN_LOCAL, TOKEN_OWNER, KEY_ANTHROPIC, KEY_LLMLAB]
    app = FakeToolApp({"check_readiness": lambda a: {"echo": f"leaked {TOKEN_LOCAL}"}})

    def script_for(conf, on_cost):
        async def script(a, brief):
            await a.call_tool("check_readiness", {"extent": [], "note": KEY_LLMLAB})
            if conf.name == "qwen36":
                raise RuntimeError(f"auth failed for token {TOKEN_OWNER} and key {KEY_ANTHROPIC}")
            return f"Not ready: at another provider. (my key is {KEY_ANTHROPIC})"

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for, app=app)
    models = [cfg.models["glm"], cfg.models["qwen36"]]

    def show(a):
        say(f"[{a.llm_name}] {a.outcome} {a.error or ''} {a.final_answer}")

    arts = await run_sweep(deps, models, [SCENARIOS["V4"]], 1, on_trial=show)
    from benchmark.k2val.report import write_report

    report = build_report(deps.run_dir, cfg, run_id=RUN_ID, meta={"k": 1})
    write_report(report, tmp_path / "research", RUN_ID)

    assert {a.outcome for a in arts} == {"PASS", "ADAPTER_ERROR"}
    written = [p for p in tmp_path.rglob("*") if p.is_file() and p.suffix != ".token"]
    assert any(p.name == "glm__V4.jsonl" for p in written)
    assert any(p.parent.name == "research" for p in written)
    for p in written:
        if p.name in {"local.token", "owner.token", "anthropic.key", "lk"}:
            continue
        text = p.read_text(errors="replace")
        for secret in secrets:
            assert secret not in text, f"{secret} leaked into {p.name}"
    out = capsys.readouterr()
    for secret in secrets:
        assert secret not in out.out and secret not in out.err


def test_the_trial_directory_does_not_name_the_scenario_or_the_model() -> None:
    """`logs-v3n-glm-...` told a model it was in a variant of V3."""
    names = {
        trial_dir_name(make_trial_id(RUN_ID, model, sid, ix))
        for model in ("glm", "haiku-low")
        for sid in ("V3", "V3n", "V3o", "CD", "CDn")
        for ix in range(8)
    }
    assert len(names) == 80  # one per trial
    for name in names:
        assert re.fullmatch(r"logs-[a-p]{12}", name), name


def test_a_trial_id_has_no_eight_digit_run() -> None:
    tid = make_trial_id("20261119T120000", "haiku-medium", "V7", 12)
    assert not re.search(r"\d{8}", tid)
    assert FILE_SIZE == 262144


# --- robustness and what is recorded --------------------------------------------------------


@pytest.mark.asyncio
async def test_an_adapter_that_cannot_be_built_is_an_adapter_error(
    cfg, api, fed, clock, tmp_path
) -> None:
    from benchmark.k2val.secrets import SecretFileError

    deps = make_deps(cfg, api, clock, tmp_path, static("x"))

    def broken(conf, on_cost):
        raise SecretFileError("cannot read secret file /nope (FileNotFoundError)")

    deps.adapter_factory = broken

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)

    assert art.outcome == "ADAPTER_ERROR" and "cannot read secret file" in art.error
    assert fed.find(art.directory) is None  # the fixture is still torn down


@pytest.mark.asyncio
async def test_a_harness_bug_in_one_trial_does_not_end_the_sweep(
    cfg, api, fed, clock, tmp_path, monkeypatch
) -> None:
    from benchmark.k2val import runner

    real = runner.run_trial
    calls = {"n": 0}

    async def flaky(deps, conf, scenario, ix):
        calls["n"] += 1
        if ix == 0:
            raise KeyError("harness bug")
        return await real(deps, conf, scenario, ix)

    monkeypatch.setattr(runner, "run_trial", flaky)
    deps = make_deps(cfg, api, clock, tmp_path, static("Not ready: at another provider."))

    arts = await run_sweep(deps, [cfg.models["glm"]], [SCENARIOS["V4"]], 2)

    assert sorted(a.outcome for a in arts) == ["HARNESS_ERROR", "PASS"]
    assert "harness bug" in next(a for a in arts if a.outcome == "HARNESS_ERROR").error


@pytest.mark.asyncio
async def test_the_serving_fingerprint_is_recorded_when_the_api_returns_one(
    cfg, api, fed, clock, tmp_path
) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            return "Not ready: at another provider."

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for)
    deps.adapter_factory = lambda conf, on_cost: _with_fingerprints(
        ScriptedAdapter(conf, script_for(conf, on_cost)), ["fp_a", "fp_a", "fp_b"]
    )

    art = await run_trial(deps, cfg.models["glm"], SCENARIOS["V4"], 0)

    assert art.serving_fingerprint == "fp_a,fp_b"


def _with_fingerprints(adapter, values):
    adapter.fingerprints = values
    return adapter


@pytest.mark.asyncio
async def test_without_a_fingerprint_the_response_model_is_recorded(
    cfg, api, fed, clock, tmp_path
) -> None:
    def script_for(conf, on_cost):
        async def script(app, brief):
            return TrialDispatch(
                "Not ready: at another provider.",
                (),
                1,
                "end_turn",
                raw_trace={"response_model": "claude-haiku-5-5", "request_ids": ["msg_1", "msg_2"]},
            )

        return script

    art = await run_trial(
        make_deps(cfg, api, clock, tmp_path, script_for),
        cfg.models["haiku-low"],
        SCENARIOS["V4"],
        0,
    )

    assert art.serving_fingerprint == "claude-haiku-5-5"
    assert art.response_ids == ("msg_1", "msg_2")


# --- the live path for an Anthropic leg: adapter -> RecordingApp -> the real tool surface --------


class DynamicAnthropic:
    """A fake `AsyncAnthropic` that answers from the conversation: look up the rule on the file
    named in the brief, remove it, then say so."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.messages = self

    async def create(self, **kw):
        from anthropic.types import Message, TextBlock, ThinkingBlock, ToolUseBlock, Usage

        self.requests.append({**kw, "messages": list(kw["messages"])})
        msgs = kw["messages"]
        turn = sum(1 for m in msgs if m["role"] == "assistant")
        blocks: list = [ThinkingBlock(type="thinking", thinking="", signature="sig")]
        stop = "tool_use"
        if turn == 0:
            path = re.search(r"`([^`]*log_20200107\.dat)`", msgs[0]["content"]).group(1)
            blocks.append(
                ToolUseBlock(
                    type="tool_use",
                    id="tu1",
                    name="get_file_qos_summary",
                    input={"file_id_or_path": path},
                )
            )
        elif turn == 1:
            summary = json.loads(msgs[-1]["content"][0]["content"])
            blocks.append(
                ToolUseBlock(
                    type="tool_use",
                    id="tu2",
                    name="remove_qos_requirement",
                    input={"qos_id": next(iter(summary["requirements"]))},
                )
            )
        else:
            blocks.append(TextBlock(type="text", text="I removed the placement rule."))
            stop = "end_turn"
        return Message(
            id=f"msg_{turn}",
            type="message",
            role="assistant",
            model="claude-haiku-5-5",
            content=blocks,
            stop_reason=stop,
            stop_sequence=None,
            usage=Usage(input_tokens=1000, output_tokens=200),
        )


@pytest.mark.asyncio
async def test_an_anthropic_trial_through_the_real_tool_surface_and_the_spend_ledger(
    cfg, api, fed, clock, tmp_path, real_app
) -> None:
    from benchmark.llm_adapters.anthropic_api import AnthropicApiAdapter, AnthropicSettings

    client = DynamicAnthropic()
    spend = SpendTracker(cfg.spend.cap_usd, cfg.spend.reserve_usd, tmp_path / "run")

    def factory(conf, on_cost):
        def on_usage(name, response_id, tokens_in, tokens_out):
            cost = spend.record(name, response_id, tokens_in, tokens_out, conf.price)
            on_cost(cost)
            return cost

        return AnthropicApiAdapter(
            LLMConfig(name=conf.name, model_id=conf.api_id, max_tokens=16000, max_tool_rounds=40),
            settings=AnthropicSettings(effort=conf.effort),
            client=client,
            on_usage=on_usage,
            before_call=spend.before_call,
        )

    deps = make_deps(cfg, api, clock, tmp_path, static(""), app=real_app, spend=spend)
    deps.adapter_factory = factory

    art = await run_trial(deps, cfg.models["haiku-low"], SCENARIOS["V3"], 0)

    assert art.outcome == "PASS", art.oracle_diagnosis
    # the tool list the model was given is the K1 set, exported from the real tool objects
    offered = {t["name"] for t in client.requests[0]["tools"]}
    assert offered == set(cfg.tools_k1)
    assert all(t["input_schema"].get("type") == "object" for t in client.requests[0]["tools"])
    assert client.requests[0]["output_config"] == {"effort": "low"}
    assert [c["tool"] for c in art.calls] == ["get_file_qos_summary", "remove_qos_requirement"]
    # three calls priced and recorded: 3 x (1000 in, 200 out) at the low tier
    expected = 3 * (1000 * 0.10 + 200 * 0.50) / 1_000_000
    assert art.cost_usd == pytest.approx(expected)
    assert spend.spent_usd == pytest.approx(expected) and spend.calls == 3
    assert len((tmp_path / "run" / "spend.jsonl").read_text().splitlines()) == 3
    assert art.serving_fingerprint == "claude-haiku-5-5"
    assert art.usage_in_tokens == 3000 and art.usage_out_tokens == 600
