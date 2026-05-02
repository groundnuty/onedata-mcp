"""Discovery-band oracles (D1..D6). All format-tier per paper Table 3.

Format-tier oracles return federation_pass=None (no separate federation
post-state check; the answer IS the deliverable). See design/06.
"""

from __future__ import annotations

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext
from benchmark.oracles._helpers import (
    contains_token,
    extract_int,
    extract_kv_lines,
    extract_paths,
)
from onedata_mcp.api import spaces as spaces_api

SPACE = "ppam_2026_mcp_tests"
SPACE_PATH_PREFIX = f"/{SPACE}/"


async def verify_d1(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    """D1: List spaces with provider counts.

    Ground truth = ctx.spaces_snapshot — the spaces visible at
    fixture-prepare time. Re-querying at oracle time would be
    flaky: the federation can churn (entries appear/disappear)
    between the agent's call and the oracle's. See
    research/empirical-findings #18.
    """
    spaces = ctx.spaces_snapshot or tuple(await spaces_api.list_user_spaces())
    answer = trace.final_answer
    for s in spaces:
        name = s.get("name")
        if not isinstance(name, str):
            continue
        provider_count = (
            len(s.get("providers") or {})
            if isinstance(s.get("providers"), dict)
            else len(s.get("providers") or [])
        )
        if not contains_token(answer, name):
            return OracleResult.format_only(
                mcp_pass=False, diagnosis=f"answer omits space {name!r}"
            )
        # Try plain key first, then backticked. NOT `a or b` — that
        # short-circuits when extract_int returns 0 (falsy), mis-FAILing
        # every space with provider_count=0 (e.g. orphaned spaces like
        # 'TestData'). Surfaced 2026-05-02 by 3-LLM K=1 slate where the
        # diagnosis "expected 0, got None" was a defaulting bug.
        count_int = extract_int(answer, name)
        if count_int is None:
            count_int = extract_int(answer, f"`{name}`")
        if count_int is None or count_int != provider_count:
            return OracleResult.format_only(
                mcp_pass=False,
                diagnosis=(
                    f"space {name!r}: expected provider_count={provider_count}, got {count_int!r}"
                ),
            )
    return OracleResult.format_only(mcp_pass=True)


async def verify_d2(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    """D2: Find files whose path begins with /<space>/d2/datasets/.

    Ground truth = the fixture spec (paths we materialised in Phase 2).
    """
    expected = {p for p in ctx.fixture_paths if p.startswith(f"{SPACE_PATH_PREFIX}d2/datasets/")}
    reported = extract_paths(trace.final_answer, anchor=SPACE_PATH_PREFIX)
    if reported == expected:
        return OracleResult.format_only(mcp_pass=True)
    return OracleResult.format_only(
        mcp_pass=False,
        diagnosis=(
            f"path-set mismatch: missing={sorted(expected - reported)} "
            f"unexpected={sorted(reported - expected)}"
        ),
    )


async def verify_d3(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    """D3: Read manifest, report 'size=N; head=<chars>'.

    Ground truth = the fixture content (we put it there in Phase 2). We
    re-derive expected size from the spec, not by reading the federation,
    so an Onedata read-failure mid-trial doesn't corrupt the oracle.
    """
    manifest_path = f"{SPACE_PATH_PREFIX}d3/manifest.txt"
    file_id = ctx.fixture_paths.get(manifest_path)
    if not file_id:
        return OracleResult.format_only(
            mcp_pass=False, diagnosis="fixture path missing from RunContext"
        )

    # Spec ground truth: from benchmark.scenarios D3 fixture content.
    from benchmark.scenarios import D3 as D3_SCENARIO

    expected_content = D3_SCENARIO.fixture.files[0].content
    expected_size = len(expected_content.encode("utf-8"))
    expected_head = expected_content[:50]

    size = extract_int(trace.final_answer, "size")
    if size != expected_size:
        return OracleResult.format_only(
            mcp_pass=False,
            diagnosis=f"size mismatch: expected {expected_size}, got {size}",
        )
    if not contains_token(trace.final_answer, expected_head):
        return OracleResult.format_only(
            mcp_pass=False, diagnosis="head substring not present in answer"
        )
    return OracleResult.format_only(mcp_pass=True)


async def verify_d4(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    """D4: List providers — must contain both bound provider names."""
    answer = trace.final_answer
    have_pl = contains_token(answer, "cloud-pl")
    have_sk = contains_token(answer, "Cloud-SK")
    if have_pl and have_sk:
        return OracleResult.format_only(mcp_pass=True)
    missing = [n for n, present in (("cloud-pl", have_pl), ("Cloud-SK", have_sk)) if not present]
    return OracleResult.format_only(
        mcp_pass=False, diagnosis=f"answer missing provider names: {missing}"
    )


async def verify_d5(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    """D5: Get JSON metadata — agent's k:v lines must match fixture metadata.

    Ground truth = the fixture spec (the metadata we set in Phase 2).
    """
    from benchmark.scenarios import D5 as D5_SCENARIO

    expected = D5_SCENARIO.fixture.files[0].json_metadata or {}
    parsed = extract_kv_lines(trace.final_answer)
    for k, v in expected.items():
        if parsed.get(k, "") != str(v):
            return OracleResult.format_only(
                mcp_pass=False,
                diagnosis=f"k:v mismatch for {k!r}: expected {v!r}, got {parsed.get(k)!r}",
            )
    return OracleResult.format_only(mcp_pass=True)


async def verify_d6(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    """D6: Trivial sanity — benchmark space must appear in the listing."""
    if contains_token(trace.final_answer, SPACE):
        return OracleResult.format_only(mcp_pass=True)
    return OracleResult.format_only(mcp_pass=False, diagnosis=f"space name {SPACE!r} not in answer")
