"""Placement-introspection oracles (P1..P6).

Tier mix per paper Table 3: P1, P2, P5, P6 static; P3, P4 dynamic.

All return OracleResult with both axes (mcp_pass + federation_pass) per
design/06.
"""

from __future__ import annotations

import time

from benchmark._runtime_types import AgentTrace, OracleResult, RunContext
from benchmark.oracles._helpers import (
    contains_token,
    extract_paths,
    find_calls,
    has_successful_call,
)
from onedata_mcp.api import files as files_api
from onedata_mcp.api import qos as qos_api
from onedata_mcp.api import transfers as transfers_api
from onedata_mcp.utils import OnedataApiError

SPACE = "ppam_2026_mcp_tests"
DYNAMIC_DEADLINE_SECONDS = 60.0
DYNAMIC_POLL_INTERVAL = 5.0
# EU placement signals — accept paper-canonical user-attribute tokens
# (kept for forward compat if Cyfronet later configures user QoS attrs)
# AND providerId-based fallbacks because SPICE providers don't expose
# user QoS attrs (research/empirical-findings #14).
from benchmark._federation_constants import (  # noqa: E402
    PROVIDER_ID_CLOUD_PL,
    PROVIDER_ID_CLOUD_SK,
)

EU_TOKENS = (
    "country=PL",
    "country=SK",
    "country=AT",
    "country=DE",
    "geo=EU",
    PROVIDER_ID_CLOUD_PL,
    PROVIDER_ID_CLOUD_SK,
)
PL_TOKENS = ("country=PL", PROVIDER_ID_CLOUD_PL)
SK_TOKENS = ("country=SK", PROVIDER_ID_CLOUD_SK)


# ---------------------------------------------------------------------------
# P1: Where stored, fully replicated? (static)
# ---------------------------------------------------------------------------


async def verify_p1(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    target_path = f"/{ctx.space_name}/p1/model_v1.pt"
    file_id = ctx.fixture_paths.get(target_path)
    if not file_id:
        return OracleResult(False, False, diagnosis="fixture path missing")

    # mcp_pass: agent called get_file_distribution on the target file.
    called_dist = has_successful_call(
        trace,
        "get_file_distribution",
        lambda a: a.get("file_id_or_path") in (target_path, file_id),
    )
    # Agent must also report cloud-pl AND Cloud-SK as fully-replicated EU.
    answer = trace.final_answer
    answer_ok = contains_token(answer, "cloud-pl") and contains_token(answer, "Cloud-SK")
    mcp_pass = called_dist and answer_ok

    # federation_pass: both providers actually fully replicated.
    federation_pass = True
    fed_diag: list[str] = []
    try:
        dist = await files_api.get_file_distribution(file_id)
        per_provider = dist.get("distributionPerProvider") or {}
        fully = set()
        for pid, entry in per_provider.items():
            if isinstance(entry, dict) and entry.get("success"):
                # Onedata 25.0 returns `virtualSize`; older docs sometimes
                # show `logicalSize`. Empirical-findings #14, 2026-05-01.
                logical = entry.get("virtualSize") or entry.get("logicalSize") or 0
                physical = sum(
                    sb.get("physicalSize") or 0
                    for sb in (entry.get("distributionPerStorageBackend") or {}).values()
                    if isinstance(sb, dict) and sb.get("success")
                )
                if logical > 0 and physical >= logical:
                    fully.add(pid)
        if len(fully) < 2:
            federation_pass = False
            fed_diag.append(f"only {len(fully)} provider(s) fully replicated")
    except OnedataApiError as e:
        federation_pass = False
        fed_diag.append(f"distribution read error: {e}")

    mcp_diag = []
    if not called_dist:
        mcp_diag.append("no successful get_file_distribution call")
    if not answer_ok:
        mcp_diag.append("answer missing one or both provider names")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# P2: QoS-violation diagnostic (static)
# ---------------------------------------------------------------------------


async def verify_p2(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    violator_path = f"/{ctx.space_name}/p2/critical/violator.bin"
    file_id = ctx.fixture_paths.get(violator_path)
    if not file_id:
        return OracleResult(False, False, diagnosis="fixture path missing")

    # mcp_pass: agent called get_file_qos_summary on the violator (or any
    # /p2/critical/ file), AND the answer names violator.bin + 'impossible'.
    called_qos = bool(
        find_calls(
            trace,
            "get_file_qos_summary",
            lambda a: a.get("file_id_or_path") in (violator_path, file_id),
        )
    )
    answer = trace.final_answer
    answer_ok = contains_token(answer, "violator.bin") and contains_token(answer, "impossible")
    mcp_pass = called_qos and answer_ok

    # federation_pass: violator.bin's QoS rule is actually 'impossible'.
    federation_pass = True
    fed_diag: list[str] = []
    try:
        summary = await qos_api.get_file_qos_summary(file_id)
        requirements = summary.get("requirements") or {}
        if not any(s == "impossible" for s in requirements.values()):
            federation_pass = False
            fed_diag.append(
                f"no rule in 'impossible' state; statuses={list(requirements.values())}"
            )
    except OnedataApiError as e:
        federation_pass = False
        fed_diag.append(f"qos summary read error: {e}")

    mcp_diag = []
    if not called_qos:
        mcp_diag.append("no get_file_qos_summary on violator file")
    if not answer_ok:
        mcp_diag.append("answer missing 'violator.bin' or 'impossible'")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# P3: Force ≥2 EU replicas, polling (dynamic)
# ---------------------------------------------------------------------------


async def verify_p3(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    target_path = f"/{ctx.space_name}/p3/result.bin"
    file_id = ctx.fixture_paths.get(target_path)
    if not file_id:
        return OracleResult(False, False, diagnosis="fixture path missing")

    # mcp_pass: agent added an EU+replicas≥2 QoS req, AND polled (either
    # list_space_transfers or get_file_qos_summary), AND the answer names
    # which condition was observed.
    def _eu_2plus(args: dict) -> bool:
        expr = args.get("expression", "") or ""
        rep = args.get("replicas_num", 0) or 0
        return any(tok in expr for tok in EU_TOKENS) and rep >= 2

    added_qos = bool(find_calls(trace, "add_file_qos_requirement", _eu_2plus))
    polled = bool(find_calls(trace, "list_space_transfers")) or bool(
        find_calls(
            trace,
            "get_file_qos_summary",
            lambda a: a.get("file_id_or_path") in (target_path, file_id),
        )
    )
    answer = trace.final_answer
    answer_ok = contains_token(answer, "transfer") or contains_token(answer, "fulfilled")
    mcp_pass = added_qos and polled and answer_ok

    # federation_pass: within deadline, EITHER a transfer for this file
    # appears OR a rule is fulfilled.
    federation_pass = False
    fed_diag: list[str] = []
    deadline = time.time() + DYNAMIC_DEADLINE_SECONDS
    space_id = await _space_id()
    try:
        while time.time() < deadline:
            if await _any_transfer_for_file(
                space_id, "ongoing", file_id
            ) or await _any_transfer_for_file(space_id, "ended", file_id):
                federation_pass = True
                break
            try:
                summary = await qos_api.get_file_qos_summary(file_id)
            except OnedataApiError:
                summary = {"requirements": {}}
            if any(s == "fulfilled" for s in (summary.get("requirements") or {}).values()):
                federation_pass = True
                break
            time.sleep(DYNAMIC_POLL_INTERVAL)
        if not federation_pass:
            fed_diag.append(f"no transfer + no fulfilled rule within {DYNAMIC_DEADLINE_SECONDS}s")
    except Exception as e:  # noqa: BLE001
        fed_diag.append(f"polling error: {e}")

    mcp_diag = []
    if not added_qos:
        mcp_diag.append("no add_file_qos_requirement with EU + replicas≥2")
    if not polled:
        mcp_diag.append("no list_space_transfers or get_file_qos_summary follow-up")
    if not answer_ok:
        mcp_diag.append("answer doesn't claim 'transfer' or 'fulfilled'")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# P4: Most-recent migration (dynamic, pre-staged)
# ---------------------------------------------------------------------------


async def verify_p4(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    """Pre-staged transferId is captured during fixture_runner phase 3."""
    if ctx.captured_transfer_id is None:
        return OracleResult(
            False, False, diagnosis="captured_transfer_id missing — pre-stage failed"
        )

    # mcp_pass: agent called list_space_transfers + get_transfer + reported
    # the captured tid in the answer.
    listed = bool(find_calls(trace, "list_space_transfers"))
    fetched = bool(find_calls(trace, "get_transfer"))
    answer_has_tid = contains_token(trace.final_answer, ctx.captured_transfer_id)
    mcp_pass = listed and fetched and answer_has_tid

    # federation_pass: the captured tid is still queryable (transfer log
    # didn't roll over). Onedata retains transfer history; this should
    # be True modulo extreme retention pressure.
    federation_pass = True
    fed_diag: list[str] = []
    try:
        await transfers_api.get_transfer(ctx.captured_transfer_id)
    except OnedataApiError as e:
        federation_pass = False
        fed_diag.append(f"captured tid no longer queryable: {e}")

    mcp_diag = []
    if not listed:
        mcp_diag.append("no list_space_transfers call")
    if not fetched:
        mcp_diag.append("no get_transfer call")
    if not answer_has_tid:
        mcp_diag.append("answer doesn't contain captured transferId")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# P5: QoS conflict resolution (static)
# ---------------------------------------------------------------------------


async def verify_p5(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    target_path = f"/{ctx.space_name}/p5/conflicted.bin"
    file_id = ctx.fixture_paths.get(target_path)
    if not file_id:
        return OracleResult(False, False, diagnosis="fixture path missing")

    # mcp_pass: agent called get_file_qos_summary on the file, and the
    # answer reports both expressions + a conflict signal.
    called = bool(
        find_calls(
            trace,
            "get_file_qos_summary",
            lambda a: a.get("file_id_or_path") in (target_path, file_id),
        )
    )
    answer = trace.final_answer
    # Accept either canonical attribute tokens or providerId tokens
    # (per research/empirical-findings #14 — SPICE federation doesn't
    # expose user-attribute QoS tags so scenarios use providerIds).
    has_pl = any(contains_token(answer, t) for t in PL_TOKENS)
    has_sk = any(contains_token(answer, t) for t in SK_TOKENS)
    has_conflict_word = any(
        contains_token(answer, kw)
        for kw in ("conflict", "mutual", "exclusive", "incompatible", "cannot")
    )
    mcp_pass = called and has_pl and has_sk and has_conflict_word

    # federation_pass: both rules are actually attached.
    federation_pass = True
    fed_diag: list[str] = []
    try:
        summary = await qos_api.get_file_qos_summary(file_id)
        expressions: list[str] = []
        for qos_id in summary.get("requirements") or {}:
            try:
                detail = await qos_api.get_qos_requirement(qos_id)
            except OnedataApiError:
                continue
            # Onedata 25.0 returns `expression`, NOT `qosExpression`
            # (the swagger example is misleading). Empirical-findings #15.
            expr = detail.get("expression") or detail.get("qosExpression") or ""
            expressions.append(expr)
        # Either user-attr tokens (paper-canonical) or providerId tokens
        # (SPICE-empirical) count as the PL / SK signal.
        has_pl_rule = any(any(t in e for t in PL_TOKENS) for e in expressions)
        has_sk_rule = any(any(t in e for t in SK_TOKENS) for e in expressions)
        if not (has_pl_rule and has_sk_rule):
            federation_pass = False
            fed_diag.append(f"missing PL or SK rule; got expressions={expressions}")
    except OnedataApiError as e:
        federation_pass = False
        fed_diag.append(f"qos read error: {e}")

    mcp_diag = []
    if not called:
        mcp_diag.append("no get_file_qos_summary on conflict file")
    if not has_pl:
        mcp_diag.append("answer missing PL rule (country=PL or cloud-pl providerId)")
    if not has_sk:
        mcp_diag.append("answer missing SK rule (country=SK or Cloud-SK providerId)")
    if not has_conflict_word:
        mcp_diag.append("answer missing conflict / mutual-exclusion signal")

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# P6: replicas_num == 1 introspection (static)
# ---------------------------------------------------------------------------


async def verify_p6(ctx: RunContext, trace: AgentTrace) -> OracleResult:
    candidates = [
        p for p in ctx.fixture_paths if p.startswith(f"/{ctx.space_name}/p6/single-copy/")
    ]

    # Compute the federation's expected single-replica set.
    expected: set[str] = set()
    federation_pass = True
    fed_diag: list[str] = []
    for path in candidates:
        file_id = ctx.fixture_paths[path]
        try:
            summary = await qos_api.get_file_qos_summary(file_id)
            single = False
            for qos_id in summary.get("requirements") or {}:
                try:
                    detail = await qos_api.get_qos_requirement(qos_id)
                except OnedataApiError:
                    continue
                if detail.get("replicasNum") == 1:
                    single = True
                    break
            if single:
                expected.add(path)
        except OnedataApiError as e:
            federation_pass = False
            fed_diag.append(f"{path} qos read error: {e}")

    # mcp_pass: agent called get_file_qos_summary (or list_files_recursively)
    # AND reported set matches federation's actual single-replica set.
    called = bool(find_calls(trace, "get_file_qos_summary")) or bool(
        find_calls(trace, "list_files_recursively")
    )
    reported = extract_paths(trace.final_answer, anchor=f"/{ctx.space_name}/")
    answer_ok = reported == expected
    mcp_pass = called and answer_ok

    mcp_diag = []
    if not called:
        mcp_diag.append("no get_file_qos_summary or list_files_recursively call")
    if not answer_ok:
        mcp_diag.append(
            f"set mismatch: missing={sorted(expected - reported)} "
            f"unexpected={sorted(reported - expected)}"
        )

    return OracleResult(
        mcp_pass=mcp_pass,
        federation_pass=federation_pass,
        diagnosis=_render_diag(mcp_pass, mcp_diag, federation_pass, fed_diag),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _space_id() -> str:
    from benchmark.fixture_runner import _resolve_space_id_async

    return await _resolve_space_id_async()


async def _any_transfer_for_file(space_id: str, state: str, file_id: str) -> bool:
    page = await transfers_api.list_space_transfers(space_id, state=state, limit=100)
    for tid in page.get("transfers", []):
        try:
            detail = await transfers_api.get_transfer(tid)
        except OnedataApiError:
            continue
        if detail.get("fileId") == file_id:
            return True
    return False


def _render_diag(
    mcp_pass: bool, mcp_diag: list[str], federation_pass: bool, fed_diag: list[str]
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
