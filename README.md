# Onedata MCP Server (PPAM 2026 fork)

An [MCP](https://modelcontextprotocol.io/) server that connects assistants to [Onedata](https://onedata.org/) (Onezone + Oneprovider): spaces, harvesters, files, **QoS, distribution, providers, transfers**.

`groundnuty/onedata-mcp` extends a pre-existing Onedata MCP base surface with seven federation-state tools (six per paper spec, plus a recursive `query_by_metadata` that needs no harvester) for the PPAM 2026 *LLM-agentic access to a federated scientific data layer with Onedata* benchmark. Pinned to **Onedata 25.0** swagger. The headline benchmark uses a curated 16-tool allowlist defined in [`benchmark/tool_allowlist.py`](benchmark/tool_allowlist.py); see [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) for endpoint mapping and the three corrections vs. paper §3 spec.

## Tool surface

The server registers **27 tools** in total (the table below). Three lenses matter:

- **Maturity — 16 `stable` / 11 `experimental`.** `stable` tools were validated against the PPAM 7-LLM K=8 panel (== the HEADLINE benchmark allowlist); `experimental` tools are registered but not yet panel-swept — either added after the paper freeze, or inherited from the upstream base surface and never validated against our panel. See [`onedata_mcp/tool_maturity.py`](onedata_mcp/tool_maturity.py). Origin (`ours`/`upstream`) is a provenance signal — note that panel-validation trumps origin: an upstream tool that survived K=8 is `stable`.
- **Read-only token — 19 tools.** Running with a `data.readonly` Onedata token prunes the 8 mutating tools (see [`onedata_mcp/token_policy.py`](onedata_mcp/token_policy.py)).
- **HEADLINE benchmark allowlist — 16 tools.** The subset the PPAM benchmark restricts the agent to (`benchmark/tool_allowlist.py::HEADLINE`); identical to the `stable` set. The benchmark curates *down* to 16 — it does not cap what the server exposes.

You can restrict the exposed surface at launch — see [Selecting tools at launch](#selecting-tools-at-launch). The branch name (`14-tools`) is historical; the authoritative live count is what `tools/list` returns (27).

`Mat.` column: `stable` = panel-validated; `exp·up` = experimental, inherited from upstream (the genuine distrust set); `exp·ours` = experimental, added by this fork post-paper.

| Tool                          | Group         | Mat.      | Notes                                                |
|-------------------------------|---------------|-----------|------------------------------------------------------|
| `list_user_spaces`            | spaces        | stable    |                                                      |
| `list_marketplace_spaces`     | spaces        | exp·up    |                                                      |
| `list_space_providers`        | spaces        | stable    | **NEW** — providers from oneprovider's `/spaces/{sid}` |
| `get_file_id`                 | files         | exp·up    |                                                      |
| `get_file_attributes`         | files         | exp·up    |                                                      |
| `list_children`               | files         | exp·up    |                                                      |
| `list_files_recursively`      | files         | stable    |                                                      |
| `download_file`               | files         | stable    |                                                      |
| `grep_file_content`           | files         | exp·up    |                                                      |
| `create_file`                 | files         | stable    |                                                      |
| `create_directory`            | files         | exp·ours  | **NEW** — explicit directory creation (finding M-11) |
| `delete_file`                 | files         | stable    |                                                      |
| `move_file`                   | files         | stable    | **NEW** — CDMI (`PUT /cdmi/{dst_space}/{path}`); intra-space only |
| `get_file_metadata`           | files         | stable    | json / rdf / xattrs                                  |
| `set_file_metadata`           | files         | stable    | json / rdf / xattrs                                  |
| `get_file_distribution`       | files         | stable    | **NEW** — per-provider, per-storage block ranges     |
| `get_file_qos_summary`        | qos           | stable    | **NEW**                                              |
| `add_file_qos_requirement`    | qos           | stable    | **NEW** — async; returns ID, replication eventual    |
| `get_qos_requirement`         | qos           | stable    | **NEW** — detail by ID                               |
| `remove_qos_requirement`      | qos           | exp·ours  | **NEW** — not in the panel-validated HEADLINE set    |
| `list_space_transfers`        | transfers     | stable    | **NEW** — IDs only; pair with `get_transfer`         |
| `get_transfer`                | transfers     | stable    | **NEW**                                              |
| `schedule_file_replication`   | transfers     | exp·ours  | **NEW** — schedule a replication transfer to a target provider |
| `list_user_harvesters`        | harvesters    | exp·up    |                                                      |
| `get_harvester_index_schema`  | harvesters    | exp·up    |                                                      |
| `query_harvester_index`       | harvesters    | exp·up    | excluded from headline benchmark allowlist           |
| `query_by_metadata`           | metadata      | stable    | **NEW** — recursive predicate evaluator (no harvester) |

### Selecting tools at launch

Two env vars restrict which tools the server exposes (model-invisible; they prune the registered surface, they do not alter any tool). With neither set, all 27 are exposed.

| Env var | Effect |
| ------- | ------ |
| `ONEDATA_MCP_MATURITY` | Comma-separated maturity tiers to expose. `stable` → only the 16 panel-validated tools; `stable,experimental` (or unset) → all 27. |
| `ONEDATA_MCP_TOOLS` | Comma-separated explicit tool allowlist (e.g. `list_user_spaces,download_file`). Takes precedence over `ONEDATA_MCP_MATURITY`; unknown names are ignored with a warning. |

```bash
# Expose only the panel-validated set (conservative deployment):
ONEDATA_MCP_MATURITY=stable uv run onedata-mcp
```

This composes with the read-only-token prune: both simply hide tools, so a read-only token + `ONEDATA_MCP_MATURITY=stable` yields the stable-and-non-mutating intersection.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) (recommended)

## Install

```bash
uv sync
```

## Environment variables

Configuration is loaded from the process environment. [python-dotenv](https://pypi.org/project/python-dotenv/) is used so a `.env` file in the **current working directory** is picked up when the server starts.

1. Copy the example file and edit values:

   ```bash
   cp .env.example .env
   ```

2. Set the variables below (see `.env.example` for placeholders).

### Onedata API (required for live calls)

| Variable | Description |
| -------- | ----------- |
| `ONEDATA_ONEZONE_HOST` | Onezone base URL, e.g. `https://your-onezone.example` (no `/api/...` suffix). |
| `ONEDATA_ONEZONE_TOKEN` | Token sent as `X-Auth-Token` to Onezone. |
| `ONEDATA_ONEPROVIDER_HOST` | Oneprovider base URL, e.g. `https://your-oneprovider.example`. |
| `ONEDATA_ONEPROVIDER_TOKEN` | Token sent as `X-Auth-Token` to Oneprovider. |
| `ONEDATA_ALLOW_INSECURE_TLS` | Set to `true` only if you must use HTTPS with self-signed or otherwise unverifiable certificates (default: verify TLS). |

### Server / logging (optional)

| Variable | Description |
| -------- | ----------- |
| `FASTMCP_LOG_LEVEL` | Logging level (default: `INFO`). |
| `FASTMCP_LOG_FILE` | If set, logs are also appended to this file path. |

## Run the MCP server (stdio)

This is the usual mode for desktop clients (Cursor, Claude Desktop, etc.):

```bash
uv run onedata-mcp
```

Ensure the client runs the command from a directory where your `.env` exists, **or** export the same variables in the environment before starting the server.

### Cursor example (`mcp.json`)

Adjust the path to your checkout:

```json
{
  "mcpServers": {
    "onedata": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/onedata-mcp",
        "onedata-mcp"
      ],
      "env": {
        "ONEDATA_ONEZONE_HOST": "https://your-onezone.example",
        "ONEDATA_ONEZONE_TOKEN": "your-token",
        "ONEDATA_ONEPROVIDER_HOST": "https://your-oneprovider.example",
        "ONEDATA_ONEPROVIDER_TOKEN": "your-token"
      }
    }
  }
}
```

You can omit `env` here and rely on a `.env` file next to the project if the server process starts with that working directory.

### MCP Inspector

```bash
uv run fastmcp dev inspector onedata_mcp/main.py:mcp
```

## Telemetry (OpenTelemetry)

The server can emit one OpenTelemetry span per MCP tool call (tool name,
duration, success/error status, error class on failure) and continues an
incoming W3C `traceparent`, so a client-initiated trace flows
client → MCP server → Onedata REST as a single correlated trace.

**Telemetry is opt-in and fully no-op by default.** With none of the `OTEL_*`
variables set, no exporter is installed — there is no startup cost and no
retry spam without a collector. Enable it purely through the standard `OTEL_*`
environment variables (nothing is hardcoded):

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://<your-collector-host>:4318"  # OTLP/HTTP
export OTEL_SERVICE_NAME="onedata-mcp"
# optional: export OTEL_RESOURCE_ATTRIBUTES="deployment.environment=dev"
uv run onedata-mcp
```

Any OTLP/HTTP collector works (e.g. an `otel/opentelemetry-collector` container
exposing `:4318`). Trace-context propagation: a client that places a
`traceparent` in the MCP request `_meta` will have the server's spans parented
to that trace.

## Development

- Format / lint: `uv run ruff format`
- Tests: `uv run pytest`

See `AGENTS.md` for repository conventions.
