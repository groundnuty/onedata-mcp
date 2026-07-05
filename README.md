# Onedata MCP Server (PPAM 2026 fork)

An [MCP](https://modelcontextprotocol.io/) server that connects assistants to [Onedata](https://onedata.org/) (Onezone + Oneprovider): spaces, harvesters, files, **QoS, distribution, providers, transfers**.

This fork (branch `ppam2026/14-tools` of `groundnuty/onedata-mcp`) extends [`M0rgho/onedata-mcp`](https://github.com/M0rgho/onedata-mcp) with seven federation-state tools (six per paper spec, plus a recursive `query_by_metadata` that needs no harvester) for the PPAM 2026 *LLM-agentic access to a federated scientific data layer with Onedata* benchmark. Pinned to **Onedata 25.0** swagger. The headline benchmark uses a curated 16-tool allowlist defined in [`benchmark/tool_allowlist.py`](benchmark/tool_allowlist.py); see [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) for endpoint mapping and the three corrections vs. paper §3 spec.

## Tool surface

The server registers **26 tools** in total (the table below). Two subsets matter:

- **19 tools** remain when the server is run with a read-only Onedata token (a `data.readonly` caveat prunes the 7 mutating tools — see [`onedata_mcp/token_policy.py`](onedata_mcp/token_policy.py)).
- **16 tools** form the curated **HEADLINE** benchmark allowlist (`benchmark/tool_allowlist.py::HEADLINE`) — the subset the PPAM benchmark restricts the agent to. This is distinct from the registered surface: the benchmark curates *down* to 16, it does not cap what the server exposes.

The branch name (`14-tools`) is historical — the surface has since grown; the authoritative live count is what `tools/list` returns (26).

| Tool                          | Group         | Notes                                                |
|-------------------------------|---------------|------------------------------------------------------|
| `list_user_spaces`            | spaces        |                                                      |
| `list_marketplace_spaces`     | spaces        |                                                      |
| `list_space_providers`        | spaces        | **NEW** — providers from oneprovider's `/spaces/{sid}` |
| `get_file_id`                 | files         |                                                      |
| `get_file_attributes`         | files         |                                                      |
| `list_children`               | files         |                                                      |
| `list_files_recursively`      | files         |                                                      |
| `download_file`               | files         |                                                      |
| `grep_file_content`           | files         |                                                      |
| `create_file`                 | files         |                                                      |
| `create_directory`            | files         | **NEW** — explicit directory creation (finding M-11) |
| `delete_file`                 | files         |                                                      |
| `move_file`                   | files         | **NEW** — CDMI (`PUT /cdmi/{dst_space}/{path}`); intra-space only |
| `get_file_metadata`           | files         | json / rdf / xattrs                                  |
| `set_file_metadata`           | files         | json / rdf / xattrs                                  |
| `get_file_distribution`       | files         | **NEW** — per-provider, per-storage block ranges     |
| `get_file_qos_summary`        | qos           | **NEW**                                              |
| `add_file_qos_requirement`    | qos           | **NEW** — async; returns ID, replication eventual    |
| `get_qos_requirement`         | qos           | **NEW** — detail by ID                               |
| `remove_qos_requirement`      | qos           | **NEW**                                              |
| `list_space_transfers`        | transfers     | **NEW** — IDs only; pair with `get_transfer`         |
| `get_transfer`                | transfers     | **NEW**                                              |
| `list_user_harvesters`        | harvesters    |                                                      |
| `get_harvester_index_schema`  | harvesters    |                                                      |
| `query_harvester_index`       | harvesters    | excluded from headline benchmark allowlist           |
| `query_by_metadata`           | metadata      | **NEW** — recursive predicate evaluator (no harvester) |

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
