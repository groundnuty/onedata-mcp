# User guide — Onedata MCP server

This guide walks you through installing the **Onedata MCP server**, pointing it at your Onedata federation, and using it from two of the most common LLM-agent harnesses: **Claude Code** and **opencode**.

The server exposes 16 curated tools (and seven further tools available in the ablation surface) that let an LLM agent drive a federated [Onedata](https://onedata.org/) data layer end-to-end — discover spaces and providers, read and write files, manage custom JSON metadata, declare placement policies via QoS rules, inspect block-level distribution, and observe transfers. The design and the benchmark behind it are described in our PPAM 2026 paper (DOI [10.5281/zenodo.20213326](https://doi.org/10.5281/zenodo.20213326)).

---

## 1. Prerequisites

- An Onedata federation you can reach over the network (a public Onezone URL + at least one Oneprovider URL). If you don't have one yet, see [onedata.org](https://onedata.org/) for the upstream service or contact your data-platform operators.
- **Python 3.12** or newer.
- [**`uv`**](https://docs.astral.sh/uv/) — the Python package manager we recommend (`pip install uv` or [`brew install uv`](https://docs.astral.sh/uv/getting-started/installation/)). Plain `pip` works too if you prefer.
- An MCP-aware client. This guide covers two: **Claude Code** ([claude.com/claude-code](https://claude.com/claude-code)) and **opencode** ([opencode.ai](https://opencode.ai/)). The MCP protocol is identical; any other MCP-aware client (Cursor, Claude Desktop, MCP Inspector, etc.) will work with the same configuration shape.

---

## 2. Installation

```bash
git clone https://github.com/groundnuty/onedata-mcp.git
cd onedata-mcp
git checkout ppam2026/14-tools     # the branch evaluated in the PPAM 2026 paper
uv sync                            # install dependencies into a uv-managed venv
```

Quick verification:

```bash
uv run onedata-mcp --help
```

That should print the server's startup banner. (The server starts in stdio mode by default — see § 9 for the HTTP transport mode.)

---

## 3. Configure (environment + tokens)

The server reads its configuration from process environment variables, with a `.env` file in the working directory as a convenience. Copy the template and fill it in:

```bash
cp .env.example .env
```

### Required variables

| Variable                       | Meaning                                                                       |
|--------------------------------|-------------------------------------------------------------------------------|
| `ONEDATA_ONEZONE_HOST`         | Base URL of your Onezone, e.g. `https://onezone.example.org` (no `/api/...`)  |
| `ONEDATA_ONEZONE_TOKEN`        | Onezone access token (`X-Auth-Token` header value)                            |
| `ONEDATA_ONEPROVIDER_HOST`     | Base URL of a Oneprovider supporting your spaces                              |
| `ONEDATA_ONEPROVIDER_TOKEN`    | Oneprovider access token                                                      |

### Optional

| Variable                       | Meaning                                                                        |
|--------------------------------|---------------------------------------------------------------------------------|
| `ONEDATA_ALLOW_INSECURE_TLS`   | Set to `true` only if your federation uses self-signed TLS (development only). |
| `FASTMCP_LOG_LEVEL`            | Server log level (default `INFO`; `DEBUG` for verbose).                        |
| `FASTMCP_LOG_FILE`             | If set, logs are also appended to this file.                                   |
| `MCP_TRANSPORT`                | Set to `http` for the HTTP transport (default is `stdio`). See § 9.            |

### How to get an Onedata token

In the Onezone web UI: **Tokens → Create token**. Choose the appropriate scope:

- **Access token** with `ozw.user.*` privileges (or admin-scope if you intend to provision spaces and assign providers).
- The same token can be passed as both `ONEDATA_ONEZONE_TOKEN` and `ONEDATA_ONEPROVIDER_TOKEN` if its scope reaches both — otherwise mint one per service.

Tokens expire; the server makes no attempt to refresh them. If the agent starts getting `HTTP 401`s, mint a fresh token and restart the server.

---

## 4. Test the server standalone (optional but recommended)

Before wiring it into an agent, confirm the server starts and reaches your federation:

```bash
uv run onedata-mcp
```

In stdio mode the process waits for JSON-RPC on stdin and writes responses on stdout — useful for an agent, not for a human. To poke it manually, use [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
uv run fastmcp dev inspector onedata_mcp/main.py:mcp
```

The Inspector opens a local browser UI listing the tools. Try calling `list_user_spaces` — you should see your Onedata spaces in the response.

If you see "no spaces", verify your `ONEDATA_ONEZONE_HOST` and token in the Onezone UI; the server bubbles `X-Auth-Token` errors as MCP errors with the structured payload preserved.

---

## 5. Using with Claude Code

Claude Code reads MCP server configuration from one of two locations:

- **Project-level** — a file named `.mcp.json` in the project root (preferred if you want this MCP server scoped to one workspace).
- **User-level** — `~/.claude.json` (shared across all workspaces).

### Config

Create `.mcp.json` in your project root:

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
        "ONEDATA_ONEZONE_HOST": "https://onezone.example.org",
        "ONEDATA_ONEZONE_TOKEN": "your-token-here",
        "ONEDATA_ONEPROVIDER_HOST": "https://oneprovider.example.org",
        "ONEDATA_ONEPROVIDER_TOKEN": "your-token-here"
      }
    }
  }
}
```

Equivalent CLI registration (you can use either form):

```bash
claude mcp add onedata \
  --env ONEDATA_ONEZONE_HOST=https://onezone.example.org \
  --env ONEDATA_ONEZONE_TOKEN=your-token \
  --env ONEDATA_ONEPROVIDER_HOST=https://oneprovider.example.org \
  --env ONEDATA_ONEPROVIDER_TOKEN=your-token \
  -- uv run --directory /absolute/path/to/onedata-mcp onedata-mcp
```

You can also point `env` at a file instead of inlining the tokens — Claude Code accepts standard shell-style references; or use a `.env` file in the working directory the server starts in.

### Verifying the connection

Start Claude Code in the project. In the REPL:

```
> /mcp
```

You should see `onedata` listed with the tool count. If you see "connection failed", check the server's stderr output for the underlying error (token, URL, TLS).

### Example session

Once connected, prompt the agent in natural language. Claude Code will pick the right tools automatically.

> **You:** List my Onedata spaces and tell me how many providers support each.
>
> **Claude:** *(calls `list_user_spaces`, then `list_space_providers` for each)*
>
> Here are your spaces:
>
> | Space | Provider count |
> |---|:-:|
> | IndustrialMonitoring | 3 |
> | LabBackups | 2 |
> | scratch | 1 |

> **You:** In `IndustrialMonitoring`, find all files under `/datasets/uc1_training/` and tag every `.parquet` file with `{"reviewed": false}`.
>
> **Claude:** *(calls `list_files_recursively` to enumerate the subtree, then `set_file_metadata` per `.parquet` file)*
>
> Tagged 18 files with `reviewed=false`. Done.

> **You:** Add a QoS rule on `/IndustrialMonitoring/critical/checkpoint_42.bin` that requires it to be on at least 2 EU providers, then poll until the rule fulfils or 60 seconds pass.
>
> **Claude:** *(calls `add_file_qos_requirement` with expression like `country=PL | country=SK` and `replicas_num=2`, then loops on `get_file_qos_summary` + `list_space_transfers`)*
>
> QoS rule `b724a1...` created. After 12 seconds the rule status moved from `pending` to `fulfilled`; one replication transfer to `Cloud-SK` is visible in the space transfer log.

---

## 6. Using with opencode

opencode is the open-source alternative harness; its MCP support is structurally identical to Claude Code's.

### Config

opencode reads from `opencode.json` (project root) or `~/.config/opencode/opencode.json` (user-level). For an MCP server using stdio transport:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "onedata": {
      "type": "local",
      "command": [
        "uv",
        "run",
        "--directory",
        "/absolute/path/to/onedata-mcp",
        "onedata-mcp"
      ],
      "enabled": true,
      "environment": {
        "ONEDATA_ONEZONE_HOST": "https://onezone.example.org",
        "ONEDATA_ONEZONE_TOKEN": "your-token-here",
        "ONEDATA_ONEPROVIDER_HOST": "https://oneprovider.example.org",
        "ONEDATA_ONEPROVIDER_TOKEN": "your-token-here"
      }
    }
  }
}
```

Two differences from the Claude Code config:

- The top-level key is `mcp` (singular), not `mcpServers`.
- Each server's command is an array under `command` (no separate `args`), and env is under `environment` rather than `env`.
- An `enabled` boolean lets you switch a server off without removing it (useful when you have several MCPs configured).

### Verifying the connection

Start `opencode` in the project. In the REPL:

```
/mcp
```

Same `list_user_spaces`-style verification as with Claude Code.

### Example session

The MCP protocol is the agent-harness's, not the model's — once the tool surface is exposed, the prompting feel is identical. Try the same three prompts from § 5; the underlying tool calls and federation interactions are the same.

If you want the agent to favour the curated 16-tool surface (the one used in the PPAM 2026 benchmark) over the wider 21-tool surface, the simplest way is to either:

- Run the server with `ONEDATA_MCP_ALLOWLIST=HEADLINE` (if your version of the server supports it), or
- Rely on the agent to discover the relevant tools through tool-list inspection — opencode (like Claude Code) does not need the allowlist to be enforced server-side for sensible behaviour.

---

## 7. Tool reference

The full curated surface (`HEADLINE` allowlist used in the PPAM 2026 paper):

### Namespace and content I/O (R1)

| Tool                       | What it does                                            |
|----------------------------|---------------------------------------------------------|
| `list_user_spaces`         | Discover all Onedata spaces available to the agent.     |
| `list_files_recursively`   | Recursive listing of a subtree, paginated.              |
| `download_file`            | Stream a file's bytes plus a structured `size_bytes`.   |
| `create_file`              | Create a file (with optional `create_parents`).         |
| `move_file`                | Atomic intra-space move (rename or directory-move).     |
| `delete_file`              | Delete a file or directory.                             |

### Custom metadata (R2)

| Tool                       | What it does                                            |
|----------------------------|---------------------------------------------------------|
| `get_file_metadata`        | Read JSON / RDF / xattrs custom metadata for a file.    |
| `set_file_metadata`        | Replace JSON / RDF / xattrs custom metadata.            |
| `query_by_metadata`        | Bounded recursive predicate search (no harvester needed). |

### Provider topology (R3)

| Tool                       | What it does                                            |
|----------------------------|---------------------------------------------------------|
| `list_space_providers`     | List which Oneproviders support a given space.          |

### Block-level distribution (R4)

| Tool                       | What it does                                            |
|----------------------------|---------------------------------------------------------|
| `get_file_distribution`    | Per-provider, per-storage block ranges held.            |

### QoS placement (R5)

| Tool                            | What it does                                                       |
|---------------------------------|---------------------------------------------------------------------|
| `get_file_qos_summary`          | Active requirements + statuses + inheritance for a file or dir.    |
| `get_qos_requirement`           | Single rule's expression, replicas count, status.                  |
| `add_file_qos_requirement`      | Declare a placement policy (async; returns rule ID immediately).   |

### Transfers (R6)

| Tool                       | What it does                                            |
|----------------------------|---------------------------------------------------------|
| `list_space_transfers`     | Scheduled transfers in a space (IDs only; pair with `get_transfer`). |
| `get_transfer`             | Single transfer's source, destination, state, bytes.    |

The server also exposes (outside HEADLINE) the inherited harvester tools (`list_user_harvesters`, `get_harvester_index_schema`, `query_harvester_index`), `get_file_id`, `get_file_attributes`, `list_children`, and `grep_file_content` for ablation experiments and Onedata-specific debugging.

---

## 8. Example prompts by capability

Once your agent has the tools, you can ask it questions in plain English. The examples below correspond to the six requirement classes (R1–R6) the curated surface was designed against:

```
R1 (CRUD):
  "What's in /MySpace/results/run01.txt? Report just the byte count."
  "Move /MySpace/staging/draft.txt to /MySpace/published/draft.txt."
  "Delete every .tmp file under /MySpace/scratch/."

R2 (metadata):
  "What does /MySpace/calibration.bin's custom JSON metadata say?"
  "Find every file in MySpace where pipeline_stage=raw and reviewed=false."

R3 (provider topology):
  "Which Oneproviders support MySpace? Group by their country code."

R4 (distribution):
  "Show the per-provider block distribution for /MySpace/dataset/large.parquet.
   Report whether the file is fully replicated on at least one EU provider."

R5 (placement policy):
  "Ensure /MySpace/critical/checkpoint.bin lives on at least 2 EU providers.
   Wait until the QoS rule fulfils or 60 seconds pass; tell me which happened."
  "List every QoS rule attached to /MySpace/ that is in 'impossible' status."

R6 (transfer activity):
  "What was the most recent transfer of /MySpace/dataset/large.parquet?
   Report its source provider, destination provider, and byte count."
```

The agent will plan the tool sequence on its own. For more complex multi-step tasks (rename + tag + verify, or add QoS + poll for fulfillment), the agent can chain four to six tool calls in sequence with no extra prompting.

---

## 9. Transport modes (stdio vs HTTP)

By default the server uses MCP's **stdio** transport — the agent harness spawns the server as a subprocess and communicates over its stdin/stdout. This is the most reliable mode and the one all the examples above assume.

For tooling that requires an HTTP endpoint (MCP Inspector running in a browser, network-based test harnesses, etc.), set `MCP_TRANSPORT=http`:

```bash
MCP_TRANSPORT=http uv run onedata-mcp
```

The HTTP server enforces **DNS-rebinding protection** via Host/Origin allow-listing (allows `127.0.0.1:<port>` and `localhost:<port>`; rejects everything else with `HTTP 403`). This is the [MCP-spec 2025-11-25](https://modelcontextprotocol.io/) requirement; the `modelcontextprotocol/conformance` suite reports **2 / 2 PASS** on the `dns-rebinding-protection` scenario after this hardening.

If you need the HTTP server to accept requests from a remote browser (i.e. add additional allow-listed hosts), you'll need to extend the middleware in `onedata_mcp/_dns_rebinding.py`. Don't disable the middleware in production.

---

## 10. Troubleshooting

| Symptom                                                  | Likely cause                                                            | Fix                                                                                                        |
|-----------------------------------------------------------|--------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| Claude Code / opencode shows "MCP connection failed"     | Server failed to start (wrong path? venv not synced?)                   | Run `uv run onedata-mcp --help` standalone; the error appears on stderr.                                    |
| Agent says "I don't have access to your Onedata spaces"  | Token unset or expired                                                  | Verify env via `env \| grep ONEDATA_`; mint a fresh token if needed.                                        |
| Agent gets HTTP 403 on a specific tool                   | Token scope doesn't reach that operation (e.g. transfers need oneprovider) | Mint a higher-scope token, or set `ONEDATA_ONEZONE_TOKEN` ≠ `ONEDATA_ONEPROVIDER_TOKEN`.                    |
| Agent gets HTTP 404 on `set_file_metadata` with `metadata_type="custom"` | Onedata accepts `{json, rdf, xattrs}` only — the M-4 finding             | The wrapper aliases `"custom"` to `"json"` automatically. If you see it, you're on an older fork — update.  |
| TLS certificate-verify failure                            | Self-signed Onedata cert                                                | Development: `ONEDATA_ALLOW_INSECURE_TLS=true`. Production: install your CA's root cert.                    |
| Server starts but `list_user_spaces` returns `[]`         | Token is valid but has no spaces in scope                               | Confirm in the Onezone web UI that the token's user has at least one space.                                 |
| Agent fails on the same tool every time across LLMs       | Likely an MCP-server-design issue, not the LLM                          | See [`research/empirical-mcp-server-findings.md`](research/empirical-mcp-server-findings.md) — M-1 .. M-13 catalogue of known patterns. |
| QoS rule sits in `impossible` status forever              | Expression names operands that don't exist in the federation (e.g. `country=PL` when no admin attributes are set) | Use `providerId=<hex>` or `anyStorage` operands; or ask the federation admin to set storage attributes.    |

---

## 11. Where to go next

- **Paper** — [LLM-agentic access to a federated scientific data layer with Onedata (PPAM 2026)](https://doi.org/10.5281/zenodo.20213326). Section 3 documents the curated tool surface; Section 5 documents the seven cross-cutting design themes.
- **Replication package** — [github.com/groundnuty/ppam-2026-mcp-onedata-replication-package](https://github.com/groundnuty/ppam-2026-mcp-onedata-replication-package). Includes the 18 verbatim benchmark briefs (`supp.~§A`), the M-1 .. M-13 server-design findings (`supp.~§C`), and the full reproducibility runbook (`supp.~§R`).
- **MCP specification** — [modelcontextprotocol.io](https://modelcontextprotocol.io/). The wire protocol the server speaks.
- **Onedata** — [onedata.org](https://onedata.org/). The underlying federated-data system.
- **opencode docs** — [opencode.ai/docs](https://opencode.ai/docs/). The opensource MCP-aware harness.
- **Claude Code docs** — [docs.anthropic.com/claude-code](https://docs.anthropic.com/claude-code). Anthropic's official MCP-aware CLI.

---

*Last updated: 2026-05-19. Issues, design suggestions, and pull requests welcome on the [engineering repository](https://github.com/groundnuty/onedata-mcp/issues).*
