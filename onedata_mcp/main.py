import logging
import os
import sys

from fastmcp import FastMCP

from onedata_mcp.modules import files, harvesters, metadata, qos, spaces, transfers


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
    mcp = FastMCP(
        name="Onedata MCP Server",
        instructions="""
    This is an MCP server for Onedata.

    Onedata is a distributed data management system for storing, sharing, and
    collaborating on data across providers and spaces.

    Core entities:
    - Spaces: top-level shared workspaces grouping files, users, and providers.
    - Providers: services that store data and expose Oneprovider APIs.
    - Files/directories: data objects addressable by file id
      or logical path (<space_name>/<path_to_file>).
    """,
    )

    files.register_module(mcp)
    harvesters.register_module(mcp)
    metadata.register_module(mcp)
    qos.register_module(mcp)
    spaces.register_module(mcp)
    transfers.register_module(mcp)

    return mcp


logger = _setup_logging()
mcp = _create_onedata_mcp_server()


def main() -> None:
    """Main entry point for the Onedata MCP Server."""
    try:
        logger.info("Server started")
        mcp.run()

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received, shutting down...")  # type: ignore
        sys.exit(0)
    except Exception as e:
        logger.error(e)  # type: ignore
        sys.exit(1)


if __name__ == "__main__":
    main()
