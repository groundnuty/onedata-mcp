"""Federation-reset + per-trial fixture materialisation.

Implementation of the 5-phase protocol described in
`design/05-federation-reset-protocol.md`. The runner uses
`onedata_mcp.api.*` directly (the "REST side-channel" in paper §4¶3),
NOT the agent-facing MCP transport.

Public entry point: `prepare_trial(scenario)` → `RunContext`.
"""

from __future__ import annotations

import json
import time

from benchmark._runtime_types import (
    CONVERGENCE_POLL_INTERVAL,
    RESET_HARD_CAP_SECONDS,
    RESET_SOFT_CAP_SECONDS,
    FixtureResetTimeout,
    RunContext,
)
from benchmark._scenario_types import FileFixture, Scenario, TransferFixtureHint
from onedata_mcp.api import files as files_api
from onedata_mcp.api import qos as qos_api
from onedata_mcp.api import spaces as spaces_api
from onedata_mcp.api import transfers as transfers_api
from onedata_mcp.config import get_oneprovider_config
from onedata_mcp.utils import OnedataApiError, request

SPACE = "ppam_2026_mcp_tests"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def prepare_trial(
    scenario: Scenario,
    *,
    space_name: str = SPACE,
    space_id: str | None = None,
) -> RunContext:
    """Wipe + materialise + pre-stage + converge for one trial of `scenario`.

    `space_name` selects which Onedata space the fixture targets. Default
    is the original shared `ppam_2026_mcp_tests`; per-LLM-space mode
    passes the LLM's dedicated space (see `benchmark/_per_llm_spaces.py`).

    `space_id` short-circuits the name-to-id lookup. Useful for spaces
    that aren't visible via `list_user_spaces` (e.g. the per-LLM spaces
    were created via the admin endpoint without auto-membership). When
    None, the code falls back to scanning `list_user_spaces` by name.

    The caller MUST pass scenario already specialised to `space_name`
    (so fixture paths match the target space) — see
    `benchmark/_scenario_specialise.py`.

    Raises FixtureResetTimeout if convergence doesn't complete within the
    hard cap; the harness catches this and marks the trial RESET_FAIL.
    """
    started_at = time.time()
    subtree_path = f"/{space_name}/{scenario.id.lower()}"

    # Pre-populate space-id cache if caller knew it. Saves a roundtrip
    # AND works for spaces invisible via list_user_spaces.
    if space_id is not None:
        _SPACE_ID_CACHE[space_name] = space_id

    # Phase 1 — Wipe
    await _wipe_subtree(subtree_path)

    # Phase 2 — Materialise files
    fixture_paths = await _materialise_files(scenario.fixture.files)

    # Phase 3 — Pre-stage transfers (P4 only at present)
    captured_transfer_id: str | None = None
    for hint in scenario.fixture.transfers:
        captured_transfer_id = await _prestage_transfer(hint, space_name)

    # Phase 4 — Convergence wait
    await _wait_for_convergence(scenario, started_at, space_name)

    # Snapshot federation state right before handing off to the harness.
    # See research/empirical-findings #18: re-querying at oracle time
    # produces flaky results when the federation has many spaces churning.
    spaces_snapshot = tuple(await spaces_api.list_user_spaces())

    resolved_space_id = _SPACE_ID_CACHE.get(space_name)

    return RunContext(
        scenario_id=scenario.id,
        fixture_paths=fixture_paths,
        captured_transfer_id=captured_transfer_id,
        fixture_started_at=started_at,
        fixture_ready_at=time.time(),
        spaces_snapshot=spaces_snapshot,
        space_name=space_name,
        space_id=resolved_space_id,
    )


# ---------------------------------------------------------------------------
# Phase 1 — Wipe
# ---------------------------------------------------------------------------


async def _wipe_subtree(subtree_path: str) -> None:
    """Recursively delete `subtree_path`. Idempotent — missing path is OK.

    Onedata's delete_file recurses into directories per the swagger.
    Leftover orphan transfers from prior trials are NOT cancelled here
    (per design/05: 'we shall see how stable transfers are' — observe first).
    """
    try:
        await files_api.delete_file(subtree_path)
    except FileNotFoundError:
        pass  # first run; nothing to wipe
    except OnedataApiError as e:
        if getattr(e, "errno", None) == "enoent":
            pass
        else:
            raise


# ---------------------------------------------------------------------------
# Phase 2 — Materialise files
# ---------------------------------------------------------------------------


async def _materialise_files(
    fixture_files: tuple[FileFixture, ...],
) -> dict[str, str]:
    """Create files, set metadata, attach QoS rules. Returns path → fileId map."""
    fixture_paths: dict[str, str] = {}
    for f in fixture_files:
        file_id = await files_api.create_file(
            f.path,
            f.content,
            create_parents=True,
        )
        fixture_paths[f.path] = file_id

        if f.json_metadata is not None:
            await files_api.set_file_metadata(file_id, "json", json.dumps(f.json_metadata))

        for expression, replicas_num in f.qos_expressions:
            await qos_api.add_qos_requirement(file_id, expression, replicas_num=replicas_num)

    return fixture_paths


# ---------------------------------------------------------------------------
# Phase 3 — Pre-stage transfers (P4)
# ---------------------------------------------------------------------------


async def _prestage_transfer(hint: TransferFixtureHint, space_name: str) -> str:
    """Schedule a transfer directly via POST /transfers and return the
    transferId.

    Earlier strategy used a temp QoS rule (providerId=<id>) to indirectly
    trigger a migration; live smoke 2026-05-01 found the rule stayed
    'pending' indefinitely on the live federation (research/empirical-
    findings #16), so the migration was never reliably triggered.

    Direct strategy: POST /transfers with the desired transfer type,
    source/dest providerIds, and fileId. Onedata returns the transferId
    immediately. The transfer remains in the federation log (Onedata
    retains transfer history), which is what the P4 oracle reads at
    trial-end.

    For 'migration': replicating_provider_id = target (where the data
    will be copied to), evicting_provider_id = the OTHER provider in
    the space (where it currently is and gets removed from). This
    mirrors paper-canonical 'migration' semantics: replicate then evict.
    """
    target_pid = await _resolve_provider_id(hint.target_provider_name, space_name)

    # Pick the OTHER bound provider as the eviction source (works because
    # this benchmark space has exactly 2 providers).
    space_id = await _resolve_space_id_async(space_name)
    detail = await spaces_api.get_space_providers(space_id)
    bound_pids = [
        e["providerId"]
        for e in (detail.get("providers") or [])
        if isinstance(e, dict) and isinstance(e.get("providerId"), str)
    ]
    other_pids = [pid for pid in bound_pids if pid != target_pid]
    if not other_pids:
        raise RuntimeError(f"Pre-stage migration needs ≥2 bound providers; got {len(bound_pids)}")

    file_id = await files_api.get_file_id(hint.src_path)

    if hint.transfer_type == "migration":
        resp = await transfers_api.create_transfer(
            file_id,
            "migration",
            replicating_provider_id=target_pid,
            evicting_provider_id=other_pids[0],
        )
    elif hint.transfer_type == "replication":
        resp = await transfers_api.create_transfer(
            file_id, "replication", replicating_provider_id=target_pid
        )
    elif hint.transfer_type == "eviction":
        resp = await transfers_api.create_transfer(
            file_id, "eviction", evicting_provider_id=target_pid
        )
    else:
        raise ValueError(f"unknown transfer_type: {hint.transfer_type!r}")

    transfer_id = resp.get("transferId")
    if not isinstance(transfer_id, str):
        raise RuntimeError(f"create_transfer returned no transferId: {resp!r}")
    return transfer_id


async def _resolve_provider_id(provider_name: str, space_name: str) -> str:
    """Look up providerId by providerName in the given space."""
    space_id = await _resolve_space_id_async(space_name)
    detail = await spaces_api.get_space_providers(space_id)
    for entry in detail.get("providers", []):
        if isinstance(entry, dict) and entry.get("providerName") == provider_name:
            pid = entry.get("providerId")
            if isinstance(pid, str):
                return pid
    raise RuntimeError(f"Provider {provider_name!r} not found in space {space_name!r}")


async def _resolve_space_id_async(space_name: str = SPACE) -> str:
    """Look up `space_name` → spaceId via list_user_spaces. Cached per name.

    Per-LLM spaces created via the admin endpoint don't appear in
    list_user_spaces (the calling user isn't a member). For those,
    `prepare_trial` pre-populates `_SPACE_ID_CACHE` from the
    `_per_llm_spaces.PER_LLM_SPACE_ID` registry. This function then
    returns the cached value without re-querying.
    """
    cached = _SPACE_ID_CACHE.get(space_name)
    if cached is not None:
        return cached
    spaces = await spaces_api.list_user_spaces()
    for s in spaces:
        if s.get("name") == space_name:
            sid = s.get("spaceId")
            if isinstance(sid, str):
                _SPACE_ID_CACHE[space_name] = sid
                return sid
    raise RuntimeError(
        f"Space {space_name!r} not found via list_user_spaces; for "
        "per-LLM spaces, pass space_id= to prepare_trial() to bypass "
        "this lookup."
    )


# Per-name space-id cache. Populated lazily by _resolve_space_id_async or
# eagerly by prepare_trial() when it knows the id from the registry.
_SPACE_ID_CACHE: dict[str, str] = {}


def _resolve_space_id_sync() -> str:
    """Convenience for callers that already cached the id; fails if not yet
    populated (always run async _resolve_space_id_async() first via
    prepare_trial). Returns the most recently used space id (or any one
    cached); ambiguous in per-LLM-space mode where multiple are cached.
    """
    if not _SPACE_ID_CACHE:
        raise RuntimeError("space id cache not populated — call prepare_trial first")
    return next(iter(_SPACE_ID_CACHE.values()))


async def _find_transfer_for_file(transfer_ids: list[str], file_id: str) -> str | None:
    """Return the first transferId in `transfer_ids` whose fileId matches."""
    for tid in transfer_ids:
        try:
            detail = await transfers_api.get_transfer(tid)
        except OnedataApiError:
            continue
        if detail.get("fileId") == file_id:
            return tid
    return None


# ---------------------------------------------------------------------------
# Phase 4 — Convergence wait
# ---------------------------------------------------------------------------


async def _wait_for_convergence(scenario: Scenario, started_at: float, space_name: str) -> None:
    """Poll until fixture state observable for 2 consecutive intervals OR
    hard-cap timeout."""
    soft_deadline = started_at + RESET_SOFT_CAP_SECONDS
    hard_deadline = started_at + RESET_HARD_CAP_SECONDS

    consecutive_matches = 0
    while time.time() < hard_deadline:
        if await _check_convergence(scenario, space_name):
            consecutive_matches += 1
            if consecutive_matches >= 2:
                return
        else:
            consecutive_matches = 0
        # Sleep up to the next poll, but not past the hard deadline.
        remaining = hard_deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(CONVERGENCE_POLL_INTERVAL, remaining))

        if time.time() > soft_deadline and consecutive_matches == 0:
            # Soft-cap warning would land here in a future logging pass.
            pass

    raise FixtureResetTimeout(
        f"Scenario {scenario.id} fixture did not converge within "
        f"{RESET_HARD_CAP_SECONDS}s (soft cap {RESET_SOFT_CAP_SECONDS}s)"
    )


async def _check_convergence(scenario: Scenario, space_name: str) -> bool:
    """Single convergence check. True iff every fixture file is observable
    with expected metadata + non-pending QoS."""
    for f in scenario.fixture.files:
        try:
            file_id = await files_api.get_file_id(f.path)
        except FileNotFoundError:
            return False

        if f.json_metadata is not None:
            try:
                got = await files_api.get_file_metadata(file_id, ["json"])
            except OnedataApiError:
                return False
            if got.get("json") != f.json_metadata:
                return False

        if f.qos_expressions:
            # First: check rules are at least attached (catches the
            # "rule was never created" case).
            try:
                summary = await qos_api.get_file_qos_summary(file_id)
            except OnedataApiError:
                return False
            requirements = summary.get("requirements") or {}
            if not isinstance(requirements, dict):
                return False
            if len(requirements) < len(f.qos_expressions):
                return False

            # Strategy 3a (research/empirical-findings #16): inspect
            # actual data placement, not QoS rule status. Onedata 25.0
            # leaves rule status pinned at 'pending' indefinitely in
            # multi-rule cases even when the underlying data IS fully
            # replicated to all required providers.
            #
            # Special case: if EVERY rule is in 'impossible' status, the
            # fixture is intentionally unfulfillable (e.g. P2 violator
            # files) — that's the terminal converged state, even though
            # no data placement satisfies it. Treat as converged.
            statuses = list(requirements.values())
            if statuses and all(s == "impossible" for s in statuses):
                continue  # this file converged: rule is terminally impossible
            if not await _qos_data_satisfied(file_id, f.qos_expressions, space_name):
                return False

    return True


async def _qos_data_satisfied(
    file_id: str,
    qos_expressions: tuple[tuple[str, int], ...],
    space_name: str,
) -> bool:
    """True if the file's data is replicated to satisfy each QoS expression.

    Reads `get_file_distribution`. For each (expression, replicas_num)
    pair, evaluates the expression once via the space-level QoS
    evaluator to get the matching providerIds, then checks the file is
    fully replicated to at least `replicas_num` of them.
    """
    try:
        dist = await files_api.get_file_distribution(file_id)
    except OnedataApiError:
        return False
    per_provider = dist.get("distributionPerProvider") or {}
    if not isinstance(per_provider, dict):
        return False

    fully_replicated_pids: set[str] = set()
    for pid, entry in per_provider.items():
        if not isinstance(entry, dict) or not entry.get("success"):
            continue
        # Empirical-findings #15: 25.0 returns virtualSize, older docs
        # showed logicalSize. Permissive fallback.
        logical = entry.get("virtualSize") or entry.get("logicalSize") or 0
        physical = sum(
            sb.get("physicalSize") or 0
            for sb in (entry.get("distributionPerStorageBackend") or {}).values()
            if isinstance(sb, dict) and sb.get("success")
        )
        if logical > 0 and physical >= logical:
            fully_replicated_pids.add(pid)

    space_id = await _resolve_space_id_async(space_name)
    for expression, replicas_num in qos_expressions:
        try:
            matching = await _evaluate_qos_expression_provider_ids(space_id, expression)
        except OnedataApiError:
            return False
        # How many of the matching providers actually hold the file fully?
        if len(matching & fully_replicated_pids) < replicas_num:
            return False
    return True


async def _evaluate_qos_expression_provider_ids(space_id: str, expression: str) -> set[str]:
    """Resolve a QoS expression to the set of providerIds whose storages
    match it. Uses POST /spaces/{sid}/evaluate_qos_expression."""
    config = get_oneprovider_config()
    response = await request(
        config,
        "POST",
        f"/spaces/{space_id}/evaluate_qos_expression",
        json_body={"expression": expression},
    )
    body = response["body"] or {}
    matching = body.get("matchingStorageBackends") or body.get("matchingStorages") or []
    pids: set[str] = set()
    for entry in matching:
        if isinstance(entry, dict):
            pid = entry.get("providerId")
            if isinstance(pid, str):
                pids.add(pid)
    return pids


# ---------------------------------------------------------------------------
# Test hook
# ---------------------------------------------------------------------------


def _reset_space_id_cache_for_tests() -> None:
    """Tests that use pytest-httpx don't share global state; reset between
    tests so stale cached ids from a prior test don't bleed in."""
    _SPACE_ID_CACHE.clear()
