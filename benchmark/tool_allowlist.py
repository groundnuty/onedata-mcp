"""Curated tool allowlist for the PPAM 2026 headline benchmark sweep.

The full MCP server exposes ~21 tools (M0rgho's 14 + 7 we added). For the
**headline** benchmark the harness restricts the agent to a curated 16-tool
surface; the **ablation** sweep uses the full surface to test the curation
contribution. See:

- design/03-tool-allowlist-curation.md   for the rationale per tool
- design/02-query-by-metadata-no-harvester.md   for harvester exclusion
- design/01-move-file-strategy.md   for the move_file caveat

Mapping back to paper Table 1 (paper-name → M0rgho name):

  list_spaces             →  list_user_spaces
  find_files              →  list_files_recursively
  read_file               →  download_file
  create_file             →  create_file
  rename_file             →  move_file              (stub — see design/01)
  remove_file             →  delete_file
  get_metadata            →  get_file_metadata
  set_metadata            →  set_file_metadata
  query_by_metadata       →  query_by_metadata
  list_providers          →  list_space_providers
  get_file_distribution   →  get_file_distribution
  get_qos_rules           →  get_file_qos_summary
  get_qos_rule_detail     →  get_qos_requirement    (added 2026-05-02 — P6)
  set_qos_rule            →  add_file_qos_requirement
  list_transfers          →  list_space_transfers
  (added)                 →  get_transfer          (needed for P4: list returns IDs only)

## 2026-05-02 — promotion of get_qos_requirement from ABLATION_EXTRAS

P6 ("find files whose effective QoS requires only 1 replica") cannot be
answered with `get_file_qos_summary` alone — its response shape is
`{requirements: {qosId: status}}`, with NO `replicas_num` field. The
per-rule details (replicas_num, expression) require
`get_qos_requirement(qosId)`. Without it in HEADLINE, all three K=1
panel LLMs converged on the same wrong answer (filename heuristics
returning `redundant.bin` only). Promoted to HEADLINE; len now 16.
Round-number reduction is acceptable trade for measurement validity.
"""

from __future__ import annotations

# The headline 16-tool benchmark allowlist. Order is presentation-only; the
# harness consumes this as a frozenset for tools/list filtering.
HEADLINE: frozenset[str] = frozenset(
    {
        # R1 — namespace and content I/O (6, full CRUD: list / read / create / move / delete)
        "list_user_spaces",
        "list_files_recursively",
        "download_file",
        "create_file",
        "move_file",
        "delete_file",
        # R2 — custom metadata (3)
        "get_file_metadata",
        "set_file_metadata",
        "query_by_metadata",
        # R3 — provider topology (1)
        "list_space_providers",
        # R4 — block-level distribution (1)
        "get_file_distribution",
        # R5 — QoS expressions (3 — get_qos_requirement promoted 2026-05-02 for P6)
        "get_file_qos_summary",
        "get_qos_requirement",
        "add_file_qos_requirement",
        # R6 — transfers (2)
        "list_space_transfers",
        "get_transfer",
    }
)

assert len(HEADLINE) == 16, f"HEADLINE must have exactly 16 tools, got {len(HEADLINE)}"


# Tools registered by the MCP server but **excluded** from the headline:
# - kept in the codebase per "extend M0rgho, don't replace" directive
# - excluded both from headline and from the full ablation surface
#   (the federation has no harvester, so they always fail)
EXCLUDED_HARVESTER: frozenset[str] = frozenset(
    {
        "list_user_harvesters",
        "get_harvester_index_schema",
        "query_harvester_index",
    }
)

# Tools available in the **full ablation surface** but not in the headline.
# These represent navigation / convenience operations the curated headline
# deliberately collapses, to keep the surface within the small-N tool-
# selection regime the paper argues for.
#   list_marketplace_spaces  — orthogonal space-discovery via marketplace
#   list_children            — shallow listing; list_files_recursively at depth=1 covers it
#   get_file_id              — path → fileId; polymorphic input on every other tool collapses this
#   get_file_attributes      — per-file attrs; same fields in list_files_recursively batch response
#   grep_file_content        — convenience over download_file
#   get_qos_requirement      — per-id detail; qos_summary covers benchmark needs
#   remove_qos_requirement   — not exercised by any of the 18 scenarios
ABLATION_EXTRAS: frozenset[str] = frozenset(
    {
        "list_marketplace_spaces",
        "list_children",
        "get_file_id",
        "get_file_attributes",
        "grep_file_content",
        "remove_qos_requirement",
        # Added 2026-05-02 (M-11): explicit mkdir for the
        # create_file-with-empty-content trap. Headline contract preserved
        # (the create_file defensive error already prevents the trap on
        # the headline surface); create_directory is the canonical
        # alternative and is exposed in the ablation surface so the
        # explicit-mkdir path can be measured separately.
        "create_directory",
        # Added 2026-07-05 (issue #8): agent-triggerable replication transfer
        # (wraps POST /transfers). Requested by the ICSOC 2026 fleet for their
        # transfer-convergence scenarios; not part of the frozen PPAM HEADLINE
        # 16, so it lives in the ablation surface.
        "schedule_file_replication",
    }
)

# Sanity: the three sets must be disjoint.
assert HEADLINE.isdisjoint(EXCLUDED_HARVESTER)
assert HEADLINE.isdisjoint(ABLATION_EXTRAS)
assert EXCLUDED_HARVESTER.isdisjoint(ABLATION_EXTRAS)


# The full ablation surface = headline + extras (excluding always-failing harvesters).
ABLATION_FULL: frozenset[str] = HEADLINE | ABLATION_EXTRAS
