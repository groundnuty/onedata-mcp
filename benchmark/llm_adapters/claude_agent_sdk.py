"""Claude leg via the official `claude-agent-sdk` Python package.

Uses the local Claude Code session for auth — NO `ANTHROPIC_API_KEY`
required. The SDK auto-bundles the `claude` CLI binary and rides on the
user's Pro/Max subscription. Verified 2026-05-01: a 1-line `query("hi")`
returns "4" with `ANTHROPIC_API_KEY=UNSET`.

## Architecture

The OpenAI-compat adapter dispatches each tool call through `mcp_app`
in-process. The Claude Agent SDK doesn't expose that hook — it spawns
its own MCP subprocess and runs its own agent loop. So this adapter:

1. Configures `ClaudeAgentOptions(mcp_servers={...})` to spawn `onedata-mcp`
   as a stdio child of the SDK's `claude` subprocess (passing the same
   federation env vars our in-process code reads).
2. Restricts the agent to ONLY the 15-tool `HEADLINE` allowlist via the
   `can_use_tool` callback — built-ins (`Bash`, `Read`, `Write`, etc.)
   are denied. This matches the OpenAI-compat path's tool-surface for
   comparison parity. The SDK's `mcp__<server>__<tool>` naming convention
   is what `allowed_tools` and `can_use_tool` see at runtime.
3. Iterates the SDK's async message stream, maps `ToolUseBlock` → our
   `ToolCall`, and pulls the final text from the `ResultMessage`.

## Comparison-parity caveat (paper-relevant)

The Claude Agent SDK exposes Claude Code's full built-in toolset
(Read/Write/Bash/Edit/Glob/Grep/WebSearch/etc.) on top of any custom
MCP tools. To compare cells with the OpenAI-compat leg fairly, the
benchmark must be confident the agent didn't fall back to `Bash`-curl
when its planned MCP tool wasn't in the allowlist. The `can_use_tool`
callback gives us the hard guarantee. Any denied call gets recorded
in the `raw_trace.denied_calls` field of the TrialDispatch — a paper-
grade observation about how harness-bundled tools shape agent reasoning.
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    PermissionResult,
    ToolPermissionContext,
    ToolResultBlock,
)

from benchmark._runtime_types import ToolCall
from benchmark.llm_adapters._protocol import LLMConfig, TrialDispatch

MCP_SERVER_NAME = "onedata"
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# Claude Code's built-in toolset (`claude --print --output-format stream-json`
# init message lists these). We disallow ALL of them for benchmark parity
# with the OpenAI-compat leg, which only ever sees the 15-tool HEADLINE.
#
# Defence-in-depth — empirically (live D1 smoke 2026-05-01) the
# `can_use_tool` callback alone did NOT block `ToolSearch`; denied_calls=[]
# despite ToolSearch appearing in the agent's trace. Hypothesis: some
# built-ins bypass the can_use_tool gate (auto-approved by SDK / Claude
# Code internals), so an explicit disallow list is needed alongside the
# callback for hard parity.
_BUILTIN_TOOLS = (
    "AskUserQuestion",
    "Bash",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Edit",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "Monitor",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "RemoteTrigger",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "Task",
    "TaskOutput",
    "TaskStop",
    "TeamCreate",
    "TeamDelete",
    "TodoWrite",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Write",
)

# Federation env vars the spawned onedata-mcp child needs. Forwarded from
# the harness env so the child connects to the same federation we're
# benchmarking against.
_FORWARDED_ENV_KEYS = (
    "ONEDATA_ONEZONE_HOST",
    "ONEDATA_ONEZONE_TOKEN",
    "ONEDATA_ONEPROVIDER_HOST",
    "ONEDATA_ONEPROVIDER_TOKEN",
    "ONEDATA_ALLOW_INSECURE_TLS",
    "FASTMCP_LOG_LEVEL",
)


def _forwarded_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _FORWARDED_ENV_KEYS if k in os.environ}


class ClaudeAgentSdkAdapter:
    """LLMAdapter implementation using `claude-agent-sdk` (Python).

    Uses the local Claude Code session for auth — no API key required.
    The agent's available tools are restricted via `can_use_tool` to the
    `HEADLINE` allowlist; denied calls (built-in fallbacks like `Bash`)
    are recorded in `TrialDispatch.raw_trace["denied_calls"]`.
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    async def dispatch(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        mcp_app,  # unused — the SDK runs its own MCP subprocess
        allowed_tools: frozenset[str],
    ) -> TrialDispatch:
        del mcp_app  # documented divergence: SDK manages its own MCP transport

        sdk_allowed = sorted(f"{TOOL_PREFIX}{name}" for name in allowed_tools)
        allowed_set = frozenset(sdk_allowed)
        denied_calls: list[dict[str, Any]] = []

        async def can_use_tool(
            tool_name: str,
            input: dict[str, Any],
            context: ToolPermissionContext,
        ) -> PermissionResult:
            del context
            if tool_name in allowed_set:
                return PermissionResultAllow(behavior="allow", updated_input=input)
            denied_calls.append({"tool_name": tool_name, "input": input})
            return PermissionResultDeny(
                behavior="deny",
                message=(
                    f"Tool {tool_name!r} is not in the benchmark allowlist; "
                    f"only {TOOL_PREFIX}* tools are available."
                ),
                interrupt=False,
            )

        cli_path = shutil.which("onedata-mcp")
        if cli_path is None:
            return TrialDispatch(
                final_answer="",
                tool_calls=(),
                rounds_used=0,
                finish_reason="adapter_error",
                error=(
                    "onedata-mcp binary not on PATH. Run "
                    "`uv pip install -e .` in the MCP fork or arrange for "
                    "the binary to be on PATH for the benchmark process."
                ),
            )

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt,
            allowed_tools=sdk_allowed,
            disallowed_tools=list(_BUILTIN_TOOLS),
            mcp_servers={
                MCP_SERVER_NAME: {
                    "type": "stdio",
                    "command": cli_path,
                    "args": [],
                    "env": _forwarded_env(),
                },
            },
            permission_mode="default",
            can_use_tool=can_use_tool,
            max_turns=self.config.max_tool_rounds,
            model=self.config.model_id or None,
            setting_sources=None,  # clean room: don't load user/project settings
        )

        captured_calls: list[ToolCall] = []
        pending: dict[str, int] = {}  # tool_use_id → captured_calls index
        final_text: str | None = None
        rounds = 0
        finish_reason: str | None = None
        usage_in = 0
        usage_out = 0
        error: str | None = None

        loop_t0 = time.perf_counter()
        # `can_use_tool` requires streaming mode → use ClaudeSDKClient, NOT the
        # standalone `query()` function. Per the SDK README: "Unlike query(),
        # ClaudeSDKClient additionally enables custom tools and hooks."
        try:
            async with ClaudeSDKClient(options=opts) as client:
                await client.query(user_prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        rounds += 1
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock):
                                idx = len(captured_calls)
                                captured_calls.append(
                                    ToolCall(
                                        tool_name=_strip_prefix(block.name),
                                        arguments=dict(block.input or {}),
                                        succeeded=True,  # may be flipped when result arrives
                                        error=None,
                                        result=None,
                                    )
                                )
                                pending[block.id] = idx
                            elif isinstance(block, TextBlock):
                                pass  # final answer comes from ResultMessage
                    elif isinstance(msg, UserMessage):
                        content = getattr(msg, "content", None)
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, ToolResultBlock):
                                    idx = pending.pop(block.tool_use_id, None)
                                    if idx is not None:
                                        captured_calls[idx] = _splice_result(
                                            captured_calls[idx], block
                                        )
                    elif isinstance(msg, ResultMessage):
                        final_text = getattr(msg, "result", None)
                        finish_reason = getattr(msg, "subtype", None) or "result"
                        usage = getattr(msg, "usage", None) or {}
                        if isinstance(usage, dict):
                            usage_in = usage.get("input_tokens", 0) or 0
                            usage_out = usage.get("output_tokens", 0) or 0
                    elif isinstance(msg, SystemMessage):
                        pass  # init / hook events
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"

        return TrialDispatch(
            final_answer=final_text or "",
            tool_calls=tuple(captured_calls),
            rounds_used=rounds,
            finish_reason=finish_reason,
            usage_in_tokens=usage_in,
            usage_out_tokens=usage_out,
            wall_clock_seconds=time.perf_counter() - loop_t0,
            raw_trace={"denied_calls": denied_calls},
            error=error,
        )


def _strip_prefix(name: str) -> str:
    return name[len(TOOL_PREFIX) :] if name.startswith(TOOL_PREFIX) else name


def _splice_result(call: ToolCall, block: ToolResultBlock) -> ToolCall:
    """Return a new ToolCall with `result` (and `succeeded`/`error`) filled
    from the SDK's ToolResultBlock. ToolCall is frozen; build a new instance."""
    is_error = bool(getattr(block, "is_error", False))
    content = getattr(block, "content", None)
    text = _content_to_text(content)
    return ToolCall(
        tool_name=call.tool_name,
        arguments=call.arguments,
        succeeded=not is_error,
        error=text if is_error else None,
        result=text if not is_error else None,
    )


def _content_to_text(content: Any) -> str:
    """Best-effort conversion of an SDK content payload to a string.

    Content can be a string, a list of dicts ([{"type":"text","text":"..."}]),
    or a list of block objects.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
            else:
                t = getattr(item, "text", None)
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return repr(content)
