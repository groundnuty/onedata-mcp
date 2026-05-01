"""Federation-reset + per-trial fixture materialisation.

Implementation of the 5-phase protocol described in
`design/05-federation-reset-protocol.md`. The runner uses
`onedata_mcp.api.*` directly (the "REST side-channel" in paper §4¶3),
NOT the agent-facing MCP transport.

Public entry point: `prepare_trial(scenario)` → `RunContext`.
"""

from __future__ import annotations

import contextlib
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
from onedata_mcp.utils import OnedataApiError

SPACE = "ppam_2026_mcp_tests"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def prepare_trial(scenario: Scenario) -> RunContext:
    """Wipe + materialise + pre-stage + converge for one trial of `scenario`.

    Raises FixtureResetTimeout if convergence doesn't complete within the
    hard cap; the harness catches this and marks the trial RESET_FAIL.
    """
    started_at = time.time()
    subtree_path = f"/{SPACE}/{scenario.id.lower()}"

    # Phase 1 — Wipe
    await _wipe_subtree(subtree_path)

    # Phase 2 — Materialise files
    fixture_paths = await _materialise_files(scenario.fixture.files)

    # Phase 3 — Pre-stage transfers (P4 only at present)
    captured_transfer_id: str | None = None
    for hint in scenario.fixture.transfers:
        captured_transfer_id = await _prestage_transfer(hint)

    # Phase 4 — Convergence wait
    await _wait_for_convergence(scenario, started_at)

    return RunContext(
        scenario_id=scenario.id,
        fixture_paths=fixture_paths,
        captured_transfer_id=captured_transfer_id,
        fixture_started_at=started_at,
        fixture_ready_at=time.time(),
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


async def _prestage_transfer(hint: TransferFixtureHint) -> str:
    """Trigger a migration of `hint.src_path` to `hint.target_provider_name`,
    capture the transferId, then back out the staging QoS rule.

    Strategy: temp QoS rule pinning to providerId. The Onedata QoS DSL
    supports `providerId=<id>` as an explicit operand. We add the rule,
    poll until a transfer for this file appears in list_space_transfers,
    capture the transferId, then remove the rule. The transferId remains
    in the federation transfer log (Onedata retains transfer history),
    which is what the oracle reads at trial-end.
    """
    target_pid = await _resolve_provider_id(hint.target_provider_name)

    # Resolve the file id for the temp rule.
    file_id = await files_api.get_file_id(hint.src_path)

    # Add temp pinning rule.
    rule = await qos_api.add_qos_requirement(
        file_id,
        f"providerId={target_pid}",
        replicas_num=1,
    )
    rule_id = rule.get("qosRequirementId") or rule.get("id")

    try:
        # Poll until the migration appears in the transfer log for this file.
        deadline = time.time() + RESET_HARD_CAP_SECONDS
        captured: str | None = None
        while time.time() < deadline:
            page = await transfers_api.list_space_transfers(
                _resolve_space_id_sync(),
                state="ongoing",
                limit=100,
            )
            captured = await _find_transfer_for_file(page["transfers"], file_id)
            if captured is not None:
                break
            page = await transfers_api.list_space_transfers(
                _resolve_space_id_sync(), state="ended", limit=100
            )
            captured = await _find_transfer_for_file(page["transfers"], file_id)
            if captured is not None:
                break
            time.sleep(CONVERGENCE_POLL_INTERVAL)

        if captured is None:
            raise FixtureResetTimeout(
                f"Pre-stage transfer for {hint.src_path} did not appear in "
                f"the transfer log within {RESET_HARD_CAP_SECONDS}s"
            )
        return captured
    finally:
        # Always release the temp rule, even if polling failed.
        # Best-effort cleanup; orphan rule is recoverable manually if needed.
        if rule_id:
            with contextlib.suppress(OnedataApiError):
                await qos_api.remove_qos_requirement(rule_id)


async def _resolve_provider_id(provider_name: str) -> str:
    """Look up providerId by providerName in the benchmark space."""
    space_id = await _resolve_space_id_async()
    detail = await spaces_api.get_space_providers(space_id)
    for entry in detail.get("providers", []):
        if isinstance(entry, dict) and entry.get("providerName") == provider_name:
            pid = entry.get("providerId")
            if isinstance(pid, str):
                return pid
    raise RuntimeError(f"Provider {provider_name!r} not found in space {SPACE!r}")


async def _resolve_space_id_async() -> str:
    """Look up SPACE name → spaceId via list_user_spaces. Cached per process."""
    global _CACHED_SPACE_ID
    if _CACHED_SPACE_ID is not None:
        return _CACHED_SPACE_ID
    spaces = await spaces_api.list_user_spaces()
    for s in spaces:
        if s.get("name") == SPACE:
            sid = s.get("spaceId")
            if isinstance(sid, str):
                _CACHED_SPACE_ID = sid
                return sid
    raise RuntimeError(f"Benchmark space {SPACE!r} not found")


_CACHED_SPACE_ID: str | None = None


def _resolve_space_id_sync() -> str:
    """Convenience for callers that already cached the id; fails if not yet
    populated (always run async _resolve_space_id_async() first via
    prepare_trial)."""
    if _CACHED_SPACE_ID is None:
        raise RuntimeError("space id cache not populated — call prepare_trial first")
    return _CACHED_SPACE_ID


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


async def _wait_for_convergence(scenario: Scenario, started_at: float) -> None:
    """Poll until fixture state observable for 2 consecutive intervals OR
    hard-cap timeout."""
    soft_deadline = started_at + RESET_SOFT_CAP_SECONDS
    hard_deadline = started_at + RESET_HARD_CAP_SECONDS

    consecutive_matches = 0
    while time.time() < hard_deadline:
        if await _check_convergence(scenario):
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


async def _check_convergence(scenario: Scenario) -> bool:
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
            try:
                summary = await qos_api.get_file_qos_summary(file_id)
            except OnedataApiError:
                return False
            requirements = summary.get("requirements", {})
            if not isinstance(requirements, dict):
                return False
            # Any requirement still in 'pending' status → not converged.
            for status in requirements.values():
                if status == "pending":
                    return False
            # Number of distinct rules attached must be at least the
            # number we requested (the system may add more for inheritance,
            # which we tolerate).
            if len(requirements) < len(f.qos_expressions):
                return False

    return True


# ---------------------------------------------------------------------------
# Test hook
# ---------------------------------------------------------------------------


def _reset_space_id_cache_for_tests() -> None:
    """Tests that use pytest-httpx don't share global state; reset between
    tests so a stale cached id from a prior test doesn't bleed in."""
    global _CACHED_SPACE_ID
    _CACHED_SPACE_ID = None
