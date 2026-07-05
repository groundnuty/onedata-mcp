import logging
import os
import sys

from fastmcp import FastMCP

from onedata_mcp.experimental import experimental_enabled
from onedata_mcp.modules import files, harvesters, metadata, qos, spaces, transfers
from onedata_mcp.telemetry import TracingMiddleware, setup_telemetry
from onedata_mcp.token_policy import (
    WRITE_TOOL_NAMES,
    resolve_register_write_tools_sync,
)

# Base server instructions — the validated, headline surface. Kept as a module
# constant so the default (experimental off) is byte-identical.
_BASE_INSTRUCTIONS = """
    This is an MCP server for Onedata.

    Onedata is a distributed data management system for storing, sharing, and
    collaborating on data across providers and spaces.

    Core entities:
    - Spaces: top-level shared workspaces grouping files, users, and providers.
    - Providers: services that store data and expose Oneprovider APIs.
    - Files/directories: data objects addressable by file id
      or logical path (<space_name>/<path_to_file>).
    """

# Enriched instructions — un-validated (ONEDATA_MCP_EXPERIMENTAL, onedata-mcp#1).
# Adds path discipline + navigation-efficiency guidance using OUR tool names.
_EXPERIMENTAL_INSTRUCTIONS = (
    _BASE_INSTRUCTIONS
    + """
    Addressing: absolute paths must start with '/'. Relative paths are not
    supported. Tools taking `file_id_or_path` accept either the absolute logical
    path or the opaque file id (an id does not start with '/').

    Working efficiently:
    - Read a known path directly with `get_file_attributes` / `get_file_metadata`;
      do not page through `list_children` to find a known file.
    - To find a file by name, use `list_files_recursively` with a `prefix`, or a
      harvester index — do not scan a directory entry by entry.
    - Use `list_children` only to explore an unknown directory structure.
    - Before querying a harvester index, call `get_harvester_index_schema` and
      build filters from the declared `mappings.properties`.
    """
)


def _setup_logging() -> logging.Logger:
    """Configure logging for the server."""
    log_level = os.environ.get("FASTMCP_LOG_LEVEL", "INFO")
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    log_file = os.environ.get("FASTMCP_LOG_FILE")

    logging.basicConfig(level=log_level, format=log_format, force=True)

    print(f"Log file: {log_file}, log level: {log_level}")
    if log_file:
        try:
            log_dir = os.path.dirname(log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir, exist_ok=True)

            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(log_format))
            logging.getLogger().addHandler(file_handler)
            logging.info("Logging to file: %s", log_file)
        except Exception as e:
            logging.error("Failed to set up log file %s: %s", log_file, e)

    return logging.getLogger("onedata-mcp-server")


def _create_onedata_mcp_server() -> FastMCP:
    # Opt-in un-validated candidates (new tools + enriched instructions/docstrings)
    # gated behind ONEDATA_MCP_EXPERIMENTAL. Off by default → validated surface.
    experimental = experimental_enabled()

    mcp = FastMCP(
        name="Onedata MCP Server",
        instructions=_EXPERIMENTAL_INSTRUCTIONS if experimental else _BASE_INSTRUCTIONS,
    )

    # Observability: install the OTLP tracer iff OTEL_* is configured (no-op
    # otherwise), and emit one span per tool call via middleware. Opt-in only;
    # nothing is hardcoded. See onedata_mcp/telemetry.py.
    setup_telemetry()
    mcp.add_middleware(TracingMiddleware())

    files.register_module(mcp, experimental=experimental)
    harvesters.register_module(mcp, experimental=experimental)
    metadata.register_module(mcp)
    qos.register_module(mcp)
    spaces.register_module(mcp, experimental=experimental)
    transfers.register_module(mcp)

    # Operational safety: when the Oneprovider token is read-only
    # (data.readonly caveat), prune the mutating tools from the surface so a
    # read-only deployment cannot be asked to attempt writes. The default
    # (writable token, or check skipped) leaves the full hardened surface
    # untouched. This prunes already-registered tools by name — it does not
    # alter any tool definition.
    if not resolve_register_write_tools_sync():
        write_tools = WRITE_TOOL_NAMES | ({"set_file_xattrs"} if experimental else set())
        for tool_name in write_tools:
            try:
                mcp.local_provider.remove_tool(tool_name)
            except Exception as exc:  # noqa: BLE001 — best-effort prune; log + continue
                logger.warning("Could not remove write tool %r: %s", tool_name, exc)

    return mcp


logger = _setup_logging()
mcp = _create_onedata_mcp_server()


def main() -> None:
    """Main entry point for the Onedata MCP Server.

    Transport selection (env var MCP_TRANSPORT, default ``stdio``):

    - ``stdio``  — subprocess transport. Used by claude-agent-sdk and
      the benchmark harness. Default for backward compat.
    - ``http``   — streamable-http transport. Used by the
      modelcontextprotocol/conformance suite and the
      modelcontextprotocol/inspector CLI smoke. Listens on
      ``MCP_HOST:MCP_PORT`` (defaults: ``127.0.0.1:3037``).

    Example HTTP launch:

        MCP_TRANSPORT=http MCP_PORT=3037 uv run onedata-mcp
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower().strip()
    try:
        if transport == "http":
            host = os.environ.get("MCP_HOST", "127.0.0.1")
            port = int(os.environ.get("MCP_PORT", "3037"))
            logger.info("Server started, transport=http host=%s port=%d", host, port)

            # MCP-2025-11-25 security: enforce DNS-rebinding protection
            # on the HTTP transport. Stdio is unaffected. Without this,
            # the conformance suite's `dns-rebinding-protection` scenario
            # FAILs (M-13). Operator can opt-out by setting
            # MCP_DNS_REBINDING_PROTECTION=0 (NOT recommended; offered
            # for debugging exotic Host-header environments).
            from starlette.middleware import Middleware

            from onedata_mcp._dns_rebinding import DnsRebindingProtection

            middleware: list[Middleware] = []
            if os.environ.get("MCP_DNS_REBINDING_PROTECTION", "1").strip() != "0":
                middleware.append(Middleware(DnsRebindingProtection, host=host, port=port))

            mcp.run(transport="http", host=host, port=port, middleware=middleware)
        else:
            logger.info("Server started, transport=stdio")
            mcp.run()

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")  # type: ignore
        sys.exit(0)
    except Exception as e:
        logger.error(e)  # type: ignore
        sys.exit(1)


if __name__ == "__main__":
    main()
