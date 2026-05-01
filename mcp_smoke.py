#!/usr/bin/env python3
"""Single-shot live-federation smoke for the Onedata MCP server.

Exercises every callable tool once against the configured federation
(`.env` next to this script). Prints `PASS`/`FAIL` per tool with timing
and a one-line reason. The exit code is 0 iff every non-skipped tool
passes.

Default mode is `--dry-run` (read-only, safe). Pass `--write` to also
exercise create/delete/QoS-add/QoS-remove inside a per-run scratch dir
under the configured benchmark space (NOT YET WIRED — the smoke refuses
to write until --benchmark-space is supplied).

Tools that are intentionally non-functional on the SPICE 25.0 deployment
are listed in EXPECTED_SKIPS with a reason and reported `SKIP`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from dotenv import load_dotenv
from fastmcp.exceptions import ToolError

# Load .env BEFORE importing onedata_mcp.main, since main.py builds the
# FastMCP server eagerly and config.get_*_config() reads env at call-time.
load_dotenv()

from onedata_mcp.main import mcp  # noqa: E402

# Tools that won't work on the current SPICE 25.0 federation; smoke
# reports SKIP rather than FAIL for these.
EXPECTED_SKIPS: dict[str, str] = {
    "move_file": "no public REST endpoint in 25.0 — see design/01-move-file-strategy.md",
    "list_user_harvesters": "no harvester configured on data.spice-platform.eu",
    "get_harvester_index_schema": "no harvester configured on data.spice-platform.eu",
    "query_harvester_index": "no harvester configured on data.spice-platform.eu",
}

# Write tools exercised only with --write.
WRITE_TOOLS: set[str] = {
    "create_file",
    "set_file_metadata",
    "delete_file",
    "add_file_qos_requirement",
    "remove_qos_requirement",
}


class SmokeContext:
    """Federation state discovered during the smoke."""

    space_id: str | None = None
    space_name: str | None = None
    root_file_id: str | None = None
    sample_reg_file_id: str | None = None
    sample_reg_file_path: str | None = None


async def _call(name: str, args: dict[str, Any]) -> tuple[bool, str, float]:
    """Invoke an MCP tool. Returns (ok, message, elapsed_s)."""
    t0 = time.perf_counter()
    try:
        result = await mcp.call_tool(name, args)
        elapsed = time.perf_counter() - t0
        # Compact summary of the result so the smoke output is readable.
        summary = _summarise(result)
        return True, summary, elapsed
    except ToolError as e:
        elapsed = time.perf_counter() - t0
        return False, f"ToolError: {e}", elapsed
    except Exception as e:  # noqa: BLE001
        elapsed = time.perf_counter() - t0
        return False, f"{type(e).__name__}: {e}", elapsed


def _summarise(result: Any) -> str:
    """One-line, length-bounded summary of a FastMCP call_tool() result."""
    text = repr(result)
    if len(text) > 120:
        text = text[:117] + "..."
    return text


def _line(status: str, name: str, elapsed: float, msg: str) -> str:
    return f"{status:5s}  {elapsed * 1000:7.1f}ms  {name:30s}  {msg}"


async def run_smoke(write_mode: bool, benchmark_space: str | None) -> int:
    print(f"# Onedata MCP smoke (mode: {'WRITE' if write_mode else 'DRY-RUN'})")
    print()

    ctx = SmokeContext()
    failures: list[str] = []
    skipped: list[str] = []
    passed: list[str] = []

    async def step(name: str, args: dict[str, Any]) -> tuple[bool, str]:
        if name in EXPECTED_SKIPS:
            skipped.append(name)
            print(_line("SKIP", name, 0.0, EXPECTED_SKIPS[name]))
            return False, ""
        if name in WRITE_TOOLS and not write_mode:
            skipped.append(name)
            print(_line("SKIP", name, 0.0, "write tool, --dry-run"))
            return False, ""
        ok, msg, elapsed = await _call(name, args)
        if ok:
            passed.append(name)
            print(_line("PASS", name, elapsed, msg))
        else:
            failures.append(name)
            print(_line("FAIL", name, elapsed, msg))
        return ok, msg

    # ---- Discovery phase: find a space we can use ----
    ok, _ = await step("list_user_spaces", {})
    if ok:
        try:
            spaces_result = await mcp.call_tool("list_user_spaces", {})
            # FastMCP wraps tool results; extract the structured payload.
            payload = _extract_payload(spaces_result)
            if isinstance(payload, list) and payload:
                first = payload[0]
                if isinstance(first, dict):
                    ctx.space_id = first.get("spaceId")
                    ctx.space_name = first.get("name")
        except Exception:
            pass

    if ctx.space_id is None:
        print()
        print("# No space available — discovery aborted, remaining tools skipped.")
        _print_summary(passed, failures, skipped)
        return 1 if failures else 2

    print(f"# Selected space: {ctx.space_name!r}  ({ctx.space_id})")
    print()

    # ---- Onezone marketplace listing (no-state-required) ----
    await step("list_marketplace_spaces", {"limit": 5})

    # ---- Oneprovider space-level reads ----
    await step("list_space_providers", {"space_id": ctx.space_id})
    await step("list_space_transfers", {"space_id": ctx.space_id, "limit": 5})

    # ---- Resolve root path → fileId ----
    if ctx.space_name:
        ok, msg = await step("get_file_id", {"path": f"/{ctx.space_name}"})
        if ok:
            try:
                result = await mcp.call_tool("get_file_id", {"path": f"/{ctx.space_name}"})
                payload = _extract_payload(result)
                if isinstance(payload, str):
                    ctx.root_file_id = payload
            except Exception:
                pass

    if ctx.root_file_id is None:
        print()
        print("# Root file id unresolved — file-level tools skipped.")
        _print_summary(passed, failures, skipped)
        return 1 if failures else 0

    # ---- Per-file (root dir) reads ----
    await step("get_file_attributes", {"file_id_or_path": ctx.root_file_id})
    await step(
        "list_children",
        {"parent_id_or_path": ctx.root_file_id, "limit": 5},
    )
    await step(
        "list_files_recursively",
        {"parent_id_or_path": ctx.root_file_id, "limit": 5},
    )
    await step("get_file_distribution", {"file_id_or_path": ctx.root_file_id})
    await step("get_file_metadata", {"file_id_or_path": ctx.root_file_id})
    await step("get_file_qos_summary", {"file_id_or_path": ctx.root_file_id})

    # ---- Pick a small regular file (if any) for content reads ----
    try:
        files_result = await mcp.call_tool(
            "list_files_recursively",
            {"parent_id_or_path": ctx.root_file_id, "limit": 20},
        )
        payload = _extract_payload(files_result)
        if isinstance(payload, dict):
            for entry in payload.get("files", []):
                if (
                    isinstance(entry, dict)
                    and entry.get("type") == "REG"
                    and isinstance(entry.get("size"), int)
                    and entry["size"] < 1024 * 1024  # < 1MB
                    and entry.get("fileId")
                ):
                    ctx.sample_reg_file_id = entry["fileId"]
                    ctx.sample_reg_file_path = entry.get("path")
                    break
    except Exception:
        pass

    if ctx.sample_reg_file_id:
        print(f"# Sample regular file: {ctx.sample_reg_file_path}")
        await step("download_file", {"file_id_or_path": ctx.sample_reg_file_id})
        await step(
            "grep_file_content",
            {"file_id_or_path": ctx.sample_reg_file_id, "pattern": "."},
        )
    else:
        skipped.append("download_file")
        skipped.append("grep_file_content")
        print(_line("SKIP", "download_file", 0.0, "no small REG file in root"))
        print(_line("SKIP", "grep_file_content", 0.0, "no small REG file in root"))

    # ---- query_by_metadata (recursive, no harvester) ----
    if ctx.space_name:
        await step(
            "query_by_metadata",
            {
                "space": ctx.space_name,
                "predicate": "_smoke_probe=*",
                "max_depth": 1,
                "max_results": 1,
            },
        )

    # ---- Harvester tools (will SKIP per EXPECTED_SKIPS) ----
    await step("list_user_harvesters", {})
    await step(
        "get_harvester_index_schema",
        {"harvester_id": "n/a", "index_id": "n/a"},
    )
    await step(
        "query_harvester_index",
        {"harvester_id": "n/a", "index_id": "n/a", "query": {}},
    )

    # ---- Stub: move_file (will SKIP per EXPECTED_SKIPS) ----
    await step(
        "move_file",
        {"src_file_id_or_path": "/x", "dst_path": "/y"},
    )

    # ---- Write phase (only if --write AND --benchmark-space) ----
    if write_mode and benchmark_space:
        print()
        print("# WRITE phase — not yet implemented; awaiting first benchmark fixture.")
        # Will land in a follow-up commit alongside scenario authoring (task #20).

    print()
    _print_summary(passed, failures, skipped)
    return 1 if failures else 0


def _extract_payload(result: Any) -> Any:
    """Best-effort extraction of the structured payload from a FastMCP
    call_tool() result.

    FastMCP wraps a tool's return value differently across versions; this
    function handles the common shapes we see at runtime.
    """
    # Direct return (some versions return the value as-is)
    if not hasattr(result, "content") and not hasattr(result, "structured_content"):
        return result
    # Newer FastMCP exposes structured_content
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        # Often wrapped in {"result": <payload>}
        if isinstance(structured, dict) and "result" in structured and len(structured) == 1:
            return structured["result"]
        return structured
    # Fallback: try .content[0].text and parse if JSON
    content = getattr(result, "content", None)
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if isinstance(text, str):
            try:
                import json

                return json.loads(text)
            except (ValueError, TypeError):
                return text
    return result


def _print_summary(passed: list[str], failures: list[str], skipped: list[str]) -> None:
    print(f"# Summary: {len(passed)} PASS, {len(failures)} FAIL, {len(skipped)} SKIP")
    if failures:
        print(f"#   FAIL: {', '.join(failures)}")
    if skipped:
        print(f"#   SKIP: {', '.join(skipped)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live-federation smoke for the Onedata MCP server."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Enable the write phase (create/set/delete/qos). Requires --benchmark-space.",
    )
    parser.add_argument(
        "--benchmark-space",
        type=str,
        default=None,
        help="Space name designated for benchmark scratch writes. Required with --write.",
    )
    args = parser.parse_args()

    if args.write and not args.benchmark_space:
        print(
            "ERROR: --write requires --benchmark-space (refusing to write into an "
            "unconfirmed space)",
            file=sys.stderr,
        )
        sys.exit(2)

    rc = asyncio.run(run_smoke(write_mode=args.write, benchmark_space=args.benchmark_space))
    sys.exit(rc)


if __name__ == "__main__":
    main()
