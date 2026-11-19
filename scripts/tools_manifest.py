"""Dump the server's `tools/list` as sorted JSON, so two checkouts can be diffed.

    uv run python scripts/tools_manifest.py [ROOT]

`ROOT` is the checkout whose `onedata_mcp` package is imported (default: the current
directory). With a ROOT other than the checkout this script lives in, run it with a Python
that has the dependencies, for example this checkout's `.venv/bin/python`.

The server is built in-process, with the `ONEDATA_MCP_TOOLS` (or `ONEDATA_MCP_MATURITY`)
of the calling environment, and listed through an in-process MCP client: what is printed
is what a client receives (name, description, inputSchema, annotations). No network is
used: the Oneprovider values are dummies, no Onezone is configured (so the read-only-token
check is skipped), and no `.env` is read.

To check that a change leaves the existing tools alone, run it on the base commit's
checkout and on the branch, with `ONEDATA_MCP_TOOLS` set to the base commit's tools, and
diff the two outputs: it must be empty.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path


def _isolate_environment() -> None:
    os.environ["ONEDATA_MCP_DOTENV"] = "0"
    os.environ["ONEDATA_ONEPROVIDER_HOST"] = "https://oneprovider.invalid"
    os.environ["ONEDATA_ONEPROVIDER_TOKEN"] = "dummy-token"
    for name in list(os.environ):
        if (
            name.startswith("OTEL_")
            or name.startswith("ONEDATA_ONEZONE_")
            or name in {"ONEDATA_MCP_NODE_NAMES", "FASTMCP_LOG_FILE"}
        ):
            del os.environ[name]


async def _manifest() -> list[dict]:
    # `main` prints a line to stdout on import; keep stdout for the JSON alone.
    with contextlib.redirect_stdout(sys.stderr):
        from onedata_mcp.main import mcp

    from fastmcp import Client

    async with Client(mcp) as client:
        tools = await client.list_tools()
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
            "annotations": (
                tool.annotations.model_dump(mode="json", exclude_none=True)
                if tool.annotations is not None
                else None
            ),
        }
        for tool in sorted(tools, key=lambda t: t.name)
    ]


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else os.getcwd()).resolve()
    if not (root / "onedata_mcp" / "main.py").is_file():
        print(f"no onedata_mcp package under {root}", file=sys.stderr)
        return 2
    # Import the package under ROOT, not whichever checkout the virtualenv has installed.
    sys.path.insert(0, str(root))
    _isolate_environment()

    manifest = asyncio.run(_manifest())

    import onedata_mcp

    imported_from = Path(onedata_mcp.__file__).resolve()
    if not imported_from.is_relative_to(root):
        print(f"imported onedata_mcp from {imported_from}, not from {root}", file=sys.stderr)
        return 2
    print(f"tools_manifest: {len(manifest)} tools from {imported_from.parent}", file=sys.stderr)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
