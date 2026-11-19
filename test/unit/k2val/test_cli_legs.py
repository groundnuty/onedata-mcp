"""The CLI legs (wave 2): the jail interface, the gating, the parsers, and an adapter run with
fake CLI binaries and a fake jail. Never the real CLIs. The `*_SMOKE` streams are real ones,
recorded in the jail (one-word prompt, no MCP server) with their ids masked; the tool-call
events are synthetic, written to the documented shapes, and UNVERIFIED."""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from benchmark.k2val import cli_agents
from benchmark.k2val.cli_agents import (
    AGY_MCP_CONFIG,
    CliAdapter,
    agy_argv,
    agy_mcp_config,
    codex_argv,
    leg_enabled,
    parse_agy_events,
    parse_codex_events,
)
from benchmark.k2val.config import ConfigError, JailConf, parse_config
from benchmark.k2val.jail import REPO_ROOT, JailError, jail_available, jail_cmd
from benchmark.k2val.recording import CallRecord, RecordingApp
from benchmark.k2val.scan import build_record, fingerprint, scan_texts, write_scan_record
from test.unit.k2val.conftest import TOKEN_LOCAL, TOKEN_OWNER, raw_config

KNOWN = frozenset({"check_readiness", "ensure_readiness", "get_file_distribution"})

# Real: codex 0.169 `exec --json`, gpt-5.6-luna, "Reply with the single word OK."
CODEX_SMOKE = [
    {"type": "thread.started", "thread_id": "<id>"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "<id>", "type": "agent_message", "text": "OK"}},
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 11781,
            "cached_input_tokens": 10112,
            "cache_write_input_tokens": 0,
            "output_tokens": 5,
            "reasoning_output_tokens": 0,
        },
    },
]

# Real: agy 1.5.12 `--output-format stream-json`, gemini-3.8-flash-low, the same prompt.
AGY_SMOKE = [
    {
        "event": "init",
        "conversation_id": "<id>",
        "init": {
            "model": "gemini-3.8-flash-low",
            "cwd": "/work",
            "tools": ["ask_question", "call_mcp_tool", "run_command"],
            "permission_mode": "always-proceed",
        },
    },
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": "<id>",
            "step_index": 0,
            "state": "DONE",
            "step_type": "user_input",
        },
    },
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": "<id>",
            "step_index": 1,
            "state": "ACTIVE",
            "step_type": "agent_response",
            "text_delta": "OK",
        },
    },
    {
        "event": "step_update",
        "step_update": {
            "conversation_id": "<id>",
            "step_index": 1,
            "state": "DONE",
            "step_type": "agent_response",
            "text_delta": "\n",
            "duration_seconds": 2.64,
            "usage": {"input_tokens": 8712, "output_tokens": 1, "thinking_tokens": 0},
        },
    },
    {
        "event": "result",
        "result": {
            "conversation_id": "<id>",
            "status": "SUCCESS",
            "response": "OK\n",
            "duration_seconds": 2.72,
            "num_turns": 1,
            "usage": {
                "input_tokens": 8712,
                "output_tokens": 1,
                "thinking_tokens": 0,
                "cache_read_tokens": 0,
                "total_tokens": 8713,
            },
        },
    },
]

# Synthetic (UNVERIFIED shapes): codex tool items.
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


def _step(index: int, step_type: str, state: str, **extra) -> dict:
    return {
        "event": "step_update",
        "step_update": {"step_index": index, "step_type": step_type, "state": state, **extra},
    }


# The real agy envelope with tool steps added. The tool-step fields are UNVERIFIED.
AGY_RUN = [
    AGY_SMOKE[0],
    AGY_SMOKE[1],
    _step(
        1,
        "call_mcp_tool",
        "ACTIVE",
        arguments={"server_name": "onedata", "tool_name": "check_readiness", "extent": []},
    ),
    _step(1, "call_mcp_tool", "DONE", output='{"ready": false}'),
    _step(2, "run_command", "ACTIVE", arguments={"command": "ls"}),
    _step(2, "run_command", "ERROR", output="denied"),
    _step(3, "agent_response", "ACTIVE", text_delta="Not "),
    _step(3, "agent_response", "DONE", text_delta="ready."),
    {
        "event": "result",
        "result": {
            "status": "SUCCESS",
            "response": "Not ready.",
            "usage": {"input_tokens": 500, "output_tokens": 40, "thinking_tokens": 10},
        },
    },
]


def jsonl(events) -> list[str]:
    return [json.dumps(e) for e in events]


# --- the jail interface ----------------------------------------------------------------------


def test_with_no_wrapper_there_is_no_jail_and_nothing_runs() -> None:
    assert not jail_available(JailConf())
    with pytest.raises(JailError, match="never run unjailed"):
        jail_cmd(["codex", "exec"], "/tmp/work", JailConf(), cli="codex")
    with pytest.raises(JailError):
        jail_cmd(["codex"], "/tmp/work", JailConf(wrapper="   "), cli="codex")


def test_jail_cmd_builds_the_wrapper_call() -> None:
    conf = JailConf(wrapper="/path/to/cli-jail.sh")
    assert jail_cmd(
        ["agy", "--print=x"],
        "/tmp/w",
        conf,
        cli="agy",
        mcp_upstream="127.0.0.1:41999",
        home_files=[(AGY_MCP_CONFIG, "/tmp/s/agy-mcp-config.json")],
    ) == [
        "/path/to/cli-jail.sh",
        "--cli",
        "agy",
        "--workdir",
        "/tmp/w",
        "--mcp-upstream",
        "127.0.0.1:41999",
        "--home-file",
        ".gemini/config/mcp_config.json=/tmp/s/agy-mcp-config.json",
        "--",
        "agy",
        "--print=x",
    ]
    assert jail_cmd(["codex"], "/tmp/w", conf, cli="codex") == [
        "/path/to/cli-jail.sh",
        "--cli",
        "codex",
        "--workdir",
        "/tmp/w",
        "--",
        "codex",
    ]


def test_there_is_no_way_to_copy_the_jails_home_out() -> None:
    """--keep-home is gone: the trace's MCP half is the server's record, the CLI's events come
    on stdout, and nothing of the jail's home is ever copied to the host."""
    with pytest.raises(TypeError):
        jail_cmd(["codex"], "/tmp/w", JailConf("/j"), cli="codex", keep_home="/tmp/k")
    assert not (REPO_ROOT / "benchmark/k2val/jail/keep_home.py").exists()


def test_a_relative_wrapper_is_taken_from_the_repository_root() -> None:
    cmd = jail_cmd(["codex"], "/tmp/w", JailConf("benchmark/k2val/jail/cli-jail.sh"), cli="codex")
    assert cmd[0] == str(REPO_ROOT / "benchmark/k2val/jail/cli-jail.sh")
    assert Path(cmd[0]).is_file()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"cli": "claude"}, "cli must be"),
        ({"cli": "codex", "mcp_upstream": "10.0.0.5:80"}, "127.0.0.1"),
        ({"cli": "codex", "mcp_upstream": "127.0.0.1:80/x"}, "127.0.0.1"),
    ],
)
def test_jail_cmd_refuses_bad_arguments(kwargs, match) -> None:
    with pytest.raises(ValueError, match=match):
        jail_cmd(["codex"], "/tmp/w", JailConf("/j"), **kwargs)


def test_the_example_config_names_the_jail_script() -> None:
    example = (REPO_ROOT / "benchmark/k2val/k2val.toml.example").read_text()
    assert 'wrapper = "benchmark/k2val/jail/cli-jail.sh"' in example


def test_mcp_add_mode_is_refused(secret_files) -> None:
    raw = raw_config(secret_files)
    raw["models"]["gpt-luna-low"]["mcp_attach"] = "mcp_add"
    with pytest.raises(ConfigError, match="only 'home'"):
        parse_config(raw)
    raw["models"]["gpt-luna-low"]["mcp_attach"] = "settings"  # the old name is still accepted
    parse_config(raw)


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
    assert argv[:6] == [
        "codex",
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",  # bwrap is the sandbox
    ]
    assert argv[6:8] == ["-m", "gpt-5.6-luna"]
    assert 'model_reasoning_effort="low"' in argv
    assert 'mcp_servers.onedata.url="http://127.0.0.1:41999/mcp"' in argv  # a URL, nothing more
    assert argv[-3:] == ["-o", "last_message.txt", "the prompt"]
    assert "-s" not in argv and "read-only" not in argv
    flat = " ".join(argv)
    assert TOKEN_LOCAL not in flat and "token" not in flat.lower() and "ONEDATA" not in flat


def test_codex_command_line_without_an_mcp_server(cfg) -> None:
    argv = codex_argv(cfg.models["gpt-luna-low"], None, "p")
    assert not any("mcp_servers" in a for a in argv)


def test_agy_command_line_and_its_mcp_config(cfg) -> None:
    conf = replace(cfg.models["gpt-luna-low"], backend="agy", binary="agy", reasoning_effort="")
    conf = replace(conf, api_id="gemini-3.8-flash-low", effort="low")
    argv = agy_argv(conf, "-the prompt starting with a dash")
    assert argv == [
        "agy",
        "--output-format",
        "stream-json",
        "--model",
        "gemini-3.8-flash-low",
        "--effort",
        "low",
        "--dangerously-skip-permissions",
        "--print=-the prompt starting with a dash",  # one argv word: never read as a flag
    ]
    assert "--sandbox" not in argv and "--settings" not in argv
    # the file `agy mcp add` writes, which the jail copies into the fresh home
    assert agy_mcp_config("http://127.0.0.1:41999/mcp") == {
        "mcpServers": {"onedata": {"disabled": False, "serverUrl": "http://127.0.0.1:41999/mcp"}}
    }
    assert AGY_MCP_CONFIG == ".gemini/config/mcp_config.json"


# --- the parsers -----------------------------------------------------------------------------


def test_the_real_codex_stream_parses() -> None:
    run = parse_codex_events(["Reading additional input from stdin...", *jsonl(CODEX_SMOKE)])
    assert run.final_answer == "OK"
    assert (run.tokens_in, run.tokens_out) == (11781, 5)
    assert run.events == [] and run.error is None and run.unparsed == 1


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


def test_the_real_agy_stream_parses() -> None:
    warning = "cli-jail: warning: agy rewrote its login file(s) inside the jail: ..."
    run = parse_agy_events([*jsonl(AGY_SMOKE), warning], KNOWN)
    assert run.final_answer == "OK"  # result.response, stripped
    assert (run.tokens_in, run.tokens_out) == (8712, 1)
    assert run.events == [] and run.error is None and run.unparsed == 1


def test_agy_answer_falls_back_to_the_last_response_step() -> None:
    no_result = [e for e in AGY_RUN if e["event"] != "result"]
    assert parse_agy_events(jsonl(no_result), KNOWN).final_answer == "Not ready."


def test_agy_tool_steps_separate_mcp_calls_from_builtin_tools() -> None:
    run = parse_agy_events(jsonl(AGY_RUN), KNOWN)

    assert run.final_answer == "Not ready."
    assert [(e.source, e.tool, e.ok) for e in run.events] == [
        ("mcp", "check_readiness", True),
        ("builtin", "run_command", False),
    ]
    assert run.events[0].result == '{"ready": false}'
    assert run.events[1].error == "denied"
    assert (run.tokens_in, run.tokens_out) == (500, 50)  # output + thinking tokens


def test_agy_failure_result() -> None:
    bad = {"event": "result", "result": {"status": "FAILED", "error": {"message": "quota"}}}
    run = parse_agy_events(jsonl([bad]), KNOWN)
    assert run.error == "quota" and run.final_answer == ""


# --- an adapter run with fake binaries and a fake jail ----------------------------------------


FAKE_CLI = """#!{python}
import json, os, sys
log = {log!r}
with open(log, "a") as f:
    row = {{"argv": sys.argv[1:], "cwd": os.getcwd(), "ls": sorted(os.listdir("."))}}
    f.write(json.dumps(row) + "\\n")
args = sys.argv[1:]
if args[:1] == ["exec"]:
    for line in {codex!r}:
        print(line)
    open("last_message.txt", "w").write("Not ready: two files are still being written.")
else:
    for line in {agy!r}:
        print(line)
"""

# A fake jail: records its arguments, environment and home files, then runs the command in
# the work dir. `--cli X --workdir W [--mcp-upstream U] [--home-file R=F]... -- argv`.
FAKE_JAIL = """#!{python}
import json, os, signal, sys, time
args = sys.argv[1:]
cut = args.index("--")
opts, argv = args[:cut], args[cut + 1:]
pairs = list(zip(opts[0::2], opts[1::2]))
home_files = {{}}
for k, v in pairs:
    if k == "--home-file":
        rel, src = v.split("=", 1)
        home_files[rel] = open(src).read()
row = {{"opts": opts, "env": dict(os.environ), "home_files": home_files}}
with open({log!r}, "a") as f:
    f.write(json.dumps(row) + "\\n")
if os.path.exists({log!r} + ".hang"):  # (the jail's env is fixed: a file is the only signal)
    def on_term(*_):
        open({log!r} + ".term", "w").write("TERM")
        sys.exit(143)
    signal.signal(signal.SIGTERM, on_term)
    print("started", flush=True)
    time.sleep(60)
os.chdir(dict(pairs)["--workdir"])
os.execv(argv[0], argv)
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
    jail.write_text(FAKE_JAIL.format(python=sys.executable, log=str(jail_log)))
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
    monkeypatch.setenv("K2VAL_JAIL_TMPDIR", str(tmp_path))
    FakeServer.instances.clear()
    conf = replace(cfg.models["gpt-luna-low"], binary=str(cli))
    jailed = replace(cfg, jail=JailConf(wrapper=str(jail)), models={**cfg.models, conf.name: conf})
    return jailed, conf, cli_log, jail_log


def log_rows(path: Path) -> list[dict]:
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

    (jail_row,) = log_rows(jail_log)
    opts = dict(zip(jail_row["opts"][0::2], jail_row["opts"][1::2], strict=True))
    scratch = Path(FakeServer.instances[0].scratch)
    assert opts == {
        "--cli": "codex",
        "--workdir": str(scratch / "work"),
        "--mcp-upstream": "127.0.0.1:41999",
    }
    # the jail script's own env: tools, $HOME for the logins, and the staging root (the
    # trial's scratch, so removing it removes the staging even after a SIGKILL)
    assert set(jail_row["env"]) == {
        "PATH",
        "LANG",
        "HOME",
        "K2VAL_JAIL_TMPDIR",
        "K2VAL_JAIL_PYTHON",
    }
    assert jail_row["env"]["K2VAL_JAIL_TMPDIR"] == str(scratch)
    assert scratch.parent == tmp_path
    (run,) = log_rows(cli_log)
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
    assert not scratch.exists()  # the scratch dir (with the work dir) is gone


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
async def test_an_agy_trial_gets_its_mcp_server_through_the_fresh_home(fakes, tmp_path) -> None:
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

    (jail_row,) = log_rows(jail_log)
    assert jail_row["opts"][:2] == ["--cli", "agy"]
    assert json.loads(jail_row["home_files"][AGY_MCP_CONFIG]) == agy_mcp_config(
        "http://127.0.0.1:41999/mcp"
    )
    (run,) = log_rows(cli_log)
    assert run["ls"] == []  # the MCP config is in the home, not in /work
    assert "--dangerously-skip-permissions" in run["argv"] and "--sandbox" not in run["argv"]
    assert run["argv"][-1] == "--print=s\n\nu"
    assert out.final_answer == "Not ready."
    assert [c.source for c in app.calls] == ["mcp", "mcp", "builtin"]


@pytest.mark.asyncio
async def test_a_cancelled_trial_terminates_the_jail_first(fakes, tmp_path) -> None:
    cfg, conf, cli_log, jail_log = fakes
    Path(str(jail_log) + ".hang").touch()
    task = asyncio.create_task(
        CliAdapter(cfg, conf, tmp_path).dispatch(
            system_prompt="s",
            user_prompt="u",
            mcp_app=RecordingApp(object(), allowed=KNOWN),
            allowed_tools=KNOWN,
        )
    )
    for _ in range(100):
        if jail_log.exists():
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # SIGTERM, which the jail script turns into a clean teardown, not an immediate SIGKILL
    assert Path(str(jail_log) + ".term").read_text() == "TERM"


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
