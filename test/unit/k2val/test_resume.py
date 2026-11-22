"""A run that survives a federation that stops: `--resume`, the latest valid record per trial,
and the health gate before each trial (design/08, "Resuming a run"). No container, no model, no
real clock: a fake federation that can be switched off, and a fake clock that brings it back."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from benchmark.k2val import rejudge
from benchmark.k2val import run as run_module
from benchmark.k2val.artefact import (
    INFRASTRUCTURE_OUTCOMES,
    OUTCOMES,
    is_valid_outcome,
    latest_per_trial,
    read_trials,
    valid_trial_keys,
    write_trial,
)
from benchmark.k2val.config import ConfigError, parse_config
from benchmark.k2val.federation import FedApi, FederationUnreachable, provider_config, zone_config
from benchmark.k2val.gate import FederationGate, FederationWaitTimeout
from benchmark.k2val.judge import JudgeVerdict
from benchmark.k2val.report import build_report
from benchmark.k2val.runner import run_sweep, run_trial
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.spend import SpendTracker
from test.unit.k2val.conftest import TOKEN_LOCAL, TOKEN_OWNER, raw_config
from test.unit.k2val.fakefed import LOCAL_HOST, OWNER_HOST, ZONE_HOST, FakeClock
from test.unit.k2val.test_report import art
from test.unit.k2val.test_runner import make_deps, static

REPO = Path(__file__).resolve().parents[3]
DOWN = "federation unreachable; waiting"
ANSWER = "Not ready: at another provider."  # a PASS of V4


class OutageClock(FakeClock):
    """The fake clock, which brings the federation back once `up_at` has passed."""

    up_at: float | None = None

    async def sleep(self, seconds: float) -> None:
        await super().sleep(seconds)
        if self.up_at is not None and self.now >= self.up_at and self.fed is not None:
            self.fed.down = False
            self.up_at = None

    def outage(self, seconds: float) -> None:
        """The federation is down now and back `seconds` later."""
        self.fed.down = True
        self.up_at = self.now + seconds


@pytest.fixture
def clock(fed) -> OutageClock:  # replaces conftest's: `api`, the gate and the runner share it
    return OutageClock(fed)


def glm(cfg):
    return cfg.models["glm"]


# --- which records count --------------------------------------------------------------------


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_only_the_four_infrastructure_outcomes_are_not_results(outcome) -> None:
    assert INFRASTRUCTURE_OUTCOMES == (
        "RESET_FAIL",
        "HARNESS_ERROR",
        "ORACLE_ERROR",
        "ADAPTER_ERROR",
    )
    assert is_valid_outcome(outcome) is (outcome not in INFRASTRUCTURE_OUTCOMES)
    # NO_RESULT is the model's (a cap reached); NEEDS_REVIEW and the budget stops are results too
    assert all(is_valid_outcome(o) for o in ("NO_RESULT", "NEEDS_REVIEW", "BUDGET_STOP"))


def rows(*spec: tuple[int, str]) -> list[dict]:
    return [
        {"llm_name": "glm", "scenario_id": "V4", "trial_ix": ix, "outcome": outcome, "n": n}
        for n, (ix, outcome) in enumerate(spec)
    ]


def test_per_trial_the_last_valid_record_wins_else_the_last() -> None:
    got = latest_per_trial(
        rows(
            (0, "RESET_FAIL"),  # superseded by the later valid record
            (1, "PASS"),
            (0, "FAIL"),
            (1, "RESET_FAIL"),  # a later failure never replaces a valid record
            (2, "RESET_FAIL"),
            (2, "HARNESS_ERROR"),  # only failures: the last one is the record
            (3, "FAIL"),
            (3, "PASS"),  # two valid records: the last
        ),
        lambda r: r,
    )
    # the records that count, in the order they were written
    assert [(r["trial_ix"], r["outcome"], r["n"]) for r in got] == [
        (1, "PASS", 1),
        (0, "FAIL", 2),
        (2, "HARNESS_ERROR", 5),
        (3, "PASS", 7),
    ]


def test_a_record_that_does_not_say_which_trial_it_is_counts_for_itself() -> None:
    old = [{"llm_name": "glm", "scenario_id": "V3n", "outcome": o} for o in ("PASS", "PASS")]
    assert latest_per_trial(old, lambda r: r) == old


def test_the_trials_with_a_valid_record_of_a_run(tmp_path) -> None:
    run_dir = tmp_path / "run"
    for ix, outcome in enumerate(
        ("PASS", "RESET_FAIL", "NO_RESULT", "ADAPTER_ERROR", "HARNESS_ERROR", "ORACLE_ERROR")
    ):
        write_trial(art("glm", "V4", ix, outcome), run_dir)
    write_trial(art("glm", "V4", 1, "FAIL"), run_dir)  # trial 1 later ran, and failed validly
    write_trial(art("qwen36", "V2", 0, "NEEDS_REVIEW"), run_dir)
    assert valid_trial_keys(run_dir) == {
        ("glm", "V4", 0),
        ("glm", "V4", 1),
        ("glm", "V4", 2),
        ("qwen36", "V2", 0),
    }


# --- the report and the rescore count each trial once ---------------------------------------


def test_the_report_counts_each_planned_trial_once_and_not_an_earlier_infrastructure_failure(
    cfg, tmp_path
) -> None:
    run_dir = tmp_path / "run"
    write_trial(art("glm", "V4", 0, "RESET_FAIL", error="fixture: connection_error"), run_dir)
    write_trial(art("glm", "V4", 1, "PASS"), run_dir)
    write_trial(art("glm", "V4", 0, "PASS"), run_dir)  # the resume ran trial 0 again
    meta = {"k": 2, "models": ["glm"], "scenarios": ["V4"]}

    text = build_report(run_dir, cfg, run_id="R", meta=meta)

    assert "| 2/2 (pass^2=1) |" in text  # not INCOMPLETE, not 3 trials
    assert "INCOMPLETE" not in text and "RESET_FAIL" not in text
    assert "## Harness and deployment problems\n\nNone." in text
    assert "1 earlier record(s) of trials that were run again are superseded" in text
    assert len(read_trials(run_dir, all_records=True)) == 3  # the files keep them


def test_a_trial_that_never_ran_validly_keeps_its_last_failure_in_the_report(cfg, tmp_path) -> None:
    run_dir = tmp_path / "run"
    write_trial(art("glm", "V4", 0, "RESET_FAIL", error="first outage"), run_dir)
    write_trial(art("glm", "V4", 0, "RESET_FAIL", error="second outage"), run_dir)
    write_trial(art("glm", "V4", 1, "PASS"), run_dir)
    text = build_report(
        run_dir, cfg, run_id="R", meta={"k": 2, "models": ["glm"], "scenarios": ["V4"]}
    )
    assert "second outage" in text and "first outage" not in text
    assert "INCOMPLETE" not in text  # both planned indices have a record
    assert "1/1 +1 RESET_FAIL" in text


def test_the_rescore_reads_each_trial_once_and_keeps_the_line_it_came_from(tmp_path) -> None:
    run_dir = tmp_path / "R1"
    run_dir.mkdir()
    brief = "A placement rule keeps `/s/logs-x/log_20200107.dat` at vistula-dc. Remove that rule."
    base = {"llm_name": "glm", "scenario_id": "V3n", "brief": brief}
    records = [
        {**base, "trial_ix": 0, "outcome": "FAIL", "final_answer": "I removed the rule."},
        {**base, "trial_ix": 0, "outcome": "PASS", "final_answer": "No rule remains on that file."},
        {**base, "trial_ix": 1, "outcome": "ORACLE_ERROR", "final_answer": ""},
        {**base, "trial_ix": 1, "outcome": "PASS", "final_answer": "There is no rule to remove."},
    ]
    (run_dir / "glm__V3n.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    other = tmp_path / "R2"  # the same trial of ANOTHER run is not this run's
    other.mkdir()
    (other / "glm__V3n.jsonl").write_text(
        json.dumps({**base, "trial_ix": 0, "outcome": "FAIL"}) + "\n"
    )

    trials = list(rejudge.iter_trials([run_dir, other]))

    assert [(t.source, t.row["trial_ix"], t.row["outcome"]) for t in trials] == [
        ("R1", 0, "PASS"),
        ("R1", 1, "PASS"),
        ("R2", 0, "FAIL"),
    ]
    assert [t.key for t in trials] == [
        "R1/glm__V3n.jsonl#1",
        "R1/glm__V3n.jsonl#3",
        "R2/glm__V3n.jsonl#0",
    ]
    assert len(list(rejudge.iter_trials([run_dir], all_records=True))) == 4
    verdicts = {
        t.key: JudgeVerdict("V3n", value="NO_RULE", evidence="no rule", evidence_quoted=True)
        for t in trials
    }
    scored = rejudge.compare(trials, verdicts)
    assert [(r["trial"], r["stored_outcome"]) for r in scored] == [
        ("R1/glm__V3n.jsonl#1", "PASS"),
        ("R1/glm__V3n.jsonl#3", "PASS"),
        ("R2/glm__V3n.jsonl#0", "FAIL"),
    ]


# --- a resumed run ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resume_runs_only_the_trials_without_a_valid_record(
    cfg, api, fed, clock, tmp_path
) -> None:
    ran: list[str] = []

    def script_for(conf, on_cost):
        async def script(app, brief):
            ran.append(brief)
            return ANSWER

        return script

    deps = make_deps(cfg, api, clock, tmp_path, script_for)
    for ix, outcome in enumerate(("PASS", "RESET_FAIL", "NO_RESULT", "ADAPTER_ERROR")):
        write_trial(art("glm", "V4", ix, outcome), deps.run_dir)
    skip = valid_trial_keys(deps.run_dir)
    assert skip == {("glm", "V4", 0), ("glm", "V4", 2)}

    arts = await run_sweep(deps, [glm(cfg)], [SCENARIOS["V4"]], 4, skip=skip)

    assert sorted((a.trial_ix, a.outcome) for a in arts) == [(1, "PASS"), (3, "PASS")]
    assert len(ran) == 2
    now = {r["trial_ix"]: r["outcome"] for r in read_trials(deps.run_dir)}
    assert now == {0: "PASS", 1: "PASS", 2: "NO_RESULT", 3: "PASS"}  # NO_RESULT kept, not re-run
    assert len(read_trials(deps.run_dir, all_records=True)) == 6  # appended, nothing rewritten


@pytest.mark.asyncio
async def test_a_config_with_nothing_left_to_run_is_not_touched(
    cfg, api, fed, clock, tmp_path, monkeypatch
) -> None:
    from benchmark.k2val.fixtures import FixtureBuilder

    swept: list[str] = []
    real = FixtureBuilder.sweep_space

    async def spy(self):
        swept.append(self.space)
        return await real(self)

    monkeypatch.setattr(FixtureBuilder, "sweep_space", spy)
    deps = make_deps(cfg, api, clock, tmp_path, static(ANSWER))
    skip = {("glm", "V4", 0), ("glm", "V4", 1)}

    arts = await run_sweep(deps, [glm(cfg), cfg.models["qwen36"]], [SCENARIOS["V4"]], 2, skip=skip)

    assert {a.llm_name for a in arts} == {"qwen36"}
    assert swept == ["k2val-qwen36"]  # glm's space was not even swept


# --- the run command ---------------------------------------------------------------------------


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """A one-config panel for `run_main` (the same shape as the CLI-leg test's), with the sweep
    replaced by a fake that records what it was asked to run."""
    from benchmark.k2val import runner

    token = tmp_path / "t"
    token.write_text(TOKEN_LOCAL)
    key = tmp_path / "k"
    key.write_text("lk-key-for-the-test")
    toml = tmp_path / "k2val.toml"
    toml.write_text(
        f"""
[federation.onezone]
host = "{ZONE_HOST}"
[federation.local]
name = "vistula-dc"
host = "{LOCAL_HOST}"
token_file = "{token}"
[federation.owner]
name = "nordic-marine-dc"
host = "{OWNER_HOST}"
token_file = "{token}"
[run]
artefact_root = "{tmp_path / "artefacts"}"
research_dir = "{tmp_path / "research"}"
[models.glm]
backend = "openai_compat"
api_id = "zai-org/GLM-5.3-Flash"
space = "k2val-glm"
base_url = "https://llmlab.example/api/v1"
key_file = "{key}"
"""
    )
    for var in (
        "ONEDATA_MCP_DOTENV",
        "ONEDATA_ONEPROVIDER_HOST",
        "ONEDATA_ONEPROVIDER_TOKEN",
        "ONEDATA_ONEZONE_HOST",
        "ONEDATA_ONEZONE_TOKEN",
        "ONEDATA_ALLOW_INSECURE_TLS",
    ):
        monkeypatch.setenv(var, "restored-by-monkeypatch")
    seen: list[dict] = []

    async def fake_sweep(deps, models, scenarios, k, *, on_trial=None, **kw):
        seen.append({"models": [m.name for m in models], "k": k, **kw})
        return []

    monkeypatch.setattr(runner, "run_sweep", fake_sweep)

    def go(*args: str) -> int:
        return run_module.main(["--config", str(toml), *args])

    return SimpleNamespace(
        go=go, seen=seen, root=tmp_path / "artefacts", research=tmp_path / "research"
    )


def start_run(panel, outcomes: dict[int, str], run_id: str = "r1") -> Path:
    """The artefacts of an earlier, interrupted run of glm / V4."""
    run_dir = panel.root / run_id
    for ix, outcome in outcomes.items():
        write_trial(art("glm", "V4", ix, outcome), run_dir)
    return run_dir


def test_a_resume_skips_the_valid_trials_and_says_how_many(panel, capsys) -> None:
    start_run(panel, {0: "PASS", 1: "RESET_FAIL", 2: "NO_RESULT"})

    code = panel.go("--scenarios", "V4", "--k", "4", "--run-id", "r1", "--resume")

    out = capsys.readouterr().out
    assert code == 0
    assert panel.seen == [{"models": ["glm"], "k": 4, "skip": {("glm", "V4", 0), ("glm", "V4", 2)}}]
    assert "2 of 4 planned trial(s) already have a valid record and are skipped; 2 will run" in out
    assert (panel.research / "k2val-r1.md").exists()


def test_a_run_id_that_has_artefacts_is_refused_without_resume_and_left_alone(
    panel, capsys
) -> None:
    run_dir = start_run(panel, {0: "PASS", 1: "RESET_FAIL"})
    before = {p.name: p.read_text() for p in run_dir.iterdir()}

    code = panel.go("--scenarios", "V4", "--k", "2", "--run-id", "r1")

    err = capsys.readouterr().err
    assert code == 2
    assert "--resume" in err and "new --run-id" in err
    assert panel.seen == []  # nothing was run
    assert {p.name: p.read_text() for p in run_dir.iterdir()} == before  # nothing was written
    assert not (panel.research / "k2val-r1.md").exists()


def test_resume_needs_an_existing_run(panel, capsys) -> None:
    assert panel.go("--scenarios", "V4", "--resume") == 2
    assert "--resume needs --run-id" in capsys.readouterr().err
    assert panel.go("--scenarios", "V4", "--run-id", "typo", "--resume") == 2
    assert "holds no run to continue" in capsys.readouterr().err
    assert not (panel.root / "typo").exists()  # a mistyped id does not start a new run
    assert panel.seen == []


def test_a_new_run_id_and_a_default_one_start_a_run_as_before(panel) -> None:
    assert panel.go("--scenarios", "V4", "--k", "1", "--run-id", "fresh") == 0
    assert panel.seen == [{"models": ["glm"], "k": 1}]  # no skip: today's behaviour
    assert (panel.root / "fresh" / "tools_snapshot.json").exists()
    assert panel.go("--scenarios", "V4", "--k", "1") == 0  # a timestamp: nothing to refuse


def test_a_resume_with_nothing_to_run_still_writes_the_report_and_exits_0(panel, capsys) -> None:
    start_run(panel, {0: "PASS", 1: "FAIL"})

    code = panel.go("--scenarios", "V4", "--k", "2", "--run-id", "r1", "--resume")

    assert code == 0
    assert (
        "2 of 2 planned trial(s) already have a valid record and are skipped; 0 will run"
        in capsys.readouterr().out
    )
    assert (panel.research / "k2val-r1.md").exists()
    assert (panel.root / "r1" / "run.json").exists()


def test_a_narrower_resume_does_not_shrink_the_recorded_plan(panel) -> None:
    run_dir = start_run(panel, {0: "PASS"})
    (run_dir / "run.json").write_text(
        json.dumps(
            {"k": 3, "models": ["glm", "qwen36"], "scenarios": ["V4", "V5"], "started_at": 1.0}
        )
    )

    assert panel.go("--scenarios", "V4", "--k", "2", "--run-id", "r1", "--resume") == 0

    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["models"] == ["glm", "qwen36"] and meta["scenarios"] == ["V4", "V5"]
    assert meta["k"] == 3 and meta["started_at"] == 1.0 and len(meta["resumed_at"]) == 1


@pytest.mark.asyncio
async def test_the_tool_snapshot_of_a_resumed_run_is_kept_or_written_beside(
    cfg, tmp_path, capsys
) -> None:
    from benchmark.k2val.run import _tools_snapshot

    tmp_path = tmp_path / "snapshots"
    tmp_path.mkdir()

    def listing(description: str):
        async def list_tools():
            return [
                SimpleNamespace(name=n, description=description, parameters={})
                for n in cfg.tools_k2
            ]

        return SimpleNamespace(list_tools=list_tools)

    await _tools_snapshot(listing("one"), cfg, tmp_path)
    first = (tmp_path / "tools_snapshot.json").read_text()
    await _tools_snapshot(listing("one"), cfg, tmp_path, resumed=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tools_snapshot.json"]
    await _tools_snapshot(listing("two"), cfg, tmp_path, resumed=True)
    assert (tmp_path / "tools_snapshot.json").read_text() == first
    beside = [p.name for p in tmp_path.iterdir() if p.name.startswith("tools_snapshot.resumed-")]
    assert len(beside) == 1 and "differ" in capsys.readouterr().out


def test_make_passes_resume_to_the_run_command() -> None:
    def dry(*args: str) -> str:
        out = subprocess.run(
            ["make", "-n", "k2val", *args], capture_output=True, text=True, cwd=REPO
        )
        assert out.returncode == 0, out.stderr
        return out.stdout

    assert "--run-id r1" in dry("K2VAL_RID=r1", "K2VAL_RESUME=1") and "--resume" in dry(
        "K2VAL_RID=r1", "K2VAL_RESUME=1"
    )
    assert "--resume" not in dry("K2VAL_RID=r1")


# --- the federation health gate ------------------------------------------------------------------


class ScriptedFed:
    """`ping` fails with FederationUnreachable until the fake clock reaches `up_at`."""

    def __init__(self, clock, up_at: float | None) -> None:
        self.clock, self.up_at, self.pings = clock, up_at, 0

    async def ping(self) -> None:
        self.pings += 1
        if self.up_at is None or self.clock.time() < self.up_at:
            raise FederationUnreachable("local provider: connection_error")


def gate_of(fed, clock, wait_s: float = 21600.0) -> FederationGate:
    return FederationGate(fed, clock, wait_s=wait_s, poll_s=60.0)


@pytest.mark.asyncio
async def test_the_gate_lets_a_reachable_federation_through_at_once(capsys) -> None:
    clock = FakeClock()
    fed = ScriptedFed(clock, up_at=0.0)

    await gate_of(fed, clock).wait()

    assert (clock.sleeps, fed.pings, capsys.readouterr().out) == (0, 1, "")


@pytest.mark.asyncio
async def test_the_gate_waits_polls_every_minute_and_says_so_once(capsys) -> None:
    clock = FakeClock()
    fed = ScriptedFed(clock, up_at=clock.time() + 150)  # back after 150 s: seen at the poll at 180

    await gate_of(fed, clock).wait()

    assert clock.now - 1_000_000.0 == 180 and clock.sleeps == 3
    out = capsys.readouterr().out.splitlines()
    assert out == ["[k2val] " + DOWN, "[k2val] federation resumed after 180 s"]


@pytest.mark.asyncio
async def test_the_gate_gives_up_after_the_bound_with_the_reason_a_reset_fail_carries(
    capsys,
) -> None:
    clock = FakeClock()
    fed = ScriptedFed(clock, up_at=None)

    with pytest.raises(FederationWaitTimeout) as raised:
        await gate_of(fed, clock, wait_s=200).wait()

    assert str(raised.value).startswith("federation unreachable for 200 s")
    assert clock.now - 1_000_000.0 == 200  # polls of 60, 60, 60 and a last one of 20: never past it
    assert capsys.readouterr().out.count(DOWN) == 1


@pytest.mark.asyncio
async def test_one_outage_is_one_wait_for_every_config_and_every_later_trial_fails_fast(
    capsys,
) -> None:
    clock = FakeClock()
    fed = ScriptedFed(clock, up_at=None)
    gate = gate_of(fed, clock, wait_s=300)

    outcomes = await asyncio.gather(*[gate.wait() for _ in range(4)], return_exceptions=True)

    assert all(isinstance(o, FederationWaitTimeout) for o in outcomes)
    assert clock.now - 1_000_000.0 == 300  # not 4 x 300: the outage's clock is shared
    assert capsys.readouterr().out.count(DOWN) == 1
    with pytest.raises(FederationWaitTimeout):  # a trial that asks later fails at once
        await gate.wait()
    assert clock.now - 1_000_000.0 == 300
    fed.up_at = clock.time()  # the first probe that succeeds ends the outage
    await gate.wait()
    assert "federation resumed after 300 s" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_wait_of_zero_does_not_wait(capsys) -> None:
    clock = FakeClock()
    with pytest.raises(FederationWaitTimeout):
        await gate_of(ScriptedFed(clock, None), clock, wait_s=0).wait()
    assert clock.sleeps == 0 and DOWN not in capsys.readouterr().out


# --- the probe ---------------------------------------------------------------------------------


def api_with_zone(clock) -> FedApi:
    return FedApi(
        provider_config(LOCAL_HOST, TOKEN_LOCAL, insecure_tls=False),
        provider_config(OWNER_HOST, TOKEN_OWNER, insecure_tls=False),
        zone=zone_config(ZONE_HOST, TOKEN_LOCAL, insecure_tls=False),
        clock=clock,
    )


@pytest.mark.asyncio
async def test_the_probe_asks_each_service_for_its_configuration_and_names_the_one_down(
    fed, clock
) -> None:
    api = api_with_zone(clock)

    await api.ping()

    assert [(h, p) for _, h, p in fed.requests] == [
        (LOCAL_HOST, "/api/v3/oneprovider/configuration"),
        (OWNER_HOST, "/api/v3/oneprovider/configuration"),
        (ZONE_HOST, "/api/v3/onezone/configuration"),
        (LOCAL_HOST, "/api/v3/oneprovider/spaces"),
        (OWNER_HOST, "/api/v3/oneprovider/spaces"),
    ]
    fed.down = True
    with pytest.raises(FederationUnreachable, match="local provider: connection_error"):
        await api.ping()


@pytest.mark.asyncio
async def test_the_probe_calls_a_5xx_down_but_any_other_answer_up(httpx_mock) -> None:
    api = api_with_zone(FakeClock())
    for host in (LOCAL_HOST, OWNER_HOST):
        httpx_mock.add_response(
            url=f"{host}/api/v3/oneprovider/configuration", json={}, is_reusable=True
        )
        httpx_mock.add_response(
            url=f"{host}/api/v3/oneprovider/spaces", json=[], is_reusable=True, is_optional=True
        )
    zone = f"{ZONE_HOST}/api/v3/onezone/configuration"
    httpx_mock.add_response(url=zone, status_code=503)
    with pytest.raises(FederationUnreachable, match="zone: http_503"):
        await api.ping()
    httpx_mock.add_response(url=zone, status_code=401)  # up: a token is a trial's matter
    await api.ping()


@pytest.mark.asyncio
async def test_the_probe_gives_up_on_a_service_that_does_not_answer(httpx_mock) -> None:
    async def hang(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    httpx_mock.add_callback(hang, is_optional=True, is_reusable=True)
    with pytest.raises(FederationUnreachable, match="local provider: timeout"):
        await api_with_zone(FakeClock()).ping(timeout_s=0.05)


def test_a_federation_wait_is_a_run_setting(secret_files) -> None:
    raw = raw_config(secret_files)
    assert parse_config(raw).limits.federation_wait_s == 21600.0
    raw["run"]["federation_wait_s"] = 90
    assert parse_config(raw).limits.federation_wait_s == 90.0
    raw["run"]["federation_wait_s"] = -1
    with pytest.raises(ConfigError, match="federation_wait_s"):
        parse_config(raw)


# --- the gate in the runner ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_federation_that_dies_between_trials_makes_the_next_trial_wait_not_fail(
    cfg, api, fed, clock, tmp_path, capsys
) -> None:
    """The failure being guarded against: trial 1 ran, a load guard on the host stopped the
    containers, and every later trial failed at once (connection_error, RESET_FAIL). Now the
    next trial waits for the federation."""
    deps = make_deps(cfg, api, clock, tmp_path, static(ANSWER))

    def on_trial(a) -> None:
        if a.trial_ix == 0:  # the federation dies right after the first trial, for 10 minutes
            clock.outage(600)

    arts = await run_sweep(deps, [glm(cfg)], [SCENARIOS["V4"]], 3, on_trial=on_trial)

    assert [(a.trial_ix, a.outcome) for a in arts] == [(0, "PASS"), (1, "PASS"), (2, "PASS")]
    assert all(r["outcome"] == "PASS" for r in read_trials(deps.run_dir, all_records=True))
    out = capsys.readouterr().out
    assert out.count(DOWN) == 1 and "federation resumed after 600 s" in out
    assert arts[1].started_at >= arts[0].ended_at + 600  # it started once the federation was back


@pytest.mark.asyncio
async def test_a_federation_that_is_down_at_launch_is_waited_for_by_every_config_once(
    cfg, api, fed, clock, tmp_path, capsys
) -> None:
    """The sweep of each config's space is behind the gate too: a federation that is down then
    must not turn every planned trial of the config into a blank RESET_FAIL."""
    clock.outage(180)
    deps = make_deps(cfg, api, clock, tmp_path, static(ANSWER))

    arts = await run_sweep(deps, [glm(cfg), cfg.models["qwen36"]], [SCENARIOS["V4"]], 2)

    assert sorted(a.outcome for a in arts) == ["PASS"] * 4
    assert capsys.readouterr().out.count(DOWN) == 1  # one outage, one pause, two configs


@pytest.mark.asyncio
async def test_after_the_bound_the_trials_are_reset_fails_that_say_why_and_the_wait_is_not_repeated(
    cfg, api, fed, clock, tmp_path
) -> None:
    cfg = replace(cfg, limits=replace(cfg.limits, federation_wait_s=300.0))
    fed.down = True  # never comes back
    started = clock.time()
    deps = make_deps(cfg, api, clock, tmp_path, static(ANSWER))

    arts = await run_sweep(deps, [glm(cfg), cfg.models["qwen36"]], [SCENARIOS["V4"]], 2)

    assert [a.outcome for a in arts] == ["RESET_FAIL"] * 4
    assert all(a.error.startswith("federation unreachable for 300 s") for a in arts)
    assert all(a.fixture_error == a.error for a in arts)
    assert clock.time() - started == 300  # the wait of the outage, not one per config or trial


@pytest.mark.asyncio
async def test_a_trial_waits_for_the_federation_before_it_reserves_budget_and_times_out(
    cfg, api, fed, clock, tmp_path
) -> None:
    cfg = replace(cfg, limits=replace(cfg.limits, federation_wait_s=120.0))
    spend = SpendTracker(cap_usd=1.0, reserve_usd=0.5)
    seen: list[int] = []

    class SpyGate(FederationGate):
        async def wait(self) -> None:
            seen.append(spend.in_flight)  # what the budget holds while a trial waits
            await super().wait()

    fed.down = True
    gate = SpyGate(api, clock, wait_s=120.0)
    deps = make_deps(cfg, api, clock, tmp_path, static(ANSWER), spend=spend, gate=gate)

    art_ = await run_trial(deps, cfg.models["haiku-low"], SCENARIOS["V4"], 0)

    assert seen == [0] and spend.in_flight == 0
    assert art_.outcome == "RESET_FAIL" and art_.error.startswith(
        "federation unreachable for 120 s"
    )
    assert art_.fixture_error == art_.error and art_.rounds_used == 0  # the model was never called
    assert read_trials(deps.run_dir)[0]["outcome"] == "RESET_FAIL"


@pytest.mark.asyncio
async def test_the_probe_waits_while_a_provider_does_not_accept_the_token_yet(httpx_mock) -> None:
    """A provider just restarted answers /configuration before it can check a token with the
    zone; the panel's own requests then get 401 (seen on the 2026-11-22 runs)."""
    api = api_with_zone(FakeClock())
    for host in (LOCAL_HOST, OWNER_HOST):
        httpx_mock.add_response(
            url=f"{host}/api/v3/oneprovider/configuration", json={}, is_reusable=True
        )
    httpx_mock.add_response(
        url=f"{ZONE_HOST}/api/v3/onezone/configuration", json={}, is_reusable=True
    )
    spaces = f"{LOCAL_HOST}/api/v3/oneprovider/spaces"
    httpx_mock.add_response(url=spaces, status_code=401)
    with pytest.raises(FederationUnreachable, match="local provider: token not accepted yet"):
        await api.ping()
    httpx_mock.add_response(url=spaces, json=[])
    httpx_mock.add_response(url=f"{OWNER_HOST}/api/v3/oneprovider/spaces", json=[])
    await api.ping()
