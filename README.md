# Onedata MCP Server

An [MCP](https://modelcontextprotocol.io/) server that gives AI agents access to an [Onedata](https://onedata.org/) data federation. An agent can:

- find spaces, providers and files, and read and write data and metadata;
- see which provider physically holds which blocks of a file;
- place data with QoS requirements, and schedule and follow transfers;
- check, or make sure, that the data an analysis needs are complete and readable at its provider.

It talks to the Onezone and Oneprovider REST APIs of **Onedata 25.0**.

## Tools

The server registers 23 tools by default. **Access** `W` marks the 9 tools that change state in the federation. **Maturity** is explained [below](#maturity).

| Tool | What it does | Access | Maturity |
|---|---|---|---|
| **Spaces and providers** | | | |
| `list_user_spaces` | List the spaces the user belongs to | R | stable |
| `list_space_providers` | List the providers supporting a space (by name or id) | R | stable |
| **Files and directories** | | | |
| `list_files_recursively` | List all files under a directory | R | stable |
| `list_children` | List the direct children of a directory | R | experimental |
| `get_file_attributes` | Read a file's attributes (size, type, replication state, …) | R | experimental |
| `download_file` | Read a file's content | R | stable |
| `create_file` | Create a file with given content | W | stable |
| `create_directory` | Create a directory | W | experimental |
| `move_file` | Move or rename a file or directory within a space | W | stable |
| `delete_file` | Delete a file, or a directory recursively | W | stable |
| **Metadata** | | | |
| `get_file_metadata` | Read JSON, RDF or extended-attribute metadata | R | stable |
| `set_file_metadata` | Write JSON, RDF or extended-attribute metadata | W | stable |
| `query_by_metadata` | Find files whose JSON metadata matches a predicate (no harvester needed) | R | stable |
| **Distribution, QoS and transfers** | | | |
| `get_file_distribution` | Show which blocks of a file each provider and storage holds | R | stable |
| `get_file_qos_summary` | Show the QoS requirements on a file and whether they are met | R | stable |
| `add_file_qos_requirement` | Add a QoS requirement, e.g. "keep a replica at provider X" | W | stable |
| `get_qos_requirement` | Read one QoS requirement | R | stable |
| `remove_qos_requirement` | Remove a QoS requirement | W | experimental |
| `schedule_file_replication` | Copy a file's data to a given provider | W | experimental |
| `list_space_transfers` | List the transfers in a space | R | stable |
| `get_transfer` | Read one transfer: type, destination, bytes moved, timing | R | stable |
| **Data readiness** | | | |
| `check_readiness` | Are the declared data complete and readable at this provider, and if not, why | R | experimental |
| `ensure_readiness` | Bring the declared data here, wait, and keep them here | W | experimental |

The live list is whatever `tools/list` returns; `make tools-manifest` prints it as JSON. Eight more tools are available as [preview features](#choosing-which-tools-to-expose).

### Maturity

- **stable**: validated with several LLMs against a live Onedata deployment.
- **experimental**: implemented and unit-tested, but not yet validated that way; it may still change.

The classification lives in [`onedata_mcp/tool_maturity.py`](onedata_mcp/tool_maturity.py). How a tool moves from experimental to stable, and when it moves back, is set out in [`design/10-tool-maturity.md`](design/10-tool-maturity.md).

### Readiness tools

`check_readiness` and `ensure_readiness` answer one question: are the data an analysis declares complete and readable at the provider this server talks to? The agent declares them as an `extent`: per dataset (a space name or a directory path), optionally file names, or inclusive `YYYY-MM-DD` date ranges matched against the `YYYYMMDD` in each file name.

- `check_readiness` is read-only. It answers ready, or not ready with a reason per group of files:
  - `being_written`: no other provider holds the whole file yet;
  - `at_other_site`;
  - `in_transfer`;
  - `unreachable`: the file is only at a provider that is down.
- `ensure_readiness` places one QoS requirement per file for this provider (`providerId=<local provider> & anyStorage`). Onedata then copies the file here and protects the replica from eviction. The tool waits up to `deadline_s`; one call waits at most 240 s (`ONEDATA_MCP_ENSURE_MAX_WAIT_S` changes the cap). Call it again if the answer is not ready.

A file counts as ready only when this provider holds all of it, and either another provider does too or the file was written here. A transfer's status or a QoS requirement's `fulfilled` is never taken as proof. Design: [`design/07-readiness-tools.md`](design/07-readiness-tools.md).

## Choosing which tools to expose

The settings below only hide tools: none changes how a tool behaves or what it says to the model. They combine; for example, a read-only token plus `ONEDATA_MCP_MATURITY=stable` exposes the stable tools that do not write.

| Setting | Effect |
|---|---|
| `ONEDATA_MCP_MATURITY` | Comma-separated tiers to expose: `stable` or `stable,experimental`. Unset exposes all tools. |
| `ONEDATA_MCP_TOOLS` | Comma-separated explicit list of tool names. Takes precedence over `ONEDATA_MCP_MATURITY`; unknown names are ignored with a warning. |
| A read-only Onedata token | A Oneprovider token with the `data.readonly` caveat hides the 9 `W` tools automatically. |

```bash
# Only the stable tools:
ONEDATA_MCP_MATURITY=stable uv run onedata-mcp
```

**Preview features** are a separate switch from the maturity tiers. `ONEDATA_MCP_EXPERIMENTAL=1` turns them on:

- eight extra tools, all experimental:
  - `list_space_datasets`: list a space's top-level datasets;
  - `set_file_xattrs`: merge extended attributes (hidden under a read-only token);
  - `get_file_id`: resolve a path to a file id (not needed: every tool that takes a path resolves it itself);
  - `grep_file_content`: return the lines of a file that contain a literal, case-sensitive substring (not a regex);
  - `list_marketplace_spaces`: list spaces advertised in the marketplace (it finds spaces no other tool can act on);
  - `list_user_harvesters`: list the user's harvesters and their indices;
  - `get_harvester_index_schema`: read a harvester index and its schema;
  - `query_harvester_index`: query a harvester index, with the query given as an object or as a JSON string (the three harvester tools need a harvester, which the test federation does not have);
- extra guidance in the server instructions.

Off by default; with it off, the tool list is exactly the one above. A preview tool can join the default list through the promotion protocol in [`design/10-tool-maturity.md`](design/10-tool-maturity.md).

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) (recommended)

## Install

```bash
uv sync
```

## Configuration

The server reads its configuration from the environment. It also loads a `.env` file, the first one found from the installed `onedata_mcp` package upward (a checkout's root `.env`), without overriding variables already set. `ONEDATA_MCP_DOTENV=0` turns that off (see [USAGE.md](USAGE.md)).

```bash
cp .env.example .env   # then edit the values
```

### Onedata connection (required for live calls)

| Variable | Description |
|---|---|
| `ONEDATA_ONEZONE_HOST` | Onezone base URL, e.g. `https://your-onezone.example` (no `/api/...` suffix) |
| `ONEDATA_ONEZONE_TOKEN` | Token sent as `X-Auth-Token` to Onezone |
| `ONEDATA_ONEPROVIDER_HOST` | Oneprovider base URL, e.g. `https://your-oneprovider.example` |
| `ONEDATA_ONEPROVIDER_TOKEN` | Token sent as `X-Auth-Token` to Oneprovider |
| `ONEDATA_ALLOW_INSECURE_TLS` | `true` only for self-signed or otherwise unverifiable certificates (default: verify TLS) |

### Logging (optional)

| Variable | Description |
|---|---|
| `FASTMCP_LOG_LEVEL` | Log level (default `INFO`) |
| `FASTMCP_LOG_FILE` | Also append logs to this file |

## Run

stdio, the usual mode for desktop clients (Cursor, Claude Desktop, …):

```bash
uv run onedata-mcp
```

Start it from the directory that holds your `.env`, or pass the variables in the environment.

### Client configuration (`mcp.json`)

```json
{
  "mcpServers": {
    "onedata": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/onedata-mcp", "onedata-mcp"],
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

### MCP Inspector

```bash
uv run fastmcp dev inspector onedata_mcp/main.py:mcp
```

## Telemetry (OpenTelemetry)

The server can emit one span per tool call (tool name, duration, status, error class) and continues an incoming W3C `traceparent`. A client-started trace therefore runs client → MCP server → Onedata REST as one trace.

Telemetry is off, with no exporter and no cost, until you set the standard `OTEL_*` variables:

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT="http://<your-collector-host>:4318"   # OTLP/HTTP
export OTEL_SERVICE_NAME="onedata-mcp"
uv run onedata-mcp
```

A client that puts a `traceparent` in the MCP request `_meta` gets the server's spans parented to its trace.

## Development

`make` is the entry point; `make help` lists every target.

```bash
make check    # lint + format + unit tests
make test     # unit tests only
```

More documentation:

- [USAGE.md](USAGE.md): using the tools;
- [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md): REST endpoint mapping;
- [`design/`](design/): design decisions;
- [AGENTS.md](AGENTS.md): repository conventions.

## Background

The federation-state tools (distribution, QoS, transfers, providers, metadata queries) were built and evaluated for the PPAM 2026 paper *LLM-agentic access to a federated scientific data layer with Onedata* ([DOI 10.5281/zenodo.20213326](https://doi.org/10.5281/zenodo.20213326)). Its multi-LLM benchmark harness is in [`benchmark/`](benchmark/), and the 16-tool set it evaluated is [`benchmark/tool_allowlist.py`](benchmark/tool_allowlist.py). Exact reproduction of the published benchmark uses the snapshot branch `ppam2026/14-tools`. On master the harness runs with the preview flag on, so its scenarios keep the tools they offered. The project builds on an earlier open-source Onedata MCP server.

## License

MIT, see [LICENSE.txt](LICENSE.txt).
