"""The `claude_sdk` backend: Claude Code on the subscription through the claude-agent-sdk.

The SDK client is replaced by a fake (`ClaudeSDKClient` is monkeypatched in the adapter's
module) and so is the loopback MCP server: no real CLI is started and no model is called. What
the tests pin: the options the real client would be given (a clean room), the mapping of the
message stream and of the server's record to the trial's trace, the permission gate, the wall
cap, the config rules, and that the spend tracker never sees the backend.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import RateLimitInfo, ToolPermissionContext, ToolResultBlock

from benchmark.k2val import claude_sdk
from benchmark.k2val.artefact import read_trials
from benchmark.k2val.claude_sdk import (
    BUILTIN_TOOLS,
    TOOL_PREFIX,
    ClaudeSdkAdapter,
    build_options,
    clean_env,
    make_can_use_tool,
    thinking_option,
)
from benchmark.k2val.config import ConfigError, ModelConf, parse_config
from benchmark.k2val.recording import CallRecord, RecordingApp
from benchmark.k2val.report import _cost_cell, build_report
from benchmark.k2val.runner import RunDeps, run_trial
from benchmark.k2val.scenarios import SCENARIOS
from benchmark.k2val.spend import SpendTracker
from test.unit.k2val.conftest import raw_config

RUN_ID = "20261120T120000"
SERVER_URL = "http://127.0.0.1:41999/mcp"
TOOLS = frozenset({"check_readiness", "ensure_readiness", "get_file_distribution"})
CTX = ToolPermissionContext()


# --- fakes ---------------------------------------------------------------------------------


class FakeServer:
    """Stands in for the loopback onedata-mcp process: a URL and the calls it recorded."""

    instances: list[FakeServer] = []
    recorded: list[CallRecord] = []

    def __init__(self, config_path, tools, scratch) -> None:
        self.tools = tools
        self.scratch = scratch
        self.url = SERVER_URL
        self.entered = False
        self.exited = False
        FakeServer.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *exc) -> None:
        self.exited = True

    def records(self) -> list[CallRecord]:
        return list(FakeServer.recorded)


def sdk_init(**over: Any) -> SystemMessage:
    data = {
        "model": "claude-haiku-5-5",
        "apiKeySource": "none",
        "claude_code_version": "2.1.329",
        "tools": None,  # the fake client fills in what the options allow
        "mcp_servers": [{"name": "onedata", "status": "connected"}],
        **over,
    }
    return SystemMessage(subtype="init", data=data)


def mcp_server(state: str, tools=TOOLS, error=None) -> dict:
    """An entry of the SDK's `get_mcp_status()`: tools are listed once connected."""
    listed = [{"name": t} for t in sorted(tools)] if state == "connected" else []
    return {"name": "onedata", "status": state, "tools": listed, "error": error}


def assistant(*blocks, mid="msg_1", model="claude-haiku-5-5", error=None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model=model, message_id=mid, error=error)


def tool_use(tool_id: str, name: str, **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=tool_id, name=name, input=args)


def tool_result(tool_id: str, text: str, is_error: bool = False) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=tool_id, content=text, is_error=is_error)]
    )


def result(**over: Any) -> ResultMessage:
    base = dict(
        subtype="success",
        duration_ms=1200,
        duration_api_ms=900,
        is_error=False,
        num_turns=3,
        session_id="s1",
        stop_reason="end_turn",
        total_cost_usd=0.0123,
        usage={
            "input_tokens": 100,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 300,
            "output_tokens": 40,
        },
        result="The data are ready.",
        model_usage={"claude-haiku-5-5": {"inputTokens": 100}},
    )
    base.update(over)
    return ResultMessage(**base)


class FakeClient:
    """`ClaudeSDKClient` with a script: `script(client)` is an async generator of messages. It
    may call `client.options.can_use_tool`, as the real CLI does through the control channel."""

    instances: list[FakeClient] = []
    script = None  # set per test
    # what `get_mcp_status()` returns on its 1st, 2nd, ... call (the last one repeats)
    mcp_statuses: list[list[dict]] | None = None

    def __init__(self, options) -> None:
        self.options = options
        self.status_calls = 0
        self.prompt: str | None = None
        self.cwd_existed_at_connect = False
        self.exited = False
        self.cancelled = False
        FakeClient.instances.append(self)

    async def __aenter__(self):
        self.cwd_existed_at_connect = Path(self.options.cwd).is_dir()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.exited = True
        self.cancelled = exc_type is asyncio.CancelledError
        return False

    def offered(self) -> set[str]:
        """The tools the options allow, as the server names them (no prefix)."""
        return {n.removeprefix(TOOL_PREFIX) for n in self.options.allowed_tools}

    async def get_mcp_status(self) -> dict:
        self.status_calls += 1
        sequence = type(self).mcp_statuses or [[mcp_server("connected", self.offered())]]
        return {"mcpServers": sequence[min(self.status_calls, len(sequence)) - 1]}

    async def query(self, prompt: str) -> None:
        self.prompt = prompt

    async def receive_response(self):
        async for msg in type(self).script(self):
            if isinstance(msg, SystemMessage) and msg.data.get("tools", 1) is None:
                msg.data["tools"] = sorted(f"{TOOL_PREFIX}{t}" for t in self.offered())
            yield msg


@pytest.fixture
def fake_sdk(monkeypatch):
    monkeypatch.setattr(claude_sdk, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(claude_sdk, "McpHttpServer", FakeServer)
    monkeypatch.setattr(claude_sdk, "resolve_cli", lambda conf: "/usr/bin/claude")
    monkeypatch.setattr(claude_sdk, "MCP_READY_POLL_S", 0.001)
    FakeClient.mcp_statuses = None
    FakeClient.instances.clear()
    FakeServer.instances.clear()
    FakeServer.recorded = []
    return FakeClient


def script_of(*messages):
    async def script(client):
        for m in messages:
            yield m

    return script


def sdk_conf(name: str = "haiku-sdk", **over: Any) -> ModelConf:
    fields: dict[str, Any] = {
        "name": name,
        "backend": "claude_sdk",
        "api_id": "claude-haiku-5-5",
        "space": f"k2val-{name}",
        "effort": "low",
    }
    return ModelConf(**{**fields, **over})


def sdk_cfg(cfg, *names: str, **over: Any):
    """The panel config with claude_sdk config(s) added (the shared fixture has none)."""
    confs = [sdk_conf(n, **over) for n in names or ("haiku-sdk",)]
    return replace(cfg, models={**cfg.models, **{c.name: c for c in confs}}), confs[0]


def raw_with_sdk() -> dict:
    # the shared raw config wants its secret files; only the models matter in these tests
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    files = {n: tmp / n for n in ("local", "owner", "anthropic", "lk")}
    for f in files.values():
        f.write_text("x" * 12)
    raw = raw_config(files)
    raw["models"]["haiku-sdk"] = {
        "backend": "claude_sdk",
        "api_id": "claude-haiku-5-5",
        "space": "k2val-haiku-sdk",
        "effort": "low",
    }
    return raw


def isolate_onedata_env(monkeypatch) -> None:
    """`run.main` writes the ONEDATA_* variables into the process: restore them afterwards."""
    for var in (
        "ONEDATA_MCP_DOTENV",
        "ONEDATA_ONEPROVIDER_HOST",
        "ONEDATA_ONEPROVIDER_TOKEN",
        "ONEDATA_ONEZONE_HOST",
        "ONEDATA_ONEZONE_TOKEN",
        "ONEDATA_ALLOW_INSECURE_TLS",
    ):
        monkeypatch.setenv(var, "restored-by-monkeypatch")


async def dispatch(cfg_, conf, mcp_app=None, tools=TOOLS):
    adapter = ClaudeSdkAdapter(cfg_, conf, Path("."))
    app = mcp_app or RecordingApp(object(), allowed=tools)
    out = await adapter.dispatch(
        system_prompt="SYSTEM", user_prompt="BRIEF", mcp_app=app, allowed_tools=tools
    )
    return out, app


# --- the options of the real client ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_trial_is_a_clean_room_over_the_loopback_http_server(cfg, fake_sdk) -> None:
    cfg, conf = sdk_cfg(cfg, effort="medium", thinking="adaptive")
    FakeClient.script = script_of(sdk_init(), assistant(TextBlock("ok")), result())

    await dispatch(cfg, conf)

    client = FakeClient.instances[0]
    o = client.options
    assert o.mcp_servers == {"onedata": {"type": "http", "url": SERVER_URL}}
    assert o.tools == []  # no built-in tool is offered at all
    assert {
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "Task",
    } <= set(o.disallowed_tools)
    assert o.allowed_tools == sorted(f"{TOOL_PREFIX}{t}" for t in TOOLS)
    assert o.system_prompt == "SYSTEM"  # the scenario's prompt, not Claude Code's
    assert client.prompt == "BRIEF"
    assert o.setting_sources == []  # NOT None: None loads user and project settings
    assert o.extra_args == {"strict-mcp-config": None, "disable-slash-commands": None}
    assert o.max_turns == cfg.limits.max_tool_rounds
    assert (o.model, o.effort) == ("claude-haiku-5-5", "medium")
    assert o.env["CLAUDE_CODE_EFFORT_LEVEL"] == "medium"  # an exported one must not override
    assert o.thinking == {"type": "adaptive"}
    assert o.permission_mode == "default" and o.can_use_tool is not None
    assert o.cli_path == "/usr/bin/claude"
    # it runs in an empty directory that is gone when the trial ends
    assert client.cwd_existed_at_connect
    assert not Path(o.cwd).exists() and not FakeServer.instances[0].scratch.exists()
    assert FakeServer.instances[0].tools == TOOLS


def test_no_api_key_or_onedata_token_reaches_the_cli(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SECRET")
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", "tok-SECRET")
    env = clean_env(300, "high")
    assert env["ANTHROPIC_API_KEY"] == "" and env["ANTHROPIC_AUTH_TOKEN"] == ""
    assert env["CLAUDE_CODE_USE_BEDROCK"] == ""
    assert env["ONEDATA_ONEPROVIDER_TOKEN"] == ""
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env["MCP_TOOL_TIMEOUT"] == "300000"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == "high"  # the CLI lets it override --effort
    assert "SECRET" not in repr(env)


def test_options_without_an_mcp_server_and_the_thinking_forms(cfg) -> None:
    _, conf = sdk_cfg(cfg)
    o = build_options(
        conf,
        system_prompt="s",
        allowed_tools=frozenset(),
        max_turns=1,
        tool_call_cap_s=1,
        cwd=Path("/tmp"),
        mcp_url=None,
    )
    assert o.mcp_servers == {} and o.allowed_tools == [] and o.thinking is None
    assert thinking_option("") is None
    assert thinking_option("adaptive") == {"type": "adaptive"}
    assert thinking_option("disabled") == {"type": "disabled"}
    assert thinking_option("8000") == {"type": "enabled", "budget_tokens": 8000}


# --- the trace -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_calls_come_from_the_servers_record_and_the_result_from_the_sdk(
    cfg, fake_sdk
) -> None:
    cfg, conf = sdk_cfg(cfg)
    FakeServer.recorded = [
        CallRecord(0, "check_readiness", {"extent": []}, 10.0, 11.0, True, '{"ready": false}'),
        CallRecord(1, "ensure_readiness", {}, 12.0, 13.0, False, None, "invalid arguments"),
    ]
    FakeClient.script = script_of(
        sdk_init(),
        assistant(tool_use("t1", f"{TOOL_PREFIX}check_readiness", extent=[]), mid="m1"),
        tool_result("t1", '{"ready": false}'),
        assistant(tool_use("t2", f"{TOOL_PREFIX}ensure_readiness"), mid="m2"),
        tool_result("t2", "invalid arguments", is_error=True),
        assistant(TextBlock("The data are ready."), mid="m3"),
        result(),
    )

    out, app = await dispatch(cfg, conf)

    assert out.error is None and out.finish_reason == "end_turn"
    assert out.final_answer == "The data are ready."
    assert [(c.tool_name, c.succeeded) for c in out.tool_calls] == [
        ("check_readiness", True),
        ("ensure_readiness", False),
    ]
    assert out.tool_calls[0].arguments == {"extent": []}
    assert out.tool_calls[0].result == '{"ready": false}'
    assert out.rounds_used == 3
    # prompt tokens as the anthropic adapter counts them: input + cache creation + cache read
    assert (out.usage_in_tokens, out.usage_out_tokens) == (420, 40)
    # the runner and the oracles read the server's calls from the recording app
    assert [r.tool for r in app.calls] == ["check_readiness", "ensure_readiness"]
    assert [r.source for r in app.calls] == ["mcp", "mcp"]
    t = out.raw_trace
    assert t["billing"] == "subscription" and t["sdk_total_cost_usd"] == 0.0123
    assert t["response_model"] == "claude-haiku-5-5"
    assert t["request_ids"] == ["m1", "m2", "m3"]
    assert (t["mcp_calls_from_sdk"], t["mcp_calls_from_server"]) == (2, 2)
    assert t["count_mismatch"] is None and t["denied_calls"] == []
    assert t["init"]["api_key_source"] == "none"
    assert t["unexpected_tools_offered"] == []


@pytest.mark.asyncio
async def test_a_count_mismatch_between_the_stream_and_the_server_is_noted(cfg, fake_sdk) -> None:
    cfg, conf = sdk_cfg(cfg)
    FakeClient.script = script_of(
        sdk_init(),
        assistant(tool_use("t1", f"{TOOL_PREFIX}check_readiness")),
        result(),
    )
    out, _ = await dispatch(cfg, conf)  # the server recorded nothing
    assert "SDK stream showed 1 MCP calls, the server recorded 0" in out.raw_trace["count_mismatch"]


@pytest.mark.asyncio
async def test_a_builtin_the_model_reaches_for_is_recorded_as_an_attempt(cfg, fake_sdk) -> None:
    cfg, conf = sdk_cfg(cfg)
    FakeClient.script = script_of(
        sdk_init(),
        assistant(tool_use("b1", "Bash", command="curl https://example.org")),
        tool_result("b1", "denied by the benchmark", is_error=True),
        assistant(TextBlock("I could not.")),
        result(result="I could not."),
    )

    out, app = await dispatch(cfg, conf)

    assert [(r.tool, r.source, r.ok) for r in app.calls] == [("Bash", "builtin", False)]
    assert app.calls[0].arguments == {"command": "curl https://example.org"}
    assert app.calls[0].error == "denied by the benchmark"
    assert out.raw_trace["builtin_calls"] == 1


# --- the permission gate --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_allows_only_the_scenarios_mcp_tools() -> None:
    denied: list[dict] = []
    gate = make_can_use_tool(TOOLS, denied)

    ok = await gate(f"{TOOL_PREFIX}check_readiness", {"extent": []}, CTX)
    assert isinstance(ok, PermissionResultAllow) and ok.updated_input == {"extent": []}

    for name in ("Bash", "Read", "WebFetch", "ToolSearch"):
        res = await gate(name, {"x": 1}, CTX)
        assert isinstance(res, PermissionResultDeny)
    # an MCP tool of the server that this scenario does not offer, and one of another server
    for name in (f"{TOOL_PREFIX}schedule_file_replication", "mcp__claude_ai_Gmail__send_message"):
        assert isinstance(await gate(name, {}, CTX), PermissionResultDeny)
    assert [d["tool_name"] for d in denied] == [
        "Bash",
        "Read",
        "WebFetch",
        "ToolSearch",
        f"{TOOL_PREFIX}schedule_file_replication",
        "mcp__claude_ai_Gmail__send_message",
    ]


@pytest.mark.asyncio
async def test_the_real_gate_is_the_one_in_the_options_and_denials_are_traced(
    cfg, fake_sdk
) -> None:
    cfg, conf = sdk_cfg(cfg)

    async def script(client):
        yield sdk_init()
        gate = client.options.can_use_tool
        assert isinstance(await gate("Bash", {"command": "ls"}, CTX), PermissionResultDeny)
        assert isinstance(
            await gate(f"{TOOL_PREFIX}get_file_distribution", {}, CTX), PermissionResultAllow
        )
        yield result()

    FakeClient.script = script
    out, _ = await dispatch(cfg, conf)
    assert out.raw_trace["denied_calls"] == [{"tool_name": "Bash", "input": {"command": "ls"}}]


def test_every_builtin_is_in_the_disallowed_list() -> None:
    assert {
        "Bash",
        "Read",
        "Write",
        "Edit",
        "Glob",
        "Grep",
        "WebSearch",
        "WebFetch",
        "Task",
    } <= set(BUILTIN_TOOLS)


# --- caps, errors, invalid trials ------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_turns_is_a_round_cap(cfg, fake_sdk) -> None:
    cfg, conf = sdk_cfg(cfg)
    FakeClient.script = script_of(
        sdk_init(),
        result(subtype="error_max_turns", is_error=False, result=None, num_turns=40),
    )
    out, _ = await dispatch(cfg, conf)
    assert out.finish_reason == "max_rounds" and out.final_answer == "" and out.error is None
    assert out.rounds_used == 40


@pytest.mark.asyncio
async def test_a_failed_result_is_an_adapter_error_never_an_answer(cfg, fake_sdk) -> None:
    """A rate limit or a lost login arrives as a result whose text is the message: it must not
    be scored as the model's final answer."""
    cfg, conf = sdk_cfg(cfg)
    FakeClient.script = script_of(
        sdk_init(),
        assistant(TextBlock("You've hit your limit"), error="rate_limit"),
        RateLimitEvent(
            rate_limit_info=RateLimitInfo(
                status="rejected", resets_at=1, rate_limit_type="five_hour", utilization=1.0
            ),
            uuid="u",
            session_id="s",
        ),
        result(is_error=True, result="You've hit your limit"),
    )
    out, _ = await dispatch(cfg, conf)
    assert out.final_answer == "" and out.finish_reason == "adapter_error"
    assert "hit your limit" in out.error and "rate_limit" in out.error
    assert out.raw_trace["rate_limit_events"][0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_a_session_with_no_result_and_an_sdk_exception_are_adapter_errors(
    cfg, fake_sdk
) -> None:
    cfg, conf = sdk_cfg(cfg)
    FakeClient.script = script_of(sdk_init())
    out, _ = await dispatch(cfg, conf)
    assert out.finish_reason == "adapter_error" and "without a result" in out.error

    async def boom(client):
        yield sdk_init()
        raise RuntimeError("the CLI died")

    FakeClient.script = boom
    out, _ = await dispatch(cfg, conf)
    assert out.finish_reason == "adapter_error" and "RuntimeError: the CLI died" in out.error


@pytest.mark.asyncio
async def test_a_cli_on_an_api_key_stops_the_trial(cfg, fake_sdk) -> None:
    cfg, conf = sdk_cfg(cfg)
    reached = []

    async def script(client):
        yield sdk_init(apiKeySource="user")
        reached.append("after init")  # must not be read
        yield result()

    FakeClient.script = script
    out, _ = await dispatch(cfg, conf)
    assert "apiKeySource='user'" in out.error and "not the subscription" in out.error
    assert reached == [] and out.final_answer == ""
    assert FakeClient.instances[0].exited  # the client is closed, so the CLI is stopped


@pytest.mark.asyncio
async def test_an_mcp_server_the_cli_could_not_use_is_a_harness_fault(cfg, fake_sdk) -> None:
    """With no tools the model would fail the scenario: that must not be scored as its failure."""
    cfg, conf = sdk_cfg(cfg)
    FakeClient.script = script_of(
        sdk_init(mcp_servers=[{"name": "onedata", "status": "failed"}]), result()
    )
    out, _ = await dispatch(cfg, conf)
    assert out.finish_reason == "adapter_error" and "'onedata' MCP server" in out.error


@pytest.mark.asyncio
async def test_an_unexpected_tool_in_the_session_is_reported(cfg, fake_sdk) -> None:
    cfg, conf = sdk_cfg(cfg)
    FakeClient.script = script_of(
        sdk_init(tools=[*(f"{TOOL_PREFIX}{t}" for t in sorted(TOOLS)), "Skill"]), result()
    )
    out, _ = await dispatch(cfg, conf)
    assert out.raw_trace["unexpected_tools_offered"] == ["Skill"]


# --- the MCP server must be there before the model starts ------------------------------------


@pytest.mark.asyncio
async def test_the_query_waits_until_the_server_is_connected_with_every_tool(cfg, fake_sdk) -> None:
    """A real CLI showed the server `pending` for about a second after it started."""
    cfg, conf = sdk_cfg(cfg)
    FakeClient.mcp_statuses = [
        [mcp_server("pending")],
        [mcp_server("pending")],
        [mcp_server("connected", tools={"check_readiness"})],  # not all of them yet
        [mcp_server("connected", tools=TOOLS)],
    ]
    FakeClient.script = script_of(sdk_init(), assistant(TextBlock("x")), result())

    out, _ = await dispatch(cfg, conf)

    client = FakeClient.instances[0]
    assert client.status_calls == 4 and client.prompt == "BRIEF"
    assert out.error is None
    assert out.raw_trace["mcp_server"]["status"] == "connected"
    assert out.raw_trace["extra_mcp_tools_exposed"] == []  # an extra tool is fatal (below)


@pytest.mark.asyncio
async def test_a_dead_server_means_no_query_and_an_adapter_error(cfg, fake_sdk) -> None:
    cfg, conf = sdk_cfg(cfg)
    FakeClient.mcp_statuses = [[mcp_server("failed", error="connection refused")]]
    FakeClient.script = script_of(sdk_init(), result())

    out, _ = await dispatch(cfg, conf)

    assert FakeClient.instances[0].prompt is None  # the model was never started
    assert out.finish_reason == "adapter_error" and out.final_answer == ""
    assert "status 'failed'" in out.error and "connection refused" in out.error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, needle",
    [
        ([mcp_server("pending")], "status 'pending'"),
        ([mcp_server("connected", tools={"check_readiness"})], "tools missing"),
        ([], "was not listed"),
    ],
)
async def test_a_server_that_never_gets_ready_is_an_adapter_error(
    cfg, fake_sdk, monkeypatch, status, needle
) -> None:
    cfg, conf = sdk_cfg(cfg)
    monkeypatch.setattr(claude_sdk, "MCP_READY_TIMEOUT_S", 0.05)
    FakeClient.mcp_statuses = [status]
    FakeClient.script = script_of(sdk_init(), result())

    out, _ = await dispatch(cfg, conf)

    assert FakeClient.instances[0].prompt is None
    assert out.finish_reason == "adapter_error" and "not ready after" in out.error
    assert needle in out.error


@pytest.mark.asyncio
async def test_a_server_exposing_a_tool_beyond_the_scenarios_is_a_harness_fault(
    cfg, fake_sdk
) -> None:
    """The scenario's surface exactly: an extra tool (one that could answer the task without
    the tool under promotion) stops the trial before the model starts."""
    cfg, conf = sdk_cfg(cfg)

    class Extra(FakeClient):
        async def get_mcp_status(self) -> dict:
            self.status_calls += 1
            return {"mcpServers": [mcp_server("connected", {*self.offered(), "list_children"})]}

    claude_sdk.ClaudeSDKClient = Extra  # undone by the fixture's monkeypatch
    FakeClient.script = script_of(sdk_init(), result())

    out, _ = await dispatch(cfg, conf)

    assert out.finish_reason == "adapter_error"
    assert "exposes ['list_children'] beyond the scenario's tools" in out.error


def test_the_http_server_refuses_a_surface_that_is_not_the_scenarios() -> None:
    from benchmark.k2val.mcp_http import surface_mismatch

    assert surface_mismatch({"a", "b"}, {"a", "b"}) is None
    assert "also expose ['c']" in surface_mismatch({"a"}, {"a", "c"})
    assert "does not expose ['b']" in surface_mismatch({"a", "b"}, {"a"})


@pytest.mark.asyncio
async def test_a_session_that_does_not_offer_the_scenarios_tools_is_not_scored(
    cfg, fake_sdk
) -> None:
    """The server connected, but the CLI hands the model other tools: a harness fault."""
    cfg, conf = sdk_cfg(cfg)
    FakeClient.script = script_of(sdk_init(tools=[f"{TOOL_PREFIX}check_readiness"]), result())

    out, _ = await dispatch(cfg, conf)

    assert out.finish_reason == "adapter_error" and out.final_answer == ""
    assert "does not offer" in out.error and "ensure_readiness" in out.error


# --- through the runner ---------------------------------------------------------------------


def make_deps(cfg, api, clock, tmp_path, spend=None) -> RunDeps:
    from test.unit.k2val.test_runner import AgreeingJudge, FakeToolApp

    def factory(conf, on_cost):
        return ClaudeSdkAdapter(cfg, conf, tmp_path)

    return RunDeps(
        cfg=cfg,
        run_id=RUN_ID,
        run_dir=tmp_path / "run",
        fed=api,
        app=FakeToolApp(),
        adapter_factory=factory,
        spend=spend,
        clock=clock,
        settle_poll_s=15.0,
        judge=AgreeingJudge(),
        surfaces=None,
    )


@pytest.mark.asyncio
async def test_the_wall_cap_cancels_the_trial_into_no_result_and_keeps_the_calls(
    cfg, api, fed, clock, tmp_path, fake_sdk
) -> None:
    cfg, conf = sdk_cfg(cfg)
    cfg = replace(cfg, limits=replace(cfg.limits, wall_cap_s=0.3))
    fed.add_space(conf.space)
    FakeServer.recorded = [
        CallRecord(0, "check_readiness", {"extent": []}, 10.0, 11.0, True, '{"ready": false}')
    ]

    async def script(client):
        yield sdk_init()
        await asyncio.sleep(30)  # the model is still working when the cap is reached
        yield result()

    FakeClient.script = script

    art = await run_trial(make_deps(cfg, api, clock, tmp_path), conf, SCENARIOS["V4"], 0)

    assert art.outcome == "NO_RESULT" and "wall cap" in art.no_result_reason
    assert [c["tool"] for c in art.calls] == ["check_readiness"]
    client = FakeClient.instances[0]
    assert client.exited and client.cancelled  # the SDK client was closed: the CLI is stopped
    assert FakeServer.instances[0].exited
    assert not FakeServer.instances[0].scratch.exists()
    assert fed.find(art.directory) is None  # the fixture is still torn down


@pytest.mark.asyncio
async def test_the_spend_tracker_never_sees_the_subscription(
    cfg, api, fed, clock, tmp_path, fake_sdk
) -> None:
    cfg, conf = sdk_cfg(cfg)
    fed.add_space(conf.space)
    # a cap already spent: an `anthropic` trial would be refused, a `claude_sdk` one is not
    spend = SpendTracker(cap_usd=0.0, reserve_usd=0.5, run_dir=tmp_path / "run")
    FakeClient.script = script_of(sdk_init(), assistant(TextBlock("x")), result())

    art = await run_trial(make_deps(cfg, api, clock, tmp_path, spend), conf, SCENARIOS["V4"], 0)

    assert art.outcome not in ("BUDGET_REFUSED", "BUDGET_STOP")
    assert art.cost_usd == 0.0
    assert (art.usage_in_tokens, art.usage_out_tokens) == (420, 40)
    assert art.raw_trace["billing"] == "subscription"
    assert (spend.calls, spend.in_flight, spend.spent_usd) == (0, 0, 0.0)
    assert not (tmp_path / "run" / "spend.jsonl").exists()


@pytest.mark.asyncio
async def test_a_subscription_config_shows_as_such_in_the_report(
    cfg, api, fed, clock, tmp_path, fake_sdk
) -> None:
    cfg, _ = sdk_cfg(cfg, "haiku-low", "haiku-high")
    FakeClient.script = script_of(sdk_init(), assistant(TextBlock("x")), result())
    deps = make_deps(cfg, api, clock, tmp_path)
    for name in ("haiku-low", "haiku-high"):
        fed.add_space(cfg.models[name].space)
        await run_trial(deps, cfg.models[name], SCENARIOS["V4"], 0)

    rows = read_trials(deps.run_dir)
    assert {r["cost_usd"] for r in rows} == {0.0}
    assert {r["raw_trace"]["billing"] for r in rows} == {"subscription"}
    report = build_report(deps.run_dir, cfg, run_id=RUN_ID)
    effort_table = report.split("## Haiku effort comparison")[1]
    assert effort_table.count("| subscription |") == 2
    assert "0.0000" not in effort_table
    assert _cost_cell([{"cost_usd": 0.5, "raw_trace": {}}]) == "0.5000"


# --- config ---------------------------------------------------------------------------------


def models_of(raw: dict, **over: Any) -> dict:
    raw = {**raw, "models": {**raw["models"]}}
    raw["models"]["haiku-sdk"] = {**raw["models"]["haiku-sdk"], **over}
    return raw


def test_a_claude_sdk_config_needs_no_key_and_no_price() -> None:
    conf = parse_config(raw_with_sdk()).models["haiku-sdk"]
    assert conf.backend == "claude_sdk" and conf.is_claude_sdk
    assert not conf.is_anthropic and not conf.is_cli
    assert (conf.key_file, conf.price, conf.effort, conf.thinking) == ("", None, "low", "")


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_every_effort_the_cli_takes_is_accepted(effort: str) -> None:
    conf = parse_config(models_of(raw_with_sdk(), effort=effort)).models["haiku-sdk"]
    assert conf.effort == effort


@pytest.mark.parametrize(
    "over, needle",
    [
        ({"effort": ""}, "models.haiku-sdk.effort is required for claude_sdk"),
        ({"effort": "huge"}, "models.haiku-sdk.effort is required for claude_sdk"),
        ({"key_file": "/some/key"}, "runs on the subscription: remove key_file"),
        (
            {"price": {"input_low": 1.0, "output_low": 2.0}},
            "runs on the subscription: remove price",
        ),
        ({"thinking": "sometimes"}, "models.haiku-sdk.thinking must be one of"),
        ({"thinking": 0}, "token budget must be positive"),
        ({"thinking": True}, "must be a string or an integer"),
    ],
)
def test_claude_sdk_config_errors_name_the_key(over: dict, needle: str) -> None:
    raw = models_of(raw_with_sdk(), **over)
    if over.get("effort") == "":
        raw["models"]["haiku-sdk"].pop("effort")
    with pytest.raises(ConfigError, match=needle):
        parse_config(raw)


def test_thinking_forms_and_it_is_for_claude_sdk_only() -> None:
    for given, want in (("adaptive", "adaptive"), ("disabled", "disabled"), (8000, "8000")):
        conf = parse_config(models_of(raw_with_sdk(), thinking=given)).models["haiku-sdk"]
        assert conf.thinking == want
    raw = raw_with_sdk()
    raw["models"]["glm"]["thinking"] = "adaptive"
    with pytest.raises(ConfigError, match="models.glm.thinking is only for the claude_sdk"):
        parse_config(raw)


def test_the_example_config_runs_the_claude_configs_on_the_subscription() -> None:
    from benchmark.k2val.config import load_config

    example = Path(__file__).resolve().parents[3] / "benchmark/k2val/k2val.toml.example"
    cfg = load_config(example)
    for name, effort in (
        ("sonnet-medium", "medium"),
        ("haiku-low", "low"),
        ("haiku-medium", "medium"),
        ("haiku-high", "high"),
    ):
        conf = cfg.models[name]
        assert conf.backend == "claude_sdk" and conf.effort == effort
        assert (conf.key_file, conf.price) == ("", None)
    assert (
        cfg.models["sonnet-medium"].api_id == "claude-sonnet-5-5"
        and cfg.models["sonnet-medium"].control
    )
    assert all(
        cfg.models[n].api_id == "claude-haiku-5-5"
        for n in ("haiku-low", "haiku-medium", "haiku-high")
    )
    assert not any(m.is_anthropic for m in cfg.models.values())
    assert "ANTHROPIC" not in example.read_text().replace("ANTHROPIC_API_KEY", "")


def test_the_run_command_builds_a_claude_sdk_adapter_without_reading_a_key(
    tmp_path, monkeypatch
) -> None:
    from benchmark.k2val import run as run_module
    from benchmark.k2val import runner

    token = tmp_path / "t"
    token.write_text("tok-local-SECRET-7f3a91")
    toml = tmp_path / "k2val.toml"
    toml.write_text(
        f"""
[federation.onezone]
host = "https://zone.k2val.datahub.test"
[federation.local]
name = "vistula-dc"
host = "https://local.k2val.datahub.test"
token_file = "{token}"
[federation.owner]
name = "nordic-marine-dc"
host = "https://owner.k2val.datahub.test"
token_file = "{token}"
[run]
artefact_root = "{tmp_path / "artefacts"}"
research_dir = "{tmp_path / "research"}"
[models.haiku-low]
backend = "claude_sdk"
api_id = "claude-haiku-5-5"
space = "k2val-haiku-low"
effort = "low"
"""
    )
    isolate_onedata_env(monkeypatch)
    seen: dict = {}

    async def fake_sweep(deps, models, scenarios, k, *, on_trial=None):
        seen["adapter"] = deps.adapter_factory(models[0], lambda c: None)
        seen["spend"] = deps.spend
        return []

    monkeypatch.setattr(runner, "run_sweep", fake_sweep)

    code = run_module.main(
        ["--config", str(toml), "--scenarios", "V4", "--k", "1", "--run-id", "r1"]
    )

    assert code == 0
    assert isinstance(seen["adapter"], ClaudeSdkAdapter)
    assert seen["spend"] is None  # no `anthropic` config in the run: no tracker
    assert not (tmp_path / "artefacts" / "r1" / "spend.jsonl").exists()


def test_a_missing_claude_binary_stops_the_run_before_a_fixture_is_built(
    tmp_path, monkeypatch, capsys
) -> None:
    from benchmark.k2val import run as run_module

    token = tmp_path / "t"
    token.write_text("tok-local-SECRET-7f3a91")
    toml = tmp_path / "k2val.toml"
    toml.write_text(
        f"""
[federation.onezone]
host = "https://zone.k2val.datahub.test"
[federation.local]
name = "vistula-dc"
host = "https://local.k2val.datahub.test"
token_file = "{token}"
[federation.owner]
name = "nordic-marine-dc"
host = "https://owner.k2val.datahub.test"
token_file = "{token}"
[models.haiku-low]
backend = "claude_sdk"
api_id = "claude-haiku-5-5"
space = "k2val-haiku-low"
effort = "low"
binary = "no-such-claude-binary"
"""
    )
    isolate_onedata_env(monkeypatch)
    code = run_module.main(["--config", str(toml), "--scenarios", "V4", "--k", "1"])
    assert code == 2
    assert "no-such-claude-binary" in capsys.readouterr().err


# --- the real SDK client, with a stand-in for the `claude` executable -----------------------


STAND_IN = """#!{python}
import os, sys, time
if "--version" in sys.argv:
    print("2.1.329 (Claude Code)")
    sys.exit(0)
with open({pidfile!r}, "w") as f:
    f.write(str(os.getpid()))
time.sleep(600)  # a CLI that never answers
"""


@pytest.mark.asyncio
async def test_a_cancelled_trial_leaves_no_claude_process_behind(
    cfg, tmp_path, monkeypatch
) -> None:
    """The real `ClaudeSDKClient`, started on a stand-in executable that never answers, is
    cancelled the way the wall cap cancels it. The process must be gone: a leaked CLI per
    timed-out trial would go on using the subscription."""
    pidfile = tmp_path / "pid"
    stand_in = tmp_path / "claude"
    stand_in.write_text(STAND_IN.format(python=sys.executable, pidfile=str(pidfile)))
    stand_in.chmod(stand_in.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(claude_sdk, "McpHttpServer", FakeServer)
    monkeypatch.setattr(claude_sdk, "resolve_cli", lambda conf: str(stand_in))
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    FakeServer.instances.clear()
    FakeServer.recorded = []
    cfg, conf = sdk_cfg(cfg)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(dispatch(cfg, conf), timeout=3)

    assert pidfile.exists(), "the stand-in was never started"
    pid = int(pidfile.read_text())
    for _ in range(50):  # the CLI is stopped by the client's exit, a moment later
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        os.kill(pid, 9)
        pytest.fail("the claude process outlived its cancelled trial")
