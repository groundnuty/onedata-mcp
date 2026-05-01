"""Multi-step Access oracles (A1..A6).

Tier mix per paper Table 3: A1, A6 format; A2..A5 static.

Static oracles return both mcp_pass (tool-call inspection) and
federation_pass (post-state inspection). See design/06: the agent gets
credit for issuing the right MCP call sequence even if Onedata's
post-state diverges (dbsync lag, transient errors, etc).
"""

from __future__ import annotations

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext
from benchmark.oracles._helpers import (
    extract_int,
    extract_paths,
    find_calls,
    has_successful_call,
)
from onedata_mcp.api import files as files_api
from onedata_mcp.api import qos as qos_api
from onedata_mcp.utils import OnedataApiError

SPACE = "ppam_2026_mcp_tests"
SPACE_PATH_PREFIX = f"/{SPACE}/"


# ---------------------------------------------------------------------------
# A1 (format): tag every raw file → 'tagged=N'
# ---------------------------------------------------------------------------


async def verify_a1(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    expected_n = sum(1 for p in ctx.fixture_paths if p.startswith(f"{SPACE_PATH_PREFIX}a1/raw/"))
    got = extract_int(trace.final_answer, "tagged")
    if got == expected_n:
        return OracleResult.format_only(mcp_pass=True)
    return OracleResult.format_only(
        mcp_pass=False, diagnosis=f"tagged count: expected {expected_n}, got {got}"
    )


# ---------------------------------------------------------------------------
# A2 (static): set reviewed=false on every file under /a2/inbox/
# ---------------------------------------------------------------------------


async def verify_a2(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    targets = [p for p in ctx.fixture_paths if p.startswith(f"{SPACE_PATH_PREFIX}a2/inbox/")]
    if not targets:
        return OracleResult(False, False, diagnosis="no fixture files under /a2/inbox/")

    # mcp_pass: agent called set_file_metadata on each fixture file with
    # metadata containing reviewed=false.
    mcp_pass = True
    mcp_diag: list[str] = []
    for path in targets:
        file_id = ctx.fixture_paths[path]
        if not _has_metadata_set_with(trace, path, file_id, key="reviewed", value=False):
            mcp_pass = False
            mcp_diag.append(path)

    # federation_pass: every fixture file actually has reviewed=false in JSON.
    federation_pass = True
    fed_diag: list[str] = []
    for path in targets:
        file_id = ctx.fixture_paths[path]
        try:
            got = await files_api.get_file_metadata(file_id, ["json"])
        except OnedataApiError:
            federation_pass = False
            fed_diag.append(f"{path} read-fail")
            continue
        meta = got.get("json")
        if not isinstance(meta, dict) or meta.get("reviewed") is not False:
            federation_pass = False
            fed_diag.append(f"{path} meta={meta!r}")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# A3 (static): rename + re-tag
# ---------------------------------------------------------------------------


async def verify_a3(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    draft_path = f"{SPACE_PATH_PREFIX}a3/staging/draft.txt"
    pub_path = f"{SPACE_PATH_PREFIX}a3/staging/published.txt"

    # mcp_pass: agent called move_file from draft to published, then
    # set_file_metadata on published with status=published.
    moved = bool(
        find_calls(
            trace,
            "move_file",
            lambda a: a.get("src_path") == draft_path and a.get("dst_path") == pub_path,
        )
    )
    tagged = _has_metadata_set_with(
        trace, pub_path, ctx.fixture_paths.get(pub_path) or "", key="status", value="published"
    )
    mcp_pass = moved and tagged

    # federation_pass: draft gone, published exists with metadata.
    federation_pass = True
    fed_diag: list[str] = []
    try:
        await files_api.get_file_id(draft_path)
        federation_pass = False
        fed_diag.append("draft.txt still exists")
    except FileNotFoundError:
        pass
    except OnedataApiError as e:
        federation_pass = False
        fed_diag.append(f"draft.txt check error: {e}")
    try:
        pub_id = await files_api.get_file_id(pub_path)
        got = await files_api.get_file_metadata(pub_id, ["json"])
        meta = got.get("json") or {}
        if not isinstance(meta, dict) or meta.get("status") != "published":
            federation_pass = False
            fed_diag.append(f"published.txt metadata={meta!r}")
    except (FileNotFoundError, OnedataApiError) as e:
        federation_pass = False
        fed_diag.append(f"published.txt check error: {e}")

    mcp_diag = []
    if not moved:
        mcp_diag.append("no move_file(draft→published)")
    if not tagged:
        mcp_diag.append("no set_file_metadata(published, status=published)")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# A4 (static): cross-directory copy preserving metadata
# ---------------------------------------------------------------------------


async def verify_a4(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    src_path = f"{SPACE_PATH_PREFIX}a4/source/data.csv"
    dst_path = f"{SPACE_PATH_PREFIX}a4/archive/data.csv"

    # mcp_pass: agent download_file(src) + create_file(dst) + set_file_metadata(dst).
    downloaded = has_successful_call(
        trace,
        "download_file",
        lambda a: a.get("file_id_or_path") in (src_path, ctx.fixture_paths.get(src_path)),
    )
    created = has_successful_call(trace, "create_file", lambda a: a.get("path") == dst_path)
    # metadata propagation: agent should have called set_file_metadata on the dst
    # OR copied metadata via get_file_metadata(src) + set_file_metadata(dst).
    set_meta_dst = bool(
        find_calls(
            trace,
            "set_file_metadata",
            lambda a: a.get("file_id_or_path") == dst_path or a.get("file_id_or_path") == "",
        )
    )
    mcp_pass = downloaded and created and set_meta_dst

    # federation_pass: src + content + meta intact; dst exists with same content + meta.
    src_id = ctx.fixture_paths.get(src_path)
    federation_pass = True
    fed_diag: list[str] = []
    if not src_id:
        federation_pass = False
        fed_diag.append("src missing from RunContext")
    else:
        try:
            src_content = await files_api.download_file(src_id)
            src_meta = (await files_api.get_file_metadata(src_id, ["json"])).get("json") or {}
            dst_id = await files_api.get_file_id(dst_path)
            dst_content = await files_api.download_file(dst_id)
            if dst_content != src_content:
                federation_pass = False
                fed_diag.append("dst content != src content")
            dst_meta = (await files_api.get_file_metadata(dst_id, ["json"])).get("json") or {}
            if not isinstance(dst_meta, dict) or not isinstance(src_meta, dict):
                federation_pass = False
                fed_diag.append("metadata not dict-shaped")
            else:
                for k, v in src_meta.items():
                    if dst_meta.get(k) != v:
                        federation_pass = False
                        fed_diag.append(f"meta {k}: src={v!r} dst={dst_meta.get(k)!r}")
        except (FileNotFoundError, OnedataApiError) as e:
            federation_pass = False
            fed_diag.append(f"federation check error: {e}")

    mcp_diag = []
    if not downloaded:
        mcp_diag.append("no successful download_file(src)")
    if not created:
        mcp_diag.append("no successful create_file(dst)")
    if not set_meta_dst:
        mcp_diag.append("no set_file_metadata(dst)")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# A5 (static): create + add EU QoS rule, replicas ≥ 2
# ---------------------------------------------------------------------------


async def verify_a5(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    target_path = f"{SPACE_PATH_PREFIX}a5/important/checkpoint.bin"
    eu_tokens = ("country=PL", "country=SK", "country=AT", "country=DE", "geo=EU")

    # mcp_pass: agent created the file at target_path AND added a QoS req
    # with EU expression and replicas_num ≥ 2.
    created = has_successful_call(trace, "create_file", lambda a: a.get("path") == target_path)

    def _eu_2plus(args: dict) -> bool:
        expr = args.get("expression", "") or ""
        rep = args.get("replicas_num", 0) or 0
        return any(tok in expr for tok in eu_tokens) and rep >= 2

    eu_qos_call = bool(find_calls(trace, "add_file_qos_requirement", _eu_2plus))
    mcp_pass = created and eu_qos_call

    # federation_pass: file exists, has at least one QoS req with EU + replicas≥2.
    federation_pass = True
    fed_diag: list[str] = []
    try:
        file_id = await files_api.get_file_id(target_path)
        summary = await qos_api.get_file_qos_summary(file_id)
        requirements = summary.get("requirements") or {}
        if not requirements:
            federation_pass = False
            fed_diag.append("no QoS requirements attached")
        else:
            ok = False
            for qos_id in requirements:
                try:
                    detail = await qos_api.get_qos_requirement(qos_id)
                except OnedataApiError:
                    continue
                # Onedata 25.0 returns `expression`, not `qosExpression`.
                # Empirical-findings #15, 2026-05-01.
                expr = detail.get("expression") or detail.get("qosExpression") or ""
                rep = detail.get("replicasNum") or 0
                if rep >= 2 and any(tok in expr for tok in eu_tokens):
                    ok = True
                    break
            if not ok:
                federation_pass = False
                fed_diag.append("no QoS req matches EU + replicas≥2")
    except (FileNotFoundError, OnedataApiError) as e:
        federation_pass = False
        fed_diag.append(f"federation check error: {e}")

    mcp_diag = []
    if not created:
        mcp_diag.append("no successful create_file(checkpoint.bin)")
    if not eu_qos_call:
        mcp_diag.append("no add_file_qos_requirement with EU + replicas≥2")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# A6 (format): query_by_metadata pipeline_stage=raw
# ---------------------------------------------------------------------------


async def verify_a6(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    expected = {
        f"{SPACE_PATH_PREFIX}a6/batch01/f1.txt",
        f"{SPACE_PATH_PREFIX}a6/batch01/f2.txt",
    }
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_metadata_set_with(
    trace: AgentTrace, path: str, file_id: str, *, key: str, value: object
) -> bool:
    """True if agent called set_file_metadata on this file (path or id) with
    JSON metadata containing key=value."""

    def _matches(args: dict) -> bool:
        if args.get("file_id_or_path") not in (path, file_id, "" if not file_id else None):
            return False
        if args.get("metadata_type") not in (None, "json"):
            return False
        meta_str = args.get("metadata", "") or ""
        # The MCP arg is a string; agent serialised JSON. Accept if the
        # k=v pair is present in the serialised form.
        marker = _serialised_marker(key, value)
        return marker in meta_str

    return has_successful_call(trace, "set_file_metadata", _matches)


def _serialised_marker(key: str, value: object) -> str:
    """Substring that the JSON-serialised metadata MUST contain."""
    if isinstance(value, bool):
        return f'"{key}": {"true" if value else "false"}'
    if isinstance(value, str):
        return f'"{key}": "{value}"'
    return f'"{key}": {value!r}'


def _render_diag(
    mcp_pass: bool,
    mcp_diag: list[str],
    federation_pass: bool,
    fed_diag: list[str],
) -> str:
    parts: list[str] = []
    if mcp_pass and federation_pass:
        return ""
    if not mcp_pass:
        parts.append(f"mcp_pass=False ({'; '.join(mcp_diag) or 'no detail'})")
    if not federation_pass:
        parts.append(f"federation_pass=False ({'; '.join(fed_diag) or 'no detail'})")
    if mcp_pass and not federation_pass:
        parts.insert(0, "MCP succeeded but federation diverged (likely Onedata-side):")
    return " ".join(parts)
