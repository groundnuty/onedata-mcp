# Design decision: which base implementation to build on

**Status:** decided 2026-04-30.
**Outcome:** `groundnuty/onedata-mcp` builds on a pre-existing GitHub Onedata MCP implementation (the "base surface"), with the GitLab original (`gitlab.spice-platform.eu/work-packages/wp6/onedata-mcp`) treated as informational reference only.

## Two candidate starting points

| | `gitlab.spice-platform.eu/work-packages/wp6/onedata-mcp` | GitHub base implementation |
|---|---|---|
| Origin | The repo named in the paper spec (`research/22-mcp-implementation-spec.md` §2) | A separate, more recent re-implementation |
| Last commit before fork | 2026-04-23 (`Refactored and added unit tests`) | 2026-04-30 |
| Tools shipped | 6 (file CRUD only) | ~14 (file CRUD + harvesters + spaces marketplace + per-file attrs/metadata) |
| MCP framework | raw `mcp.server.Server` | **FastMCP** with typed `Field`/`ToolAnnotations` |
| HTTP | sync via `OnedataFileRESTClient` | **async `httpx`** (concurrent calls possible) |
| Layout | one ~450-line `server.py` | layered: `api/` (REST clients), `modules/` (MCP wiring), `utils.py` (typed errors + `request()`) |
| Errors | bare `except Exception → str()` | `OnedataApiError` / `OnedataPathNotFoundError` / `OnedataInvalidSpaceError` with structured `errno` + `error_id` |
| Path/ID handling | path-only | path-or-fileId polymorphic on every tool, plus MCP-roots support |
| Auth surface | onezone host only | onezone *and* oneprovider with separate hosts/tokens |
| TLS | hardcoded `verify_ssl=False` | secure default, opt-in `ONEDATA_ALLOW_INSECURE_TLS=true` |
| Helpful errors | none | space-not-supported error fetches available-space list and embeds it |
| Live PLGrid Forge harness | absent | already wired (`test/plgrid/forge_harness.py`) — directly relevant to the paper benchmark |

## Why the GitHub base implementation

**Engineering quality + scope reduction.** Building on it takes our delta from "implement 8 new tools + harness from scratch" (~6 weeks) down to "implement 6 new tools + extend the existing harness" (~2-3 weeks). The framework work — async HTTP, typed errors, layered architecture, FastMCP wiring, OpenAI-translation bridge — is already done well. Re-doing it would have been busywork.

**The PLGrid Forge harness is a freebie.** The base surface's `test/plgrid/forge_harness.py` already implements LLM-driven tool dispatch with metric collection (`tool_call_count`, `forge_loop_wall_time_ms`, `forge_token_usage_totals`, `chat_messages_stats`, `tools_in_context_count`) — exactly the paper's metrics list. Two `tool_context_mode` settings ("full" / "minimal") map onto the paper's §5.6 ablation. We extend it to a multi-LLM panel (#19); we don't write it.

**Naming convention is API-faithful.** The base surface's `download_file` / `delete_file` / `get_file_metadata` map directly onto Onedata REST operationIds (`download_file_content`, `remove_file`, getting metadata via `data/{id}/metadata/{type}`). The paper's `read_file` / `remove_file` / `get_metadata` are paper-internal aliases that drift from the actual API. Keeping the base names means the paper-writing agent edits Table 1, but the agent's tool surface stays honest to the substrate.

## Why NOT the GitLab original

- **Drops the framework work.** We'd reimplement async HTTP, typed errors, layered architecture — none of which is paper-relevant work.
- **Drops the Forge harness.** We'd build it from scratch. ~1 week of effort that already exists in the base surface.
- **The 6-tool baseline is the *spec's* baseline, not a delivered baseline.** The paper §3 curation argument doesn't hinge on the historical 6-tool number; it hinges on the 14/15-tool curated set and what it omits from the ~400-endpoint REST API.

## Hybrid (option C, rejected)

Fork GitLab + port the base framework piece-by-piece. Worst of both: re-do the framework work *and* carry the porting cost. Rejected outright.

## Costs we accepted

- **Diverging from the spec text.** `research/22-mcp-implementation-spec.md` (in the paper repo) names the GitLab repo. We diverged from that. Mitigation: the spec is a reading aid, not an authoritative deliverable; the paper itself doesn't name a specific repo.
- **Naming churn for the paper.** Table 1 needs ~7 row updates (paper-name → server name). Documented in `design/03-tool-allowlist-curation.md` and `IMPLEMENTATION_NOTES.md`. Estimated editorial cost: ~1 hour for the paper-writing agent.

## Cross-references

- GitLab original: `ssh://git@gitlab.spice-platform.eu:7999/work-packages/wp6/onedata-mcp.git`
- Our repo: <https://github.com/groundnuty/onedata-mcp> branch `ppam2026/14-tools`
- IMPLEMENTATION_NOTES.md — overall current state
- `design/03-tool-allowlist-curation.md` — naming convention rationale
