"""The CLI legs (wave 2): the jail, the gating, the parsers, and an adapter run with fake CLI
binaries. Never the real CLIs: the fakes emit events of the recorded shape."""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from benchmark.k2val import cli_agents
from benchmark.k2val.cli_agents import (
    CliAdapter,
    agy_argv,
    agy_settings,
    codex_argv,
    leg_enabled,
    parse_agy_events,
    parse_codex_events,
)
from benchmark.k2val.config import JailConf
from benchmark.k2val.jail import JailError, jail_available, jail_cmd
from benchmark.k2val.recording import CallRecord, RecordingApp
from benchmark.k2val.scan import build_record, fingerprint, scan_texts, write_scan_record
from test.unit.k2val.conftest import TOKEN_LOCAL, TOKEN_OWNER

KNOWN = frozenset({"check_readiness", "ensure_readiness", "get_file_distribution"})

CODEX_RUN = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "think"}},
    {
        "type": "item.started",
        "item": {
            "id": "i1",
            "type": "mcp_tool_call",
            "server": "onedata",
            "tool": "check_readiness",
            "arguments": {"extent": [{"dataset": "/k2val-gpt/logs-x"}]},
            "result": None,
            "error": None,
            "status": "in_progress",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i1",
            "type": "mcp_tool_call",
            "server": "onedata",
            "tool": "check_readiness",
            "arguments": {"extent": [{"dataset": "/k2val-gpt/logs-x"}]},
            "result": {
                "content": [{"type": "text", "text": '{"ready": false}'}],
                "structured_content": {"ready": False},
            },
            "error": None,
            "status": "completed",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i2",
            "type": "mcp_tool_call",
            "server": "onedata",
            "tool": "ensure_readiness",
            "arguments": {},
            "result": None,
            "error": {"message": "invalid arguments"},
            "status": "failed",
        },
    },
    {
        "type": "item.completed",
        "item": {
            "id": "i3",
            "type": "command_execution",
            "command": "ls -la",
            "aggregated_output": "total 0",
            "exit_code": 0,
            "status": "completed",
        },
    },
    {"type": "item.completed", "item": {"id": "i4", "type": "agent_message", "text": "Not ready."}},
    {
        "type": "turn.completed",
        "usage": {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 90},
    },
]

AGY_RUN = [
    {"type": "init", "session_id": "s", "model": "gemini-3.8-flash-low"},
    {"type": "message", "role": "user", "content": "the brief"},
    {
        "type": "tool_use",
        "tool_name": "mcp_onedata_check_readiness",
        "tool_id": "c1",
        "parameters": {"extent": []},
    },
    {"type": "tool_result", "tool_id": "c1", "status": "success", "output": '{"ready": false}'},
    {
        "type": "tool_use",
        "tool_name": "run_shell_command",
        "tool_id": "c2",
        "parameters": {"command": "ls"},
    },
    {"type": "tool_result", "tool_id": "c2", "status": "error", "output": "denied"},
    {"type": "message", "role": "assistant", "content": "Not ", "delta": True},
    {"type": "message", "role": "assistant", "content": "ready.", "delta": True},
    {"type": "result", "status": "success", "stats": {"input_tokens": 500, "output_tokens": 40}},
]


def jsonl(events) -> list[str]:
    return [json.dumps(e) for e in events]


# --- the jail ----------------------------------------------------------------------------


def test_with_no_wrapper_there_is_no_jail_and_nothing_runs() -> None:
    assert not jail_available(JailConf())
    with pytest.raises(JailError, match="never run unjailed"):
        jail_cmd(["codex", "exec"], "/tmp/work", JailConf())
    with pytest.raises(JailError):
        jail_cmd(["codex"], "/tmp/work", JailConf(wrapper="   "))


def test_jail_cmd_puts_the_wrapper_in_front() -> None:
    conf = JailConf(wrapper="/path/to/jail --net loopback", workdir_flag="--cwd")
    assert jail_cmd(["codex", "exec", "x"], "/tmp/w", conf) == [
        "/path/to/jail",
        "--net",
        "loopback",
        "--cwd",
        "/tmp/w",
        "--",
        "codex",
        "exec",
        "x",
    ]


# --- the gating ------------------------------------------------------------------------------


def test_cli_legs_are_off_until_a_wrapper_is_set_and_a_clean_scan_exists(cfg, tmp_path) -> None:
    conf = cfg.models["gpt-luna-low"]

    ok, why = leg_enabled(cfg, conf, tmp_path)
    assert not ok and "[cli.jail] wrapper is not set" in why

    jailed = replace(cfg, jail=JailConf(wrapper="/path/to/jail"))
    ok, why = leg_enabled(jailed, conf, tmp_path)
    assert not ok and "no scan record" in why

    def record(matches: int, fp: str | None = None) -> None:
        rec = build_record(jailed, conf, scan_texts({"prompts": "x" * matches}, ["x"], []), 1, 0)
        if fp:
            rec["fingerprint"] = fp
        write_scan_record(tmp_path, None, rec)

    record(2)
    ok, why = leg_enabled(jailed, conf, tmp_path)
    assert not ok and "not clean (2 matches)" in why

    record(0, fp="stale")
    ok, why = leg_enabled(jailed, conf, tmp_path)
    assert not ok and "stale" in why

    # the fingerprint of a scan made with one token list is not valid for another
    record(0)
    assert leg_enabled(jailed, conf, tmp_path) == (True, "enabled")
    assert fingerprint(jailed, conf) != fingerprint(jailed, cfg.models["glm"])


# --- command lines ---------------------------------------------------------------------------


def test_codex_command_line(cfg) -> None:
    argv = codex_argv(cfg.models["gpt-luna-low"], "http://127.0.0.1:41999/mcp", "the prompt")
    assert argv[:7] == [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "-s",
        "read-only",
        "--skip-git-repo-check",
    ]
    assert argv[7:9] == ["-m", "gpt-5.6-luna"]
    assert 'model_reasoning_effort="low"' in argv
    assert 'mcp_servers.onedata.url="http://127.0.0.1:41999/mcp"' in argv  # a URL, nothing more
    assert argv[-1] == "the prompt"
    flat = " ".join(argv)
    assert TOKEN_LOCAL not in flat and "token" not in flat.lower() and "ONEDATA" not in flat


def test_agy_settings_declare_only_the_loopback_url(cfg) -> None:
    conf = replace(cfg.models["gpt-luna-low"], backend="agy", binary="agy", reasoning_effort="")
    conf = replace(conf, api_id="gemini-3.8-flash-low")
    argv = agy_argv(conf, "/w/agy-settings.json", "the prompt")
    assert argv[:5] == ["agy", "--print", "--output-format", "stream-json", "--sandbox"]
    assert argv[argv.index("--settings") : argv.index("--settings") + 2] == [
        "--settings",
        "/w/agy-settings.json",
    ]
    assert argv[argv.index("--model") : argv.index("--model") + 2] == [
        "--model",
        "gemini-3.8-flash-low",
    ]
    assert agy_settings("http://127.0.0.1:41999/mcp") == {
        "mcpServers": {"onedata": {"url": "http://127.0.0.1:41999/mcp"}}
    }
    add_mode = agy_argv(replace(conf, mcp_attach="mcp_add"), "/w/s.json", "p")
    assert "--settings" not in add_mode


# --- the parsers -----------------------------------------------------------------------------


def test_codex_events_separate_mcp_calls_from_builtin_tools() -> None:
    run = parse_codex_events(jsonl(CODEX_RUN) + ["codex: not json"])

    assert run.final_answer == "Not ready."
    mcp = [e for e in run.events if e.source == "mcp"]
    builtin = [e for e in run.events if e.source == "builtin"]
    assert [e.tool for e in mcp] == [
        "check_readiness",
        "ensure_readiness",
    ]  # started events skipped
    assert mcp[0].result == '{"ready": false}' and mcp[0].ok
    assert mcp[0].arguments == {"extent": [{"dataset": "/k2val-gpt/logs-x"}]}
    assert not mcp[1].ok and mcp[1].error == "invalid arguments"
    assert [(b.tool, b.arguments) for b in builtin] == [("shell", {"command": "ls -la"})]
    assert (run.tokens_in, run.tokens_out) == (1200, 90)
    assert run.unparsed == 1


def test_codex_failure_events() -> None:
    run = parse_codex_events(
        jsonl([{"type": "turn.failed", "error": {"message": "401 unauthorized"}}])
    )
    assert run.error == "401 unauthorized" and run.final_answer == ""


def test_agy_events_separate_mcp_calls_from_builtin_tools() -> None:
    run = parse_agy_events(jsonl(AGY_RUN), KNOWN)

    assert run.final_answer == "Not ready."
    assert [(e.source, e.tool, e.ok) for e in run.events] == [
        ("mcp", "mcp_onedata_check_readiness", True),
        ("builtin", "run_shell_command", False),
    ]
    assert run.events[0].result == '{"ready": false}'
    assert (run.tokens_in, run.tokens_out) == (500, 40)


# --- an adapter run with fake binaries -----------------------------------------------------


FAKE_CLI = """#!{python}
import json, os, sys
log = {log!r}
with open(log, "a") as f:
    row = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "ls": sorted(os.listdir("."))}}
    f.write(json.dumps(row) + "\\n")
args = sys.argv[1:]
if args[:1] == ["mcp"]:
    sys.exit(0)
if args[:1] == ["exec"]:
    for line in {codex!r}:
        print(line)
    open("last_message.txt", "w").write("Not ready: two files are still being written.")
else:
    for line in {agy!r}:
        print(line)
    if os.environ.get("FAKE_FAIL"):
        sys.exit(1)
"""

FAKE_JAIL = """#!/bin/sh
# a fake jail: <wrapper> --workdir DIR -- cmd...
echo "$@" >> {log}
shift; workdir="$1"; shift; shift
cd "$workdir" && exec "$@"
"""


def make_fakes(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    cli_log, jail_log = tmp_path / "cli.log", tmp_path / "jail.log"
    cli = tmp_path / "fake-cli"
    cli.write_text(
        FAKE_CLI.format(
            python=sys.executable, log=str(cli_log), codex=jsonl(CODEX_RUN), agy=jsonl(AGY_RUN)
        )
    )
    jail = tmp_path / "fake-jail"
    jail.write_text(FAKE_JAIL.format(log=jail_log))
    for p in (cli, jail):
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return cli, jail, cli_log, jail_log


class FakeServer:
    """Stands in for the loopback onedata-mcp process: a URL and the calls it recorded."""

    instances: list[FakeServer] = []

    def __init__(self, config_path, tools, scratch) -> None:
        self.tools = tools
        self.scratch = scratch
        self.url = "http://127.0.0.1:41999/mcp"
        FakeServer.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def records(self) -> list[CallRecord]:
        return [
            CallRecord(0, "check_readiness", {"extent": []}, 10.0, 11.0, True, '{"ready": false}'),
            CallRecord(1, "ensure_readiness", {}, 12.0, 13.0, False, None, "invalid arguments"),
        ]


@pytest.fixture
def fakes(tmp_path, monkeypatch, cfg):
    cli, jail, cli_log, jail_log = make_fakes(tmp_path)
    monkeypatch.setattr(cli_agents, "McpHttpServer", FakeServer)
    FakeServer.instances.clear()
    conf = replace(cfg.models["gpt-luna-low"], binary=str(cli))
    jailed = replace(cfg, jail=JailConf(wrapper=str(jail)), models={**cfg.models, conf.name: conf})
    return jailed, conf, cli_log, jail_log


def cli_calls(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.asyncio
async def test_a_codex_trial_runs_in_the_jail_with_an_empty_workdir_and_a_url_only(
    fakes, tmp_path
) -> None:
    cfg, conf, cli_log, jail_log = fakes
    adapter = CliAdapter(cfg, conf, tmp_path / "run")
    app = RecordingApp(object(), allowed=KNOWN)

    out = await adapter.dispatch(
        system_prompt="You are an assistant.",
        user_prompt="Are the files complete?",
        mcp_app=app,
        allowed_tools=KNOWN,
    )

    # through the jail wrapper, with the work dir; the CLI saw an empty work dir
    assert "--workdir" in jail_log.read_text() and " -- " in jail_log.read_text()
    (run,) = cli_calls(cli_log)
    assert run["ls"] == [] and run["cwd"].endswith("/work")
    flat = " ".join(run["argv"])
    assert 'mcp_servers.onedata.url="http://127.0.0.1:41999/mcp"' in flat
    assert TOKEN_LOCAL not in flat and TOKEN_OWNER not in flat
    assert "You are an assistant.\n\nAre the files complete?" in run["argv"][-1]
    # the MCP half of the trace is the server's record; the built-in tools are reported too
    assert [(c.tool, c.source) for c in app.calls] == [
        ("check_readiness", "mcp"),
        ("ensure_readiness", "mcp"),
        ("shell", "builtin"),
    ]
    assert [c.index for c in app.calls] == [0, 1, 2]
    assert app.calls[0].started_at == 10.0 and app.calls[1].ok is False
    assert out.final_answer == "Not ready: two files are still being written."  # the -o file
    assert (out.usage_in_tokens, out.usage_out_tokens) == (1200, 90)
    assert out.raw_trace["mcp_calls_from_server"] == 2 and out.raw_trace["builtin_calls"] == 1
    assert out.raw_trace["count_mismatch"] is None
    assert [t.tool_name for t in out.tool_calls] == ["check_readiness", "ensure_readiness", "shell"]
    assert FakeServer.instances[0].tools == KNOWN
    # the scratch dir (with the work dir) is gone
    assert not Path(FakeServer.instances[0].scratch).exists()


@pytest.mark.asyncio
async def test_no_wrapper_means_the_cli_is_never_started(fakes, tmp_path) -> None:
    cfg, conf, cli_log, jail_log = fakes
    unjailed = replace(cfg, jail=JailConf())

    with pytest.raises(JailError):
        await CliAdapter(unjailed, conf, tmp_path).dispatch(
            system_prompt="s",
            user_prompt="u",
            mcp_app=RecordingApp(object(), allowed=KNOWN),
            allowed_tools=KNOWN,
        )

    assert not cli_log.exists() and not jail_log.exists()
    assert FakeServer.instances == []  # not even the server was started


@pytest.mark.asyncio
async def test_an_agy_trial_uses_settings_and_leaves_the_global_config_alone(
    fakes, tmp_path
) -> None:
    cfg, codex_conf, cli_log, jail_log = fakes
    conf = replace(
        codex_conf,
        name="gemini-flash-low",
        backend="agy",
        reasoning_effort="low",
        api_id="gemini-3.8-flash-low",
    )
    app = RecordingApp(object(), allowed=KNOWN)

    out = await CliAdapter(cfg, conf, tmp_path).dispatch(
        system_prompt="s", user_prompt="u", mcp_app=app, allowed_tools=KNOWN
    )

    (run,) = cli_calls(cli_log)
    assert "--settings" in run["argv"] and "--sandbox" in run["argv"]
    assert not any(a == "mcp" for a in run["argv"])  # no `agy mcp add`: nothing registered globally
    assert out.final_answer == "Not ready."
    assert [c.source for c in app.calls] == ["mcp", "mcp", "builtin"]


@pytest.mark.asyncio
async def test_agy_mcp_add_uses_a_unique_name_and_removes_it_even_when_the_cli_fails(
    fakes, tmp_path, monkeypatch
) -> None:
    cfg, codex_conf, cli_log, jail_log = fakes
    conf = replace(
        codex_conf,
        name="gemini-flash-low",
        backend="agy",
        mcp_attach="mcp_add",
        api_id="gemini-3.8-flash-low",
    )
    app = RecordingApp(object(), allowed=KNOWN)
    # the run itself crashes
    monkeypatch.setattr(CliAdapter, "_run", _boom, raising=True)

    with pytest.raises(RuntimeError, match="cli crashed"):
        await CliAdapter(cfg, conf, tmp_path).dispatch(
            system_prompt="s", user_prompt="u", mcp_app=app, allowed_tools=KNOWN
        )

    calls = [c["argv"] for c in cli_calls(cli_log)]
    assert [c[:2] for c in calls] == [["mcp", "add"], ["mcp", "remove"]]
    assert calls[0][2] == calls[1][2] and calls[0][2].startswith("k2val-")  # the same unique name
    assert calls[0][3] == "http://127.0.0.1:41999/mcp"


async def _boom(self, argv, workdir):
    raise RuntimeError("cli crashed")


CRASHING_CLI = """#!{python}
import sys
print("some noise", flush=True)
print("authentication failed", file=sys.stderr, flush=True)
sys.exit(3)
"""

SILENT_CLI = """#!{python}
print('{{"type": "turn.started"}}', flush=True)
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "needle"),
    [(CRASHING_CLI, "exited with status 3"), (SILENT_CLI, "ended without a final message")],
)
async def test_a_cli_that_crashes_or_says_nothing_is_an_adapter_error_not_a_model_fail(
    fakes, tmp_path, script: str, needle: str
) -> None:
    cfg, conf, cli_log, jail_log = fakes
    broken = tmp_path / "broken-cli"
    broken.write_text(script.format(python=sys.executable))
    broken.chmod(broken.stat().st_mode | stat.S_IEXEC)

    out = await CliAdapter(cfg, replace(conf, binary=str(broken)), tmp_path).dispatch(
        system_prompt="s",
        user_prompt="u",
        mcp_app=RecordingApp(object(), allowed=KNOWN),
        allowed_tools=KNOWN,
    )

    assert out.finish_reason == "adapter_error"
    assert needle in out.error
    assert out.final_answer == ""
