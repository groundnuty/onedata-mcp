# Design decision: 15-tool benchmark allowlist

**Status:** decided 2026-05-01 (revised the same day after team discussion: kept `delete_file` in headline; count moved 14 → 15).
**Lives at:** `benchmark/tool_allowlist.py` (machine-readable, imported by the harness).
**Sanity-checked by:** `test/unit/test_tool_allowlist.py` (asserts every server-exposed tool is classified, every classified tool is server-exposed, sets are disjoint, count is exactly 15).

## Context

The fork registers 21 MCP tools (14 inherited from the base surface + 7 we added). The PPAM 2026 paper claims a "curated 14-tool surface" — that count is editable in the paper text and was not load-bearing on the curation argument. Before scenario authoring (#20) and harness extension (#19) we need a concrete, frozen, version-controlled allowlist that:

- maps cleanly onto the paper's Table 1 (so the paper-writing agent can update the table consistently)
- can be passed to the harness as a `frozenset[str]` to filter `tools/list` for the headline sweep
- leaves a sensible "ablation extras" set for the §5.6 ablation, that *isn't* polluted by tools that always fail (harvesters)
- defends a "data-management platform" framing — meaning **full file CRUD**, not "everything except deletion"

## The 15

Mapping to paper Table 1 (paper-name → server name in our fork):

| # | Paper Table 1 row | Allowlist tool name | R-class |
|---|---|---|---|
| 1 | `list_spaces` | `list_user_spaces` | R1 |
| 2 | `find_files` | `list_files_recursively` | R1 |
| 3 | `read_file` | `download_file` | R1 |
| 4 | `create_file` | `create_file` | R1 |
| 5 | `rename_file` | `move_file` (stub) | R1 |
| 6 | `remove_file` | `delete_file` | R1 |
| 7 | `get_metadata` | `get_file_metadata` | R2 |
| 8 | `set_metadata` | `set_file_metadata` | R2 |
| 9 | `query_by_metadata` | `query_by_metadata` | R2 |
| 10 | `list_providers` | `list_space_providers` | R3 |
| 11 | `get_file_distribution` | `get_file_distribution` | R4 |
| 12 | `get_qos_rules` | `get_file_qos_summary` | R5 |
| 13 | `set_qos_rule` | `add_file_qos_requirement` | R5 |
| 14 | `list_transfers` | `list_space_transfers` | R6 |
| **15** | **`get_transfer`** *(NEW vs paper)* | `get_transfer` | R6 |

**One paper-text edit the writing agent will need to make:**

- **Add `get_transfer` to Table 1; bump the headline count from "14-tool" to "15-tool" everywhere it appears.** *Reason:* the `list_space_transfers` REST endpoint returns transfer **IDs only** — no source / destination / state / bytes detail. Scenario P4 ("Most-recent migration of file F") is unsolvable from IDs alone; the agent must follow up with `get_transfer` per ID. Without `get_transfer` in the allowlist, P4 would always fail not because of model capability but because of allowlist incompleteness — exactly the failure mode the paper §3 curation argument warns against. (Search for `14` in §3, §4, §5.6, abstract; replace with `15` where the context is the tool count.)

`remove_file` (= `delete_file` in the server convention) **stays in the headline.** A "federated data layer" claim with create-but-no-delete reads as a CRUD asymmetry the paper would have to justify. Keeping the deletion primitive preserves the platform framing even if no headline scenario exercises it; it also leaves the door open for future scenarios (planned cleanup oracles, cross-trial fixture resets) without re-curating.

## Why these names (server convention vs paper convention)

Resolved 2026-04-30: pick whichever naming is most aligned with the underlying Onedata REST API. The server's names (`list_user_spaces`, `download_file`, `delete_file`, `get_file_metadata`, etc.) are more REST-faithful than the paper's (`list_spaces`, `read_file`, etc.) — the server name maps to the actual operationId in the swagger. The paper-writing agent updates Table 1 + prose mentions when picking up the results. Documented in IMPLEMENTATION_NOTES.md.

## What's in the ablation surface

The `tool_context_mode="full"` ablation surface (`ABLATION_FULL`) extends the headline with:

| Extra tool | Why excluded from headline |
|---|---|
| `list_marketplace_spaces` | orthogonal to user-space listing; not exercised by any scenario |
| `list_children` | shallow listing; `list_files_recursively` covers benchmark needs (max_depth=1 reproduces shallow behaviour if needed) |
| `get_file_id` | polymorphic input handling on every other tool collapses this need; only useful as an explicit fileId-resolution helper |
| `get_file_attributes` | per-file attrs available in batch via `list_files_recursively`'s `attributes=[...]` parameter |
| `grep_file_content` | convenience over `download_file`; not exercised by any scenario |
| `get_qos_requirement` | per-id detail; `get_file_qos_summary` covers benchmark needs (returns full requirements map keyed by id) |
| `remove_qos_requirement` | not exercised by any of the 18 scenarios |

**7 extras + 15 headline = 22 tools in the ablation surface.** That gap (7 tools = ~32% of the full surface) is the curation contribution we measure against in §5.6.

## What's never in any sweep

`EXCLUDED_HARVESTER`: `list_user_harvesters`, `get_harvester_index_schema`, `query_harvester_index`. Reason: the SPICE federation has no harvester configured (confirmed by user 2026-04-30; see `design/02-query-by-metadata-no-harvester.md`). These tools are kept registered for codebase parity with the upstream base surface, but they always fail on this deployment, so including them in either sweep would manufacture artificial failures unrelated to either model capability or curation.

## Why the headline is 15 (not 14, not 16)

- **14** would mean dropping either `delete_file` (sacrifices the "data-management platform" CRUD framing) or `get_transfer` (sacrifices P4 — agent can't determine "most-recent migration" from IDs alone). Either drop trades a real benchmark / framing concern for a round-number count. Rejected after team discussion 2026-05-01.
- **16** would mean keeping `list_children` in the headline alongside `list_files_recursively`. Defensible for ergonomic reasons (agents may want shallow listing without the depth=1 trick) but adds one tool that doesn't materially expand task feasibility. Rejected — `list_files_recursively` with `max_depth=1` is one option deeper, not a barrier.
- **15** is the smallest set that (a) covers all 18 scenarios with no allowlist-driven failures, (b) preserves full file-CRUD framing, and (c) introduces only one paper-text edit (count `14` → `15` plus `get_transfer` row in Table 1).

## Cross-references

- `benchmark/tool_allowlist.py` — the machine-readable source of truth
- `test/unit/test_tool_allowlist.py` — drift detection
- `design/02-query-by-metadata-no-harvester.md` — why `query_by_metadata` and not `query_harvester_index`
- `design/01-move-file-strategy.md` — why `move_file` is currently a stub but stays in the allowlist
- `IMPLEMENTATION_NOTES.md` — the three corrections vs paper §3 (qos summary path, qos_requirements top-level, transfer state enum)
- Paper draft Table 1, §3 curation argument, §5.6 ablation
