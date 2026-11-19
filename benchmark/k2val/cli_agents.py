"""The CLI agent legs (wave 2): `gpt-luna-low` (Codex CLI) and `gemini-flash-low` (agy).

DISABLED by default. A leg runs only when `[cli.jail] wrapper` is set AND the latest scan for
the config is clean (`leg_enabled`); otherwise the run skips it, says so, and goes on with the
API configs. Neither CLI is ever started except through `jail_cmd` (`jail/cli-jail.sh`). Their
MCP server runs outside the jail on 127.0.0.1:<P> (`mcp_http.McpHttpServer`); the jail relays
127.0.0.1:<P> inside its own network namespace to it, so the CLI is given the same URL and
nothing else. bwrap is the sandbox: codex runs with its own sandbox and approvals off, agy with
tool permissions auto-approved, both only inside the jail.

What is pinned and what is not:

- the flags below come from `codex exec --help` (0.169) and `agy --help` (1.5.12);
- agy takes MCP servers only from `~/.gemini/config/mcp_config.json` (`mcpServers.<name>.
  serverUrl`, the file `agy mcp add` writes); its `--settings` rejects `mcpServers`. The jail
  copies that file, written by the adapter, into the fresh home (`--home-file`);
- checked against real jailed runs (a one-word prompt, no MCP server): the codex `--json`
  and agy `stream-json` envelopes of a plain answer; see design/08;
- NOT yet seen in a real run: the tool-call events (codex `mcp_tool_call` /
  `command_execution` items, agy tool events); their payload fields are UNVERIFIED. The MCP
  calls do not depend on either parser: the loopback server records every call itself, and
  that record is the trace's MCP half. The parsers supply the final answer, the token counts,
  and the CLI's built-in tool calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from benchmark._runtime_types import ToolCall
from benchmark.k2val.config import FederationSecrets, K2valConfig, ModelConf
from benchmark.k2val.jail import (
    JailError,
    JailFileError,
    jail_available,
    jail_cmd,
    read_jail_file,
)
from benchmark.k2val.mcp_http import McpHttpServer
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.secrets import redact
from benchmark.llm_adapters._protocol import LLMConfig, TrialDispatch

MCP_SERVER_NAME = "onedata"
LAST_MESSAGE_FILE = "last_message.txt"
AGY_MCP_CONFIG = ".gemini/config/mcp_config.json"  # its place in the jail's fresh home
AGY_MCP_CONFIG_FILE = "agy-mcp-config.json"  # where the adapter writes it, outside /work
JAIL_TMPDIR_ENV = "K2VAL_JAIL_TMPDIR"
TERMINATE_GRACE_S = 10.0
MAX_JAIL_OUTPUT_BYTES = 64 << 20  # the jail's stdout+stderr; a real trial's events are far smaller


# ---------------------------------------------------------------------------
# Parsed runs
# ---------------------------------------------------------------------------


@dataclass
class CliEvent:
    """One tool call the CLI reports."""

    source: str  # "mcp" or "builtin"
    tool: str
    arguments: dict = field(default_factory=dict)
    result: str | None = None
    ok: bool = True
    error: str | None = None


@dataclass
class ParsedRun:
    final_answer: str = ""
    events: list[CliEvent] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None
    unparsed: int = 0  # lines that were not JSON


def _json_lines(lines: Iterable[str]) -> Iterable[dict | None]:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            yield None
            continue
        yield value if isinstance(value, dict) else None


def _text_of_result(result: Any) -> str | None:
    """An MCP result as codex reports it: `{content: [{type: text, text}], structured_content}`."""
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        parts = [
            c["text"]
            for c in result.get("content") or []
            if isinstance(c, dict) and isinstance(c.get("text"), str)
        ]
        if parts:
            return "".join(parts)
        structured = result.get("structured_content", result.get("structuredContent"))
        if structured is not None:
            return json.dumps(structured, ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False, default=str)


def parse_codex_events(lines: Iterable[str]) -> ParsedRun:
    """`codex exec --json` JSONL: items of type `mcp_tool_call` are MCP calls; `command_execution`,
    `file_change`, `web_search` are the CLI's built-in tools; the last `agent_message` is the
    answer; `turn.completed` carries the usage."""
    run = ParsedRun()
    for ev in _json_lines(lines):
        if ev is None:
            run.unparsed += 1
            continue
        kind = ev.get("type")
        if kind == "item.completed":
            item = ev.get("item") or {}
            t = item.get("type")
            if t == "agent_message":
                run.final_answer = item.get("text") or run.final_answer
            elif t == "mcp_tool_call":
                err = item.get("error")
                message = err.get("message") if isinstance(err, dict) else err
                run.events.append(
                    CliEvent(
                        "mcp",
                        item.get("tool", "?"),
                        item.get("arguments") or {},
                        _text_of_result(item.get("result")),
                        ok=not message and item.get("status") != "failed",
                        error=message or None,
                    )
                )
            elif t == "command_execution":
                code = item.get("exit_code")
                run.events.append(
                    CliEvent(
                        "builtin",
                        "shell",
                        {"command": item.get("command")},
                        item.get("aggregated_output"),
                        ok=code in (0, None),
                        error=None if code in (0, None) else f"exit {code}",
                    )
                )
            elif t in ("file_change", "web_search"):
                run.events.append(
                    CliEvent("builtin", t, {k: v for k, v in item.items() if k != "id"})
                )
            elif t == "error":
                run.error = item.get("message") or run.error
        elif kind == "turn.completed":
            usage = ev.get("usage") or {}
            run.tokens_in += int(usage.get("input_tokens") or 0)
            run.tokens_out += int(usage.get("output_tokens") or 0)
        elif kind == "turn.failed":
            err = ev.get("error") or {}
            run.error = err.get("message") if isinstance(err, dict) else str(err)
        elif kind == "error":
            run.error = ev.get("message") or run.error
    return run


AGY_MCP_CALL_TOOL = "call_mcp_tool"  # agy's built-in tool that calls a configured MCP server's
AGY_TEXT_STEPS = ("user_input", "agent_response")


def _is_mcp_tool_name(name: str, known: frozenset[str]) -> bool:
    """agy may prefix an MCP tool with its server name; a name ending in a known tool is MCP."""
    return name == AGY_MCP_CALL_TOOL or any(
        name == k or re.fullmatch(rf"(?:mcp[_.:-]+)?{MCP_SERVER_NAME}[_.:-]+{k}", name)
        for k in known
    )


def _agy_tool_event(step: dict, known_tools: frozenset[str]) -> CliEvent:
    """A tool step of agy's stream (UNVERIFIED: no real tool call seen yet). The tool name is
    read from `tool_name`/`name`/`tool`, else the step type; `call_mcp_tool` is agy's route to an
    MCP server, with the MCP tool's own name in its arguments when given."""
    name = step.get("tool_name") or step.get("name") or step.get("tool") or step["step_type"]
    raw_args = step.get("arguments", step.get("args", step.get("input", {})))
    args = raw_args if isinstance(raw_args, dict) else {"value": raw_args}
    if name == AGY_MCP_CALL_TOOL:
        name = args.get("tool_name") or args.get("name") or name
    return CliEvent("mcp" if _is_mcp_tool_name(name, known_tools) else "builtin", name, args)


def parse_agy_events(lines: Iterable[str], known_tools: frozenset[str]) -> ParsedRun:
    """`agy --output-format stream-json --print=...`, as seen in a real jailed run (agy 1.5.12).
    Each line is `{"event": <kind>, <kind>: {...}}`:

    - `init` {conversation_id, cwd, model, permission_mode, tools};
    - `step_update` {conversation_id, step_index, step_type, state, text_delta?, usage?,
      duration_seconds?}: one step goes ACTIVE -> DONE over several updates; the answer streams
      as `text_delta`s of `agent_response` steps;
    - `result` {conversation_id, status ("SUCCESS"), response, num_turns, duration_seconds,
      usage {input_tokens, output_tokens, thinking_tokens, cache_read_tokens, total_tokens}}.

    The final answer is `result.response` (else the last `agent_response` text); the tokens
    are `result.usage` (output = output + thinking tokens). Steps of any other type are read as
    tool calls: that part is UNVERIFIED (see `_agy_tool_event`)."""
    run = ParsedRun()
    texts: dict[int, list[str]] = {}
    tools: dict[int, CliEvent] = {}
    response: str | None = None
    for ev in _json_lines(lines):
        if ev is None:
            run.unparsed += 1
            continue
        kind = ev.get("event")
        body = ev.get(kind) if isinstance(ev.get(kind), dict) else {}
        if kind == "step_update":
            index, step_type = body.get("step_index"), body.get("step_type")
            if step_type == "agent_response":
                texts.setdefault(index, []).append(body.get("text_delta") or "")
            elif step_type and step_type not in AGY_TEXT_STEPS:
                if index not in tools:
                    tools[index] = _agy_tool_event(body, known_tools)
                    run.events.append(tools[index])
                event = tools[index]
                state = str(body.get("state", "")).upper()
                if "output" in body or "result" in body:
                    out = body.get("output", body.get("result"))
                    event.result = out if isinstance(out, str) or out is None else json.dumps(out)
                if state in ("ERROR", "FAILED", "CANCELLED"):
                    event.ok = False
                    event.error = body.get("error") or event.result or state.lower()
        elif kind == "result":
            response = body.get("response")
            usage = body.get("usage") or {}
            run.tokens_in = int(usage.get("input_tokens") or 0)
            run.tokens_out = int(usage.get("output_tokens") or 0) + int(
                usage.get("thinking_tokens") or 0
            )
            if body.get("status") not in (None, "SUCCESS"):
                error = body.get("error")
                message = error.get("message") if isinstance(error, dict) else error
                run.error = message or f"agy status {body.get('status')}"
        elif kind == "error":
            body = body or ev
            run.error = body.get("message") or str(body)
    if response is not None:
        run.final_answer = response.strip()
    elif texts:
        run.final_answer = "".join(texts[max(texts, key=lambda i: (i is not None, i or 0))]).strip()
    return run


# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------


def codex_argv(conf: ModelConf, url: str | None, prompt: str) -> list[str]:
    """`codex exec --json --ephemeral`, model and effort, the MCP server by URL (a per-invocation
    override; the jail's codex home holds no config.toml at all). Codex's own sandbox and
    approvals are off: bwrap is the sandbox. The last message goes to a file in /work.
    `url=None` attaches no MCP server (the jail smoke)."""
    mcp = ["-c", f'mcp_servers.{MCP_SERVER_NAME}.url="{url}"'] if url else []
    return [
        conf.binary,
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-m",
        conf.api_id,
        "-c",
        f'model_reasoning_effort="{conf.reasoning_effort}"',
        *mcp,
        "-o",
        LAST_MESSAGE_FILE,
        prompt,
    ]


def agy_mcp_config(url: str) -> dict:
    """agy's `mcp_config.json` (the shape `agy mcp add` writes), naming one http server."""
    return {"mcpServers": {MCP_SERVER_NAME: {"disabled": False, "serverUrl": url}}}


def agy_argv(conf: ModelConf, prompt: str) -> list[str]:
    """`agy` in print mode with stream-json events, tool permissions auto-approved (bwrap is the
    sandbox, so agy's own `--sandbox` is not used). `--print` takes the prompt as its value:
    written `--print=<prompt>`, so a prompt can never be read as a flag. The MCP server is not
    on the command line: it is in the fresh home's mcp_config.json."""
    return [
        conf.binary,
        "--output-format",
        "stream-json",
        "--model",
        conf.api_id,
        "--effort",
        conf.reasoning_effort or conf.effort or "low",
        "--dangerously-skip-permissions",
        f"--print={prompt}",
    ]


def jail_scratch(prefix: str = "k2val-cli-") -> Path:
    """A private scratch dir for one CLI trial, under a NEUTRAL temp root (K2VAL_JAIL_TMPDIR, else
    /tmp, never $TMPDIR): the jail shows its bind sources in /proc/self/mountinfo. The jail's
    own staging goes inside it too, so removing it removes everything, even after a SIGKILL."""
    root = os.environ.get(JAIL_TMPDIR_ENV) or "/tmp"
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def leg_enabled(cfg: K2valConfig, conf: ModelConf, artefact_root: Path) -> tuple[bool, str]:
    """May this CLI leg run? Needs the jail wrapper AND a clean, current scan record."""
    from benchmark.k2val.scan import latest_scan_is_clean

    if not jail_available(cfg.jail):
        return False, "wave 2, disabled: [cli.jail] wrapper is not set"
    ok, why = latest_scan_is_clean(cfg, conf, artefact_root)
    if not ok:
        return False, f"wave 2, disabled: {why}"
    return True, "enabled"


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class CliAdapter:
    """Runs one trial through a CLI: loopback MCP server, jailed CLI, parsed trace."""

    def __init__(self, cfg: K2valConfig, conf: ModelConf, run_dir: Path) -> None:
        self.cfg = cfg
        self.conf = conf
        self.run_dir = run_dir
        self.config = LLMConfig(name=conf.name, model_id=conf.api_id)

    async def dispatch(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        mcp_app: Any,  # the RecordingApp: the CLI's calls are written into its `calls`
        allowed_tools: frozenset[str],
    ) -> TrialDispatch:
        if not jail_available(self.cfg.jail):
            raise JailError("no jail configured: the CLI legs never run unjailed")
        scratch = jail_scratch()
        workdir = scratch / "work"
        workdir.mkdir()
        prompt = f"{system_prompt}\n\n{user_prompt}"
        t0 = time.perf_counter()
        server = McpHttpServer(self.cfg.path or "", allowed_tools, scratch)
        try:
            async with server:
                home_files: list[tuple[str, str]] = []
                if self.conf.backend == "codex":
                    argv = codex_argv(self.conf, server.url, prompt)
                else:
                    config = scratch / AGY_MCP_CONFIG_FILE
                    config.write_text(json.dumps(agy_mcp_config(server.url)), encoding="utf-8")
                    home_files.append((AGY_MCP_CONFIG, str(config)))
                    argv = agy_argv(self.conf, prompt)
                upstream = urlsplit(server.url).netloc  # 127.0.0.1:<P>, the same inside the jail
                run = await self._run(argv, workdir, scratch, upstream, home_files)
            records = server.records()
        finally:
            # /work is jail-written: never follow, block on, or slurp what is there
            refused: str | None = None
            try:
                last_message = (read_jail_file(workdir, LAST_MESSAGE_FILE) or "").strip()
            except JailFileError as e:
                last_message, refused = "", str(e)
            shutil.rmtree(scratch, ignore_errors=True)
        lines, returncode = run.lines, run.returncode

        if self.conf.backend == "codex":
            parsed = parse_codex_events(lines)
            if last_message:
                parsed.final_answer = last_message
        else:
            parsed = parse_agy_events(lines, allowed_tools)

        builtin = [e for e in parsed.events if e.source == "builtin"]
        mcp_apps = list(records)
        for b in builtin:
            now = time.time()
            mcp_apps.append(
                CallRecord(
                    index=0,
                    tool=b.tool,
                    arguments=b.arguments,
                    started_at=now,
                    ended_at=now,
                    ok=b.ok,
                    result_text=b.result,
                    error=b.error,
                    source="builtin",
                )
            )
        for i, rec in enumerate(mcp_apps):
            rec.index = i
        mcp_app.calls.extend(mcp_apps)
        mcp_seen = [e for e in parsed.events if e.source == "mcp"]
        note = None
        if len(mcp_seen) != len(records):
            note = f"cli reported {len(mcp_seen)} MCP calls, the server recorded {len(records)}"
        error = None
        if run.truncated:
            error = f"the CLI's output passed {MAX_JAIL_OUTPUT_BYTES} bytes; the jail was stopped"
        elif refused:  # something other than a plain file sits where the CLI writes its answer
            error = f"the jail's {LAST_MESSAGE_FILE} was not read: {refused}"
        elif returncode not in (0, None):
            error = parsed.error or f"the CLI exited with status {returncode}"
        elif not parsed.final_answer:
            error = parsed.error or "the CLI ended without a final message"
        if error:
            tail = " | ".join(line.strip() for line in lines[-3:])[:300]
            error = f"{error} (last output: {tail})" if tail else error
        return TrialDispatch(
            final_answer=parsed.final_answer,
            tool_calls=tuple(
                ToolCall(r.tool, r.arguments, r.ok, r.error, (r.result_text or "")[:8192] or None)
                for r in mcp_apps
            ),
            rounds_used=len(records),
            finish_reason="adapter_error" if error else "stop",
            usage_in_tokens=parsed.tokens_in,
            usage_out_tokens=parsed.tokens_out,
            wall_clock_seconds=time.perf_counter() - t0,
            raw_trace={
                "cli": self.conf.backend,
                "returncode": returncode,
                "events": [redact(line) for line in lines],
                "mcp_calls_from_cli": len(mcp_seen),
                "mcp_calls_from_server": len(records),
                "builtin_calls": len(builtin),
                "count_mismatch": note,
                "unparsed_lines": parsed.unparsed,
                "output_truncated": run.truncated,
                "refused_jail_file": refused,
            },
            error=redact(error) if error else None,
        )

    async def _run(
        self,
        argv: list[str],
        workdir: Path,
        scratch: Path,
        upstream: str | None,
        home_files: list[tuple[str, str]],
    ) -> JailedRun:
        cmd = jail_cmd(
            argv,
            str(workdir),
            self.cfg.jail,
            cli=self.conf.backend,
            mcp_upstream=upstream,
            home_files=home_files,
        )
        return await run_jailed(cmd, scratch, workdir)


def jail_env(scratch: Path) -> dict[str, str]:
    """The jail script's own environment: what it needs to find the tools and the CLI's login
    ($HOME), and where to stage (inside the trial's scratch). The jailed CLI gets none of it."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": os.environ.get("HOME", ""),
        JAIL_TMPDIR_ENV: str(scratch),
        "K2VAL_JAIL_PYTHON": sys.executable,
    }


@dataclass
class JailedRun:
    lines: list[str]
    returncode: int | None
    truncated: bool = False  # the output passed MAX_JAIL_OUTPUT_BYTES and the jail was stopped


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM (the script then removes its staging dir and ends the jail), SIGKILL after a grace
    period."""
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), TERMINATE_GRACE_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()


async def run_jailed(cmd: list[str], scratch: Path, cwd: Path) -> JailedRun:
    """Run the jail script, its stdout and stderr merged. The output is jail-written, so it is
    read in chunks and capped at MAX_JAIL_OUTPUT_BYTES: past that the jail is stopped and the run
    marked truncated, instead of the harness filling its memory. On cancellation: `_stop`."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd),
        env=jail_env(scratch),
    )
    assert proc.stdout is not None
    chunks: list[bytes] = []
    size = 0
    truncated = False
    try:
        while data := await proc.stdout.read(65536):
            if size + len(data) > MAX_JAIL_OUTPUT_BYTES:
                chunks.append(data[: MAX_JAIL_OUTPUT_BYTES - size])
                truncated = True
                break
            chunks.append(data)
            size += len(data)
        if truncated:
            await _stop(proc)
        await proc.wait()
    except asyncio.CancelledError:
        await _stop(proc)
        raise
    lines = b"".join(chunks).decode("utf-8", errors="replace").splitlines()
    return JailedRun(lines, proc.returncode, truncated)


def make_cli_adapter(
    cfg: K2valConfig, conf: ModelConf, run_dir: Path, secrets: FederationSecrets | None = None
) -> CliAdapter:
    return CliAdapter(cfg, conf, run_dir)
