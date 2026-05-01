# Design decision: federation reset protocol

**Status:** decided 2026-05-01.
**Implemented at:** `benchmark/fixture_runner.py`.
**Tested at:** `test/unit/test_fixture_runner.py` (mocked HTTP).
**Live verification:** pending `--write` smoke gate.

## The principle

Reset the federation **per scenario subtree**, not per whole space.
Every fixture path is anchored under `/ppam_2026_mcp_tests/<scenario_id>/`
(enforced by `test_fixture_paths_anchored_in_benchmark_space`); reset
operates on one subtree at a time. Other scenarios' state stays intact,
so trials can be ordered freely and could later parallelise.

## The 5-phase protocol

```
For each trial of scenario X:

PHASE 1 — Wipe
  delete_file('/ppam_2026_mcp_tests/<x>/')
    (recursive; idempotent — missing path is OK)

PHASE 2 — Materialise files
  for each FileFixture in scenario.fixture.files:
    create_file(path, content, create_parents=True)
    if json_metadata: set_file_metadata(file_id, 'json', json.dumps(meta))
    for (expr, replicas) in qos_expressions:
      add_qos_requirement(file_id, expr, replicas)
  → returns {logical_path: fileId} mapping

PHASE 3 — Pre-stage transfers (P4 only)
  for each TransferFixtureHint:
    add temp QoS rule pinning to target providerId
    poll list_space_transfers until a transfer for this file appears
    capture transferId
    remove temp QoS rule
  → returns the captured transferId for the oracle's ground truth

PHASE 4 — Convergence wait
  Repeat at 5-second intervals, soft cap 60s / hard cap 120s:
    converged = True
    for each FileFixture:
      verify file exists at path (lookup_file_id)
      verify json metadata == expected
      verify QoS rules attached, none in 'pending' status
    if converged for 2 consecutive polls → ready
  Hard-cap overrun → raise FixtureResetTimeout
    → harness catches, marks trial RESET_FAIL, re-queues

PHASE 5 — Trial runs (in harness, not here)
  Hand brief + RunContext to harness; capture AgentTrace; dispatch oracle.
```

## What the runner returns

```python
@dataclass(frozen=True)
class RunContext:
    scenario_id: str
    fixture_paths: dict[str, str]       # logical_path → fileId
    captured_transfer_id: str | None    # P4 only; None for everything else
    fixture_started_at: float           # epoch when phase 1 started
    fixture_ready_at: float             # epoch when phase 4 declared ready
```

The agent never sees this. The oracle uses it to know which fileIds
materialised to which logical paths and (P4 only) the ground-truth
transferId.

## Layer separation

| Layer | What it uses |
|---|---|
| **Fixture runner** | `onedata_mcp.api.*` Python functions directly. The "REST side-channel" the paper §4¶3 names. |
| **Oracle** | Same — `onedata_mcp.api.*` directly to inspect post-state for `federation_pass`. Logged separately so the harness can prove the oracle never called a write tool. |
| **Agent** | Only `mcp.call_tool(...)` via FastMCP, restricted to `allowed_tools_minimal` per scenario. Never sees `RunContext`, the captured transferId, or any other scenario's subtree. |

## Choices recorded

### Subtree, not whole-space, reset

- **Safer**: a bug in scenario X's reset can't trash scenario Y's fixture.
- **Faster**: smaller subtree → faster delete + faster convergence.
- **Composable**: lets us shuffle scenario order across runs (paper §4
  doesn't prescribe order; randomising is good for fairness).
- **Future-proof**: parallel trials would naturally use scenario-level
  isolation.

### Delete-and-recreate, not diff-and-patch

- Simpler and more deterministic; no "I think the state is right"
  guesswork.
- Edge cases like agent-created orphan files in unexpected paths are
  handled by the recursive delete.
- The 60s soft cap is generous enough to absorb the cost; if it ever
  overruns, the dbsync calibration sweep (paper §4¶5 TODO) would tighten
  the cap, not the strategy.

### Orphan-transfer policy: ignore on first pass

When the agent triggers `add_file_qos_requirement` mid-trial and the
transfer is still ongoing when reset starts, two options exist:

1. **Cancel via `DELETE /transfers/{tid}`** (Onedata 25.0 supports this).
2. **Let it run** — the wipe deletes the file; the transfer self-cancels.

Default is **option 2** (per user 2026-05-01: "we shall see how stable
transfers are"). Switch to option 1 if we observe lingering side-effects
in the live --write smoke.

### Soft / hard caps from paper §4¶5, not measured

Currently 60s soft / 120s hard. Paper §4¶5 has a `TODO(dbsync-calibration)`
marker — the 50-trial DE-Azure → SK-IISAS sweep will tighten the hard cap
to "p99 + 20% margin". Until then, the conservative caps avoid premature
RESET_FAIL flagging.

### Cached space ID

`_resolve_space_id_async()` caches the `ppam_2026_mcp_tests` spaceId per
process (one onezone roundtrip on first call, then in-memory).
Test-isolation hook `_reset_space_id_cache_for_tests()` clears the cache
between tests so pytest-httpx mocks don't bleed across cases.

## What's NOT in scope here

- **Agent harness wiring.** Workstream #19 (multi-LLM forge_harness)
  consumes the RunContext from `prepare_trial(scenario)` and dispatches
  the brief to the LLM under test.
- **pass^k aggregation.** Workstream #22 takes per-trial OracleResult
  outcomes and produces the headline tables.
- **The token-rotation logic.** Token currently has no time caveat; if
  we tighten it pre-camera-ready, the runner needs rotation. Out of scope.

## Cross-references

- `benchmark/fixture_runner.py` — the implementation
- `benchmark/_runtime_types.py` — `RunContext`, `FixtureResetTimeout`,
  `RESET_SOFT_CAP_SECONDS`, `RESET_HARD_CAP_SECONDS`
- `benchmark/_scenario_types.py` — `Fixture`, `FileFixture`,
  `TransferFixtureHint`
- `test/unit/test_fixture_runner.py` — unit tests
- `design/06-oracle-philosophy.md` — the two-axis OracleResult the
  oracle dispatches against
- `papers/ppam-2026/research/27-benchmark-space-snapshot.md` — federation
  state these phases run against
- Paper §4¶5 — soft/hard cap rationale; dbsync calibration TODO
