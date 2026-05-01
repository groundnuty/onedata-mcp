# Implementation notes — PPAM 2026 MCP server fork

**Branch:** `ppam2026/14-tools` of `groundnuty/onedata-mcp` (fork of `M0rgho/onedata-mcp`).
**Pinned target:** Onedata 25.0 (federation deployed at `data.spice-platform.eu` reports `version: 25.0`, build `46-g14b5bda7`).
**Swagger refs used during implementation:** `oneprovider-swagger@25.0` (commit `39da981`), `onezone-swagger@25.0` (commit `58c6976`), `onepanel-swagger@25.0` (commit `aa17c67`). All three repos cloned to `/Users/orzech/repos/onedata/`.

## Summary

The fork adds 6 MCP tools to M0rgho's existing surface, extending it from his metadata-and-harvester focus to the QoS / distribution / transfers / cross-provider topology that the PPAM 2026 benchmark exercises. The 6 added tools are:

1. `get_file_distribution` (file-level)
2. `move_file` (file-level, **stubbed** — see §Move file below)
3. `list_space_providers` (space-level, queries oneprovider not onezone)
4. `get_file_qos_summary` (QoS, new module)
5. `add_file_qos_requirement` (QoS, new module)
6. `list_space_transfers` (transfers, new module)

A seventh tool, `get_transfer`, is also exposed because the list endpoint returns IDs only (the agent needs a follow-up to see source/destination/state/bytes).

A QoS module helper (`get_qos_requirement`, `remove_qos_requirement`) is included for completeness of the QoS lifecycle, even though the headline benchmark only exercises `add` + `summary`.

## Module layout (added to M0rgho's existing layered architecture)

```
onedata_mcp/
├── api/                          # REST clients (httpx async)
│   ├── files.py        EXTENDED  # +get_file_distribution, +move_file (stub)
│   ├── spaces.py       EXTENDED  # +get_space_providers (oneprovider-side)
│   ├── qos.py          NEW       # 4 functions
│   └── transfers.py    NEW       # 2 functions
├── modules/                      # FastMCP tool registrations
│   ├── files.py        EXTENDED  # +mcp_get_file_distribution, +mcp_move_file
│   ├── spaces.py       EXTENDED  # +mcp_list_space_providers
│   ├── qos.py          NEW       # 4 tools
│   └── transfers.py    NEW       # 2 tools
└── main.py             EXTENDED  # imports + registers qos + transfers modules
```

## Endpoint mapping (verified against 25.0 swagger)

| Tool                            | HTTP | Path                                               | Service        | operationId               |
|---------------------------------|------|----------------------------------------------------|----------------|---------------------------|
| `get_file_distribution`         | GET  | `/data/{file_id}/distribution`                     | oneprovider    | `get_data_distribution`   |
| `get_file_qos_summary`          | GET  | `/data/{file_id}/qos/summary`                      | oneprovider    | `get_file_qos_summary`    |
| `add_file_qos_requirement`      | POST | `/qos_requirements`  (top-level, fileId in body!)  | oneprovider    | `add_qos_requirement`     |
| `get_qos_requirement`           | GET  | `/qos_requirements/{qos_id}`                       | oneprovider    | `get_qos_requirement`     |
| `remove_qos_requirement`        | DEL  | `/qos_requirements/{qos_id}`                       | oneprovider    | `remove_qos_requirement`  |
| `list_space_transfers`          | GET  | `/spaces/{sid}/transfers`                          | oneprovider    | `get_all_transfers`       |
| `get_transfer`                  | GET  | `/transfers/{tid}`                                 | oneprovider    | `get_transfer`            |
| `list_space_providers`          | GET  | `/spaces/{sid}`                                    | **oneprovider**| `get_space`               |
| `move_file`                     | —    | (no public endpoint)                               | —              | —                         |

### Three corrections vs. paper §3 spec

The paper-writing agent should update Table 1 / prose accordingly:

1. **`get_file_qos_summary` path is `/qos/summary` (slash), not `/qos_summary` (underscore).** Spec §3.6 had it wrong.
2. **`add_qos_requirement` posts to top-level `/qos_requirements`, with `fileId` in the JSON body** — not to a per-file `/data/{file_id}/qos_requirements` URL. Spec §3.7 had it wrong.
3. **`list_space_transfers` state filter values are `waiting | ongoing | ended`** — not `completed | failed | all`. Spec §3.8 had it wrong.

## Move file: known gap

Onedata 25.0 has **no public REST endpoint for move/rename** (verified by grep against `oneprovider-swagger@25.0`). `api/files.py::move_file` currently raises `NotImplementedError`. Three candidate strategies and the deferral rationale are tracked in **[`design/01-move-file-strategy.md`](design/01-move-file-strategy.md)**. Decision lands after first live smoke pass.

## QoS expression error handling (paper §5.4 H_qos_syntax metric)

The `add_file_qos_requirement` tool deliberately surfaces the server's structured error to the agent on a malformed expression. Common failure mode: Python-trained models writing `geo==PL` (double equals) when the DSL accepts only `=`. The `OnedataApiError` exposes `errno`, `error_id`, and the description, so the MCP layer can return a self-correctable message — see `test_add_qos_requirement_surfaces_api_error_for_invalid_expression` in `test/unit/api/test_qos.py`.

## Provider geo / storage classes

`list_space_providers` calls **oneprovider**'s `/spaces/{sid}` endpoint, which returns canonical `(providerId, providerName)` pairs but no geographic or storage-class attributes. Those live on per-provider QoS metadata configured by site admins. To enrich, a follow-up call to `onezone /providers/{id}` is required. For the headline benchmark we do not enrich automatically — the agent makes a second call when geo is needed (this matches the spec §6 Discovery task D2, "List all providers... For each, report its country").

If the deployment's `-spice-v1` patch on onezone changes the response shape of `/providers/{id}`, surface that in `IMPLEMENTATION_NOTES.md` after first contact (task #23 in the conversation tracker).

## query_by_metadata strategy

**Decided:** recursive primitives. New tool `query_by_metadata` lives in `onedata_mcp/api/metadata.py`, composing `list_files_recursively` + `get_file_metadata` with a small `key=value` / `key=*` predicate parser. Bounded by `max_depth`, `max_results`, and an internal `MAX_FILES_VISITED=1000` hard cap. Returns `truncated: true` whenever any cap fires.

Harvester-based tools (`query_harvester_index`, `get_harvester_index_schema`, `list_user_harvesters`) inherited from M0rgho stay in the codebase and remain callable, but are excluded from the PPAM headline 14-tool benchmark allowlist.

Full rationale and tradeoffs: **[`design/02-query-by-metadata-no-harvester.md`](design/02-query-by-metadata-no-harvester.md)**.

## Tool count for paper Table 1

The fork now exposes ~21 MCP tools (M0rgho's 14 + 7 we added: 6 from the spec plus the recursive `query_by_metadata`). The PPAM benchmark will use the harness's `tool_context_mode={"full","minimal"}` to:

- run a **headline** sweep with a curated 14-tool allowlist (matching paper's Table 1 claim, harvesters excluded)
- run an **ablation** sweep with the full ~21-tool surface

This strengthens the paper's curation argument rather than weakening it. The exact 14-tool allowlist is defined in the benchmark scenario set (task #18, deferred until scenario authoring).

## Testing

Unit tests for the 5 new/extended `api/` modules pass (60/60 total, including 33 pre-existing M0rgho tests):

```bash
uv run pytest test/unit -v
```

Tests use `pytest-httpx` to mock all HTTP traffic; no live federation contact needed. Schemas in tests are pinned to the Onedata 25.0 swagger response shapes (referenced by file in each test's docstring).

The `mcp_smoke.py` script (task #13, deferred) will exercise each tool once against the live federation before each benchmark batch.

## Versions and pinning

- **Federation:** Onedata 25.0 (verified live 2026-04-30, see `/api/v3/onezone/configuration` response).
- **Swagger refs:** all three swagger repos checked out to tag `25.0`. Diff vs `develop` tip on the 6 endpoint files + 4 definitions: zero lines.
- **Onezone patch:** `onedata/onezone:ID-ba7a778696-spice-v1` runs a SPICE-specific patch over 25.0 base. Possible behaviour deviation on onezone-side endpoints (auth, identity, provider details). Tracked as conversation task #23.
- **Federation health:** at the time of writing only 1 of 3 advertised oneproviders responded (`cloud-sk` OK; `edge` and `cloud` returned "no available server"). Tracked as task #24. Does not block scaffolding; will block the benchmark sweep.
