"""The Claude configs of the panel on the operator's subscription: backend `claude_sdk`.

`anthropic` bills an API key per token and is capped by `[spend]`. `claude_sdk` needs no key: it
drives Claude Code (the `claude` CLI, through the Python `claude-agent-sdk`) on the operator's
logged-in subscription, so nothing is billed per token and the spend tracker skips it. Tokens
are recorded when the SDK reports them; the cost of a trial is 0, noted as "subscription" in
the trial's `raw_trace`.

How a trial runs, and how far it is a clean room:

- The trial's MCP server is the loopback HTTP server of the CLI legs (`mcp_http.McpHttpServer`,
  the scenario's tool set, the trial's space tokens in ITS environment only). The SDK's
  `mcp_servers` takes `{"type": "http", "url": ...}` and hands it to the CLI's `--mcp-config`
  (`claude_agent_sdk` 0.1.72, `McpHttpServerConfig` and `_build_command`); a real CLI (2.1.329)
  attached such a server, under `--strict-mcp-config`, in about a second. For that second the
  server is `pending`, so the query is held back until `get_mcp_status()` says `connected` with
  every tool of the scenario: a model started without its tools would be scored as failing.
  Every MCP call is recorded by the server itself, as for the CLI legs: that record is the
  trace, not the SDK's message stream.
- No Claude Code built-in tool is available: `tools=[]` (the CLI's `--tools ""`, which is for
  the built-in set), the names in `BUILTIN_TOOLS` are disallowed as well, and `can_use_tool`
  allows only `mcp__onedata__<tool>` for the tools of the scenario. The earlier adapter found
  that `can_use_tool` alone did not stop every built-in, hence the three layers.
- The scenario's system prompt is the only system prompt supplied (`--system-prompt` replaces
  Claude Code's own), but NOT the only text the model sees: the CLI still adds a fixed preamble
  of about 580 tokens (seen in a session transcript, CLI 2.1.329): its SDK identity line, an
  environment block (working directory, platform, shell, OS, and any notes the host's Claude
  Code configuration adds), the model's name and knowledge cutoff, today's date, a token-budget
  reminder and the logged-in account's email as session context. Nothing of it is switched off
  here.
- Settings: `setting_sources=[]`, NOT `None`: at 0.1.72 `None` passes no `--setting-sources`
  flag, and the CLI then loads user, project and local settings (hooks, CLAUDE.md, ...).
  `--strict-mcp-config` ignores every other MCP server (the account's claude.ai connectors
  among them), `--disable-slash-commands` turns skills off, and the CLI runs in an empty working
  directory with its CLAUDE.md and auto-memory loading switched off. Managed (organisation)
  settings are not covered by `setting_sources`.
- The subscription is what is billed: the API-key variables are blanked in the CLI's
  environment, and the `init` message's `apiKeySource` is checked, so a trial never silently
  runs on an API key. The Onedata tokens are blanked in the CLI's environment too.
- Effort is passed as `--effort` AND as `CLAUDE_CODE_EFFORT_LEVEL`, which the CLI lets override
  the flag. It is not observed: the CLI may still clamp it (a `maxEffort` in managed settings, or
  a level the model does not offer).
- `max_turns` is `[run] max_tool_rounds`; the wall cap is the runner's `asyncio.wait_for` around
  `dispatch`, which cancels it (the SDK client's exit stops the CLI) into `NO_RESULT`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import PermissionResult, ToolPermissionContext, ToolResultBlock

from benchmark._runtime_types import ToolCall
from benchmark.k2val.config import K2valConfig, ModelConf
from benchmark.k2val.mcp_http import McpHttpServer, new_scratch
from benchmark.k2val.recording import CallRecord
from benchmark.k2val.secrets import redact
from benchmark.llm_adapters._protocol import LLMConfig, TrialDispatch
from benchmark.llm_adapters.claude_agent_sdk import _BUILTIN_TOOLS, _content_to_text

MCP_SERVER_NAME = "onedata"
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
TOOL_RESULT_CAP = 8 * 1024
STDERR_TAIL_LINES = 20

# The earlier adapter's list (Claude Code 2.1.12x) plus names of later or MCP-added built-ins.
# Disabling is a second layer: `tools=[]` already offers none.
BUILTIN_TOOLS = (
    *_BUILTIN_TOOLS,
    "Agent",
    "BashOutput",
    "KillShell",
    "ListMcpResourcesTool",
    "MultiEdit",
    "ReadMcpResourceTool",
)

# Variables that would route the CLI to something other than the subscription.
NOT_THE_SUBSCRIPTION = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)
# The `init` message's `apiKeySource` when the CLI uses the logged-in subscription (OAuth).
SUBSCRIPTION_KEY_SOURCES = ("none", "oauth", None)
# `init` MCP server states that mean the trial cannot work (the model would have no tools and
# be scored as failing: a harness fault, not a model failure).
DEAD_MCP_STATES = ("failed", "needs-auth")
MCP_READY_TIMEOUT_S = 30.0  # the server is `pending` for about a second after the CLI starts
MCP_READY_POLL_S = 0.25


def resolve_cli(conf: ModelConf) -> str | None:
    """The `claude` executable: the config's `binary`, else `claude` on PATH, else None (the
    SDK's bundled copy). The bundled copy can be older than the models under test and ignore an
    effort it does not know for them, so a newer one on PATH is preferred."""
    if conf.binary:
        found = shutil.which(conf.binary)
        if found is None:
            raise FileNotFoundError(f"models.{conf.name}.binary {conf.binary!r} is not executable")
        return found
    return shutil.which("claude")


def clean_env(tool_call_cap_s: float, effort: str) -> dict[str, str]:
    """What the CLI gets on top of the harness's environment (the SDK cannot unset a variable,
    so a variable to remove is set to the empty string)."""
    env = dict.fromkeys(
        [k for k in os.environ if k.startswith("ONEDATA_")] + list(NOT_THE_SUBSCRIPTION), ""
    )
    env.update(
        {
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "ENABLE_TOOL_SEARCH": "false",  # the MCP tool definitions stay in the prompt
            "MCP_TOOL_TIMEOUT": str(int(tool_call_cap_s * 1000)),  # ms: one call is bounded
            # The CLI lets this variable override `--effort` (read from the 2.1.329 binary): an
            # operator shell that exports it would otherwise run every config at one effort.
            "CLAUDE_CODE_EFFORT_LEVEL": effort,
        }
    )
    return env


def thinking_option(value: str) -> dict[str, Any] | None:
    """`ModelConf.thinking` as the SDK's `ThinkingConfig`."""
    if not value:
        return None
    if value.isdigit():
        return {"type": "enabled", "budget_tokens": int(value)}
    return {"type": value}  # "adaptive" | "disabled"


def build_options(
    conf: ModelConf,
    *,
    system_prompt: str,
    allowed_tools: frozenset[str],
    max_turns: int,
    tool_call_cap_s: float,
    cwd: Path,
    mcp_url: str | None,
    can_use_tool: Any = None,
    stderr: Any = None,
    cli_path: str | None = None,
) -> ClaudeAgentOptions:
    """The SDK options of one trial. `mcp_url=None` attaches no MCP server (the smoke)."""
    kwargs: dict[str, Any] = {}
    thinking = thinking_option(conf.thinking)
    if thinking is not None:
        kwargs["thinking"] = thinking
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        tools=[],
        allowed_tools=sorted(f"{TOOL_PREFIX}{t}" for t in allowed_tools),
        disallowed_tools=list(BUILTIN_TOOLS),
        mcp_servers={MCP_SERVER_NAME: {"type": "http", "url": mcp_url}} if mcp_url else {},
        permission_mode="default",
        can_use_tool=can_use_tool,
        max_turns=max_turns,
        model=conf.api_id,
        effort=conf.effort,  # type: ignore[arg-type]  # "xhigh": forwarded, not in the SDK's type
        setting_sources=[],
        cwd=str(cwd),
        env=clean_env(tool_call_cap_s, conf.effort),
        extra_args={"strict-mcp-config": None, "disable-slash-commands": None},
        stderr=stderr,
        cli_path=cli_path,
        **kwargs,
    )


def make_can_use_tool(allowed_tools: frozenset[str], denied: list[dict[str, Any]]):
    """The permission gate: only `mcp__onedata__<tool>` of the scenario's tool set."""
    allowed = frozenset(f"{TOOL_PREFIX}{t}" for t in allowed_tools)

    async def can_use_tool(
        tool_name: str, input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResult:
        del context
        if tool_name in allowed:
            return PermissionResultAllow(behavior="allow", updated_input=input)
        denied.append({"tool_name": tool_name, "input": input})
        return PermissionResultDeny(
            behavior="deny",
            message=(
                f"Tool {tool_name!r} is not offered in this scenario; only the "
                f"{TOOL_PREFIX}* tools of the scenario are available."
            ),
            interrupt=False,
        )

    return can_use_tool


@dataclass
class _Session:
    """What one SDK conversation produced, filled message by message."""

    expected_mcp_tools: frozenset[str] = frozenset()  # `mcp__onedata__<tool>` of the scenario
    result: ResultMessage | None = None
    init: dict[str, Any] = field(default_factory=dict)
    mcp_status: dict[str, Any] = field(default_factory=dict)  # the server as it was when ready
    fatal: str | None = None  # the trial is invalid: stop and report it
    exception: str | None = None
    assistant_error: str | None = None
    stderr: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    message_ids: list[str] = field(default_factory=list)
    assistant_messages: int = 0
    mcp_blocks: int = 0
    builtin: dict[str, CallRecord] = field(default_factory=dict)  # by tool_use_id
    rate_limits: list[dict[str, Any]] = field(default_factory=list)

    def on_stderr(self, line: str) -> None:
        self.stderr.append(line)
        del self.stderr[:-STDERR_TAIL_LINES]

    def absorb(self, msg: Any) -> bool:
        """Take one message of the stream. True: stop reading (the trial is invalid)."""
        if isinstance(msg, SystemMessage):
            if msg.subtype == "init":
                self._on_init(msg.data or {})
                return self.fatal is not None
        elif isinstance(msg, AssistantMessage):
            self.assistant_messages += 1
            if msg.model and msg.model not in self.models:
                self.models.append(msg.model)
            if msg.message_id and msg.message_id not in self.message_ids:
                self.message_ids.append(msg.message_id)
            if msg.error:
                self.assistant_error = str(msg.error)
            for block in msg.content:
                if isinstance(block, ToolUseBlock):
                    self._on_tool_use(block)
        elif isinstance(msg, UserMessage):
            content = getattr(msg, "content", None)
            for block in content if isinstance(content, list) else []:
                if isinstance(block, ToolResultBlock):
                    self._on_tool_result(block)
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            if len(self.rate_limits) < 10:
                self.rate_limits.append(
                    {
                        "status": info.status,
                        "type": info.rate_limit_type,
                        "utilization": info.utilization,
                        "resets_at": info.resets_at,
                    }
                )
        elif isinstance(msg, ResultMessage):
            self.result = msg
        return False

    def _on_init(self, data: dict[str, Any]) -> None:
        tools = [t if isinstance(t, str) else str(t.get("name")) for t in data.get("tools") or []]
        servers = [s for s in data.get("mcp_servers") or [] if isinstance(s, dict)]
        self.init = {
            "model": data.get("model"),
            "api_key_source": data.get("apiKeySource"),
            "claude_code_version": data.get("claude_code_version"),
            "tools": tools,
            "mcp_servers": [{"name": s.get("name"), "status": s.get("status")} for s in servers],
        }
        source = data.get("apiKeySource")
        if source not in SUBSCRIPTION_KEY_SOURCES:
            self.fatal = (
                f"the CLI reports apiKeySource={source!r}: it is using an API key, not the "
                "subscription (claude_sdk is the subscription backend)"
            )
            return
        for server in servers:
            if server.get("name") == MCP_SERVER_NAME and server.get("status") in DEAD_MCP_STATES:
                self.fatal = (
                    f"the CLI could not use the {MCP_SERVER_NAME!r} MCP server "
                    f"(status {server.get('status')!r}): no tools, so no trial"
                )
                return
        offered = data.get("tools")
        missing = sorted(self.expected_mcp_tools - set(tools)) if isinstance(offered, list) else []
        if missing:
            self.fatal = (
                f"the session does not offer {missing} (it offers {tools}): the model would run "
                "without the scenario's tools"
            )

    def _on_tool_use(self, block: ToolUseBlock) -> None:
        if block.name.startswith(TOOL_PREFIX):
            self.mcp_blocks += 1  # the MCP server's own record is the trace of these
            return
        now = time.time()
        self.builtin[block.id] = CallRecord(
            index=0,
            tool=block.name,
            arguments=dict(block.input or {}),
            started_at=now,
            ended_at=now,
            ok=False,
            error="no result",
            source="builtin",
        )

    def _on_tool_result(self, block: ToolResultBlock) -> None:
        record = self.builtin.get(block.tool_use_id)
        if record is None:
            return
        text = _content_to_text(getattr(block, "content", None))
        failed = bool(getattr(block, "is_error", False))
        record.ok = not failed
        record.error = (text or "error") if failed else None
        record.result_text = None if failed else text
        record.ended_at = time.time()

    async def wait_for_mcp(self, client: Any, allowed_tools: frozenset[str]) -> None:
        """Hold the query back until the CLI has the `onedata` server connected with every tool
        of the scenario. Sets `fatal` if it does not get there: a dead server at once, any other
        state after MCP_READY_TIMEOUT_S."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MCP_READY_TIMEOUT_S
        seen = "the server was not listed"
        while True:
            status = await client.get_mcp_status()
            server = next(
                (s for s in status.get("mcpServers", []) if s.get("name") == MCP_SERVER_NAME),
                None,
            )
            if server is not None:
                state = server.get("status")
                names = {t.get("name") for t in server.get("tools") or [] if isinstance(t, dict)}
                self.mcp_status = {"status": state, "tools": sorted(n for n in names if n)}
                if state in DEAD_MCP_STATES:
                    self.fatal = (
                        f"the CLI could not use the {MCP_SERVER_NAME!r} MCP server "
                        f"(status {state!r}, {server.get('error')!r}): no tools, so no trial"
                    )
                    return
                missing = sorted(allowed_tools - names)
                if state == "connected" and not missing:
                    return
                seen = f"status {state!r}" + (f", tools missing {missing}" if missing else "")
            if loop.time() >= deadline:
                self.fatal = (
                    f"the {MCP_SERVER_NAME!r} MCP server was not ready after "
                    f"{MCP_READY_TIMEOUT_S:.0f}s ({seen}): no tools, so no trial"
                )
                return
            await asyncio.sleep(MCP_READY_POLL_S)

    def unexpected_tools(self) -> list[str]:
        return [t for t in self.init.get("tools", []) if not t.startswith(TOOL_PREFIX)]


class ClaudeSdkAdapter:
    """Runs one trial through Claude Code on the subscription: loopback MCP server, SDK client,
    the server's record as the MCP half of the trace."""

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
        mcp_app: Any,  # the RecordingApp: the server's calls are written into its `calls`
        allowed_tools: frozenset[str],
    ) -> TrialDispatch:
        cli_path = resolve_cli(self.conf)
        scratch = new_scratch("k2val-claude-")
        work = scratch / "work"
        work.mkdir()
        denied: list[dict[str, Any]] = []
        session = _Session(expected_mcp_tools=frozenset(f"{TOOL_PREFIX}{t}" for t in allowed_tools))
        server = McpHttpServer(self.cfg.path or "", allowed_tools, scratch)
        t0 = time.perf_counter()
        records: list[CallRecord] = []
        try:
            async with server:
                options = build_options(
                    self.conf,
                    system_prompt=system_prompt,
                    allowed_tools=allowed_tools,
                    max_turns=self.cfg.limits.max_tool_rounds,
                    tool_call_cap_s=self.cfg.limits.tool_call_cap_s,
                    cwd=work,
                    mcp_url=server.url,
                    can_use_tool=make_can_use_tool(allowed_tools, denied),
                    stderr=session.on_stderr,
                    cli_path=cli_path,
                )
                try:
                    # `can_use_tool` needs streaming mode: the client, not `query()`.
                    async with ClaudeSDKClient(options=options) as client:
                        await session.wait_for_mcp(client, allowed_tools)
                        if session.fatal is None:
                            await client.query(user_prompt)
                            async for msg in client.receive_response():
                                if session.absorb(msg):
                                    break
                except Exception as e:  # noqa: BLE001 - the trial's error, not the harness's
                    session.exception = f"{type(e).__name__}: {e}"
                finally:
                    # Also when the wall cap cancels the trial: what was called stays recorded.
                    try:
                        records = server.records()
                    except Exception as e:  # noqa: BLE001
                        session.exception = session.exception or f"records: {e}"
                    self._publish(mcp_app, records, session)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return self._dispatch_result(session, records, denied, cli_path, time.perf_counter() - t0)

    @staticmethod
    def _publish(mcp_app: Any, records: list[CallRecord], session: _Session) -> list[CallRecord]:
        """The trace the runner and the oracles read: the server's MCP calls, then the
        built-in tools the model reached for (all denied, so a built-in is an attempt)."""
        calls = [*records, *session.builtin.values()]
        for i, rec in enumerate(calls):
            rec.index = i
        mcp_app.calls.extend(calls)
        return calls

    def _dispatch_result(
        self,
        session: _Session,
        records: list[CallRecord],
        denied: list[dict[str, Any]],
        cli_path: str | None,
        wall_s: float,
    ) -> TrialDispatch:
        result = session.result
        finish = "stop"
        error = session.fatal or session.exception
        if error is None and result is None:
            error = "the Claude Code session ended without a result"
        if error is None and result is not None:
            if result.subtype == "error_max_turns":
                finish = "max_rounds"
            elif result.is_error or result.subtype.startswith("error"):
                detail = result.result or "; ".join(result.errors or []) or "no detail"
                error = f"{result.subtype}: {detail}"
            else:
                finish = result.stop_reason or "stop"
        if error is not None:
            finish = "adapter_error"
            tail = " | ".join(line.strip() for line in session.stderr[-3:])[:300]
            extra = [
                f"assistant error {session.assistant_error!r}" if session.assistant_error else "",
                f"last stderr: {tail}" if tail else "",
            ]
            error = "; ".join([error, *[e for e in extra if e]])
        usage = (result.usage if result else None) or {}
        tokens_in = sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        note = None
        if session.mcp_blocks != len(records):
            note = (
                f"the SDK stream showed {session.mcp_blocks} MCP calls, "
                f"the server recorded {len(records)}"
            )
        answer = (result.result or "") if (result and error is None) else ""
        allowed_names = {n.removeprefix(TOOL_PREFIX) for n in session.expected_mcp_tools}
        calls = [*records, *session.builtin.values()]
        return TrialDispatch(
            final_answer=answer,
            tool_calls=tuple(
                ToolCall(
                    r.tool,
                    r.arguments,
                    r.ok,
                    r.error,
                    (r.result_text or "")[:TOOL_RESULT_CAP] or None,
                )
                for r in calls
            ),
            rounds_used=(result.num_turns if result and result.num_turns else None)
            or session.assistant_messages,
            finish_reason=finish,
            usage_in_tokens=tokens_in,
            usage_out_tokens=int(usage.get("output_tokens") or 0),
            wall_clock_seconds=wall_s,
            raw_trace={
                "backend": "claude_sdk",
                "billing": "subscription",
                "sdk_total_cost_usd": result.total_cost_usd if result else None,  # not charged
                "claude_cli": cli_path or "bundled with claude-agent-sdk",
                "init": session.init,
                "mcp_server": session.mcp_status,
                "extra_mcp_tools_exposed": sorted(
                    set(session.mcp_status.get("tools", ())) - allowed_names
                ),
                "unexpected_tools_offered": session.unexpected_tools(),
                "response_model": ",".join(session.models) or None,
                "request_ids": list(session.message_ids),
                "subtype": result.subtype if result else None,
                "stop_reason": result.stop_reason if result else None,
                "model_usage": result.model_usage if result else None,
                "permission_denials": result.permission_denials if result else None,
                "denied_calls": denied,
                "builtin_calls": len(session.builtin),
                "mcp_calls_from_sdk": session.mcp_blocks,
                "mcp_calls_from_server": len(records),
                "count_mismatch": note,
                "assistant_error": session.assistant_error,
                "rate_limit_events": session.rate_limits,
            },
            error=redact(error) if error else None,
        )


def make_claude_sdk_adapter(cfg: K2valConfig, conf: ModelConf, run_dir: Path) -> ClaudeSdkAdapter:
    return ClaudeSdkAdapter(cfg, conf, run_dir)
