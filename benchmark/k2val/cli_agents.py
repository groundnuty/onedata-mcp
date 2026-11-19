"""The CLI agent legs (wave 2): `gpt-luna-low` (Codex CLI) and `gemini-flash-low` (agy).

DISABLED by default. A leg runs only when `[cli.jail] wrapper` is set AND the latest scan for
the config is clean (`leg_enabled`); otherwise the run skips it, says so, and goes on with the
API configs. Neither CLI is ever started except through `jail_cmd`, and their MCP server runs
outside the jail on loopback (`mcp_http.McpHttpServer`): the CLI is given a URL and nothing else.

What is pinned and what is not (offline, with no real CLI session):

- the flags below come from `codex exec --help` (0.169) and `agy --help` (1.5.12);
- codex's `--json` event names (`thread.started`, `item.completed`, `turn.completed`, ...) are
  present in the binary; the item payload fields are written from the documented exec schema
  and are UNVERIFIED;
- agy's `stream-json` schema is NOT known: `parse_agy_events` is written against the
  Gemini-CLI-style events (`init`, `message`, `tool_use`, `tool_result`, `result`) and is
  UNVERIFIED. The MCP calls do not depend on either parser: the loopback server records every
  call itself, and that record is the trace's MCP half. The parsers supply the final answer,
  the token counts, and the CLI's built-in tool calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmark._runtime_types import ToolCall
from benchmark.k2val.config import FederationSecrets, K2valConfig, ModelConf
from benchmark.k2val.jail import JailError, jail_available, jail_cmd
from benchmark.k2val.mcp_http import McpHttpServer, new_scratch
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.secrets import redact
from benchmark.llm_adapters._protocol import LLMConfig, TrialDispatch

MCP_SERVER_NAME = "onedata"
LAST_MESSAGE_FILE = "last_message.txt"
SETTINGS_FILE = "agy-settings.json"


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


def _is_mcp_tool_name(name: str, known: frozenset[str]) -> bool:
    """agy may prefix an MCP tool with its server name; a name ending in a known tool is MCP."""
    return any(
        name == k or re.fullmatch(rf"(?:mcp[_.:-]+)?{MCP_SERVER_NAME}[_.:-]+{k}", name)
        for k in known
    )


def parse_agy_events(lines: Iterable[str], known_tools: frozenset[str]) -> ParsedRun:
    """`agy --print --output-format stream-json`, read as Gemini-CLI-style events (UNVERIFIED):
    `message` (assistant text), `tool_use` {tool_name, tool_id, parameters}, `tool_result`
    {tool_id, status, output}, `result` {stats}, `error`."""
    run = ParsedRun()
    pending: dict[str, CliEvent] = {}
    answer: list[str] = []
    for ev in _json_lines(lines):
        if ev is None:
            run.unparsed += 1
            continue
        kind = ev.get("type")
        if kind == "message" and ev.get("role", "assistant") == "assistant":
            text = ev.get("content") or ev.get("text") or ""
            if ev.get("delta"):
                answer.append(text)
            else:
                answer = [text]
        elif kind == "tool_use":
            name = ev.get("tool_name") or ev.get("name") or "?"
            event = CliEvent(
                "mcp" if _is_mcp_tool_name(name, known_tools) else "builtin",
                name,
                ev.get("parameters") or ev.get("input") or {},
            )
            run.events.append(event)
            pending[ev.get("tool_id") or ev.get("id") or str(len(run.events))] = event
        elif kind == "tool_result":
            event = pending.get(ev.get("tool_id") or ev.get("id") or "")
            if event is not None:
                out = ev.get("output")
                event.result = out if isinstance(out, str) or out is None else json.dumps(out)
                event.ok = ev.get("status", "success") in ("success", "ok")
                if not event.ok:
                    event.error = event.result
        elif kind == "result":
            stats = ev.get("stats") or {}
            run.tokens_in += int(stats.get("input_tokens") or stats.get("input") or 0)
            run.tokens_out += int(stats.get("output_tokens") or stats.get("output") or 0)
            if ev.get("status") not in (None, "success"):
                run.error = (ev.get("error") or {}).get("message") or str(ev.get("status"))
        elif kind == "error":
            run.error = ev.get("message") or str(ev)
    run.final_answer = "".join(answer)
    return run


# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------


def codex_argv(conf: ModelConf, url: str, prompt: str) -> list[str]:
    """`codex exec --json --ephemeral -s read-only`, model and effort, the MCP server by URL
    (a per-invocation override: the user's codex config is never modified). The last message
    goes to a file in the (empty) work dir."""
    return [
        conf.binary,
        "exec",
        "--json",
        "--ephemeral",
        "-s",
        "read-only",
        "--skip-git-repo-check",
        "-m",
        conf.api_id,
        "-c",
        f'model_reasoning_effort="{conf.reasoning_effort}"',
        "-c",
        f'mcp_servers.{MCP_SERVER_NAME}.url="{url}"',
        "-o",
        LAST_MESSAGE_FILE,
        prompt,
    ]


def agy_settings(url: str) -> dict:
    """Session-only settings declaring the loopback server as an http MCP server."""
    return {"mcpServers": {MCP_SERVER_NAME: {"url": url}}}


def agy_argv(conf: ModelConf, settings_path: str, prompt: str) -> list[str]:
    argv = [
        conf.binary,
        "--print",
        "--output-format",
        "stream-json",
        "--sandbox",
        "--model",
        conf.api_id,
        "--effort",
        conf.reasoning_effort or conf.effort or "low",
    ]
    if conf.mcp_attach == "settings":
        argv += ["--settings", settings_path]
    return [*argv, prompt]


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
        scratch = new_scratch()
        workdir = scratch / "work"
        workdir.mkdir()
        prompt = f"{system_prompt}\n\n{user_prompt}"
        t0 = time.perf_counter()
        added_name: str | None = None
        server = McpHttpServer(self.cfg.path or "", allowed_tools, scratch)
        try:
            async with server:
                if self.conf.backend == "codex":
                    argv = codex_argv(self.conf, server.url, prompt)
                else:
                    settings = workdir / SETTINGS_FILE
                    settings.write_text(json.dumps(agy_settings(server.url)), encoding="utf-8")
                    argv = agy_argv(self.conf, str(settings), prompt)
                    if self.conf.mcp_attach == "mcp_add":
                        added_name = f"k2val-{uuid.uuid4().hex[:8]}"
                        await self._agy_mcp(["add", added_name, server.url], workdir)
                lines, returncode = await self._run(argv, workdir)
                if added_name:
                    await self._agy_mcp(["remove", added_name], workdir)
                    added_name = None
            records = server.records()
        finally:
            if added_name:  # the CLI ended abnormally: still remove the per-run registration
                await self._agy_mcp(["remove", added_name], workdir)
            last = workdir / LAST_MESSAGE_FILE
            last_message = last.read_text(encoding="utf-8").strip() if last.exists() else ""
            shutil.rmtree(scratch, ignore_errors=True)

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
        if returncode not in (0, None):
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
            },
            error=redact(error) if error else None,
        )

    async def _run(self, argv: list[str], workdir: Path) -> tuple[list[str], int | None]:
        cmd = jail_cmd(argv, str(workdir), self.cfg.jail)
        env = {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(workdir),
            env=env,
        )
        try:
            out, _ = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        return out.decode("utf-8", errors="replace").splitlines(), proc.returncode

    async def _agy_mcp(self, args: list[str], workdir: Path) -> None:
        """`agy mcp add|remove` under a unique per-run name, through the jail too."""
        cmd = jail_cmd([self.conf.binary, "mcp", *args], str(workdir), self.cfg.jail)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(workdir),
            env={"PATH": os.environ.get("PATH", "")},
        )
        await proc.wait()


def make_cli_adapter(
    cfg: K2valConfig, conf: ModelConf, run_dir: Path, secrets: FederationSecrets | None = None
) -> CliAdapter:
    return CliAdapter(cfg, conf, run_dir)
