# Design decision: `query_by_metadata` without harvesters

**Status:** decided 2026-04-30.
**Tool name:** `query_by_metadata(space, predicate, path='/', max_depth=5, max_results=50)` (new, in `onedata_mcp/api/files.py` or a new `onedata_mcp/api/metadata.py`).
**Decision:** **Strategy (c) recursive primitives. No harvesters.**

## Context

The PPAM 2026 implementation spec (`research/22-mcp-implementation-spec.md` §3.9.3) named three theoretically possible strategies:

1. **(a) Onedata harvester + OpenSearch** — agent-friendly query through a managed index.
2. **(b) CouchDB-style `createView` + `queryView`** — agent writes a JavaScript map function. Rejected at spec time as not agent-friendly.
3. **(c) Recursive `list_files_recursively` + per-file `get_file_metadata` + client-side filter** — no special infra, depth- and result-bounded.

M0rgho already shipped strategy (a) under the names `query_harvester_index` / `get_harvester_index_schema` / `list_user_harvesters`.

## Why (c)

**User directive 2026-04-30:** *"we want to stay away from harvesters — the simpler metadata api the better"*.

This aligns with the paper's curation thesis. The argument the paper makes against bare wrappers (Mastouri 92% / 19%, Song 9.5%/3.25–236.5×) is that **a curated MCP surface should expose the operation classes the agent reasons over, not the full REST surface**. Harvesters are a *separate Onedata subsystem* with its own indices, schemas, and admin lifecycle. Putting them on the agent's path means:

- The agent must first *discover* the relevant harvester (`list_user_harvesters`) and the right index (`get_harvester_index_schema`) before it can query — three tool calls before the actual question.
- The harvester schema is plugin-specific (the swagger says *"plugin-specific query payload"*), so the agent has to reason about a query DSL it has no canonical training distribution for — exactly the failure mode the paper flags as out-of-distribution operationIds.
- Harvesters are **admin-installed**. The benchmark space on `data.spice-platform.eu` has no harvester configured at the time of writing. The strategy fails for our deployment as a hard precondition.

The recursive strategy (c), by contrast, composes from primitives every Onedata deployment exposes (`list_files_recursively` + `get_file_metadata`), keeps the agent's reasoning in the same conceptual space as the rest of the surface, and **bounds cost by construction** (depth and result caps).

## Implementation

```
async def query_by_metadata(
    space: str,
    predicate: str,
    *,
    path: str = "/",
    max_depth: int = 5,
    max_results: int = 50,
) -> dict[str, Any]:
    """Find files matching a `key=value` (or `key=*`) predicate.
    Multiple predicates joined by '&' are AND'ed.

    Returns: {"matches": [{path, fileId, matched_keys}, ...],
              "truncated": bool, "files_visited": N}.
    """
```

**Predicate grammar (deliberately small):**

- `key=value` — exact match on JSON metadata at top level
- `key=*` — key present, any value
- `&`-joined chain — all clauses must hold (AND semantics)
- No `|` (OR), no nesting, no ranges, no LIKE — the agent is expected to issue separate queries

**Bounds:**

- `max_depth=5` (default): how deep to recurse from `path`
- `max_results=50` (default): cap on returned matches
- Plus an internal `MAX_FILES_VISITED=1000` so a deep tree without matches still terminates

If either bound is hit, the result includes `"truncated": true` so the agent knows the answer may be partial. Documented in the tool description so this is visible to the LLM.

**Cost note (paper §3 token economics):** worst case is `max_depth × max_files_per_dir + max_results × 1` HTTP calls. The harness's `chat_messages_stats` instruments per-call cost, so post-hoc analysis can flag tasks where `query_by_metadata` is a hot loop.

## What stays of M0rgho's harvester surface

We extend, not replace (per user directive 2026-04-30). M0rgho's `list_user_harvesters`, `get_harvester_index_schema`, `query_harvester_index` remain registered and callable. They are:

- ❌ **excluded from the `tool_context_mode="minimal"` 14-tool benchmark allowlist** (the headline benchmark)
- ❌ **excluded from the `tool_context_mode="full"` ablation surface as well** — the SPICE federation has no harvester configured (confirmed 2026-04-30 by user). The harvester tools would always fail on this deployment, so they would degrade the ablation rather than enrich it.

The 14-tool allowlist is defined in the benchmark scenario set (task #18, separate workstream).

## Cross-references

- Paper spec: `papers/ppam-2026/research/22-mcp-implementation-spec.md` §3.3, §3.9.3
- Paper draft: `papers/ppam-2026/paper.tex` Table 1 row `query_by_metadata`, §3 curation argument
- Implementation notes: `IMPLEMENTATION_NOTES.md` §"query_by_metadata strategy"
- Memory: `feedback_metadata_simpler_than_harvester.md` (user's preference, 2026-04-30)
