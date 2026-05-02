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
    expected = {p for p in ctx.fixture_paths if p.startswith(f"/{ctx.space_name}/d2/datasets/")}
    reported = extract_paths(trace.final_answer, anchor=f"/{ctx.space_name}/")
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
    manifest_path = f"/{ctx.space_name}/d3/manifest.txt"
    file_id = ctx.fixture_paths.get(manifest_path)
    if not file_id:
        return OracleResult.format_only(
            mcp_pass=False, diagnosis="fixture path missing from RunContext"
        )

    # Spec ground truth: from benchmark.scenarios D3 fixture content.
    from benchmark.scenarios import D3 as D3_SCENARIO

    expected_content = D3_SCENARIO.fixture.files[0].content
    expected_size = len(expected_content.encode("utf-8"))
    # Head check uses the first 30 source-content chars (covers ~2 lines of
    # the manifest fixture). Shorter than the full file, deliberately:
    # agents typically render newlines as the 2-char escape sequence `\\n`
    # rather than real LF, then truncate to N visible chars. With a 50-char
    # expected_head, every newline shifts the agent's emission boundary by
    # 1 char relative to the source — at 2 newlines in the first 50 chars
    # of D3, the agent's normalized answer can fall 2 chars short of the
    # expected head boundary even when the agent read the file correctly.
    # 30 chars covers MANIFEST v1\nbuild=46-g14b5bda7 — distinctive enough
    # to verify the agent actually read content, robust to encoding/
    # truncation slop.
    expected_head = expected_content[:30]

    size = extract_int(trace.final_answer, "size")
    if size != expected_size:
        return OracleResult.format_only(
            mcp_pass=False,
            diagnosis=f"size mismatch: expected {expected_size}, got {size}",
        )
    # Normalise newlines + escaped-newline sequences before substring match.
    # Agents commonly render newlines as the literal two-character sequence
    # `\n` (backslash + n) when echoing file content into prose; the fixture
    # content has actual LF bytes. Both forms are equally valid renderings
    # of the head — collapse whitespace + escape variants to a single space
    # on both sides before comparing.
    answer_norm = _normalise_whitespace(trace.final_answer)
    expected_norm = _normalise_whitespace(expected_head)
    if expected_norm.lower() not in answer_norm.lower():
        return OracleResult.format_only(
            mcp_pass=False, diagnosis="head substring not present in answer"
        )
    return OracleResult.format_only(mcp_pass=True)


def _normalise_whitespace(s: str) -> str:
    """Treat literal '\\n', '\\r', '\\t' (escape-sequence renderings) and
    real LF / CR / TAB bytes as a single space. Lets the substring match be
    robust to agents that print escape sequences vs raw control bytes.
    """
    out = s
    for esc in ("\\n", "\\r", "\\t"):
        out = out.replace(esc, " ")
    for ws in ("\n", "\r", "\t"):
        out = out.replace(ws, " ")
    while "  " in out:
        out = out.replace("  ", " ")
    return out


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
