"""Startup policy: hide write tools when the Oneprovider token is read-only.

This is operational/infrastructure code — it runs once at server construction
and never appears in the MCP tool surface. It does not change any tool's
definition; when a read-only token is detected it simply prunes the mutating
tools from the already-registered surface (see ``main.py``).

The default path (writable token, or check skipped) registers the full,
multi-LLM-hardened tool surface unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor

from onedata_mcp.api.tokens import examine_access_token, token_has_data_readonly_caveat
from onedata_mcp.utils import OnedataApiError, is_valid_url

logger = logging.getLogger(__name__)

# Every MCP tool that mutates Onedata state (annotated destructiveHint=True in
# the modules). Kept in lockstep with the modules; a drift test asserts this set
# equals the destructive-annotated tools actually registered.
WRITE_TOOL_NAMES = frozenset(
    {
        # files
        "create_file",
        "create_directory",
        "delete_file",
        "set_file_metadata",
        "move_file",
        # qos
        "add_file_qos_requirement",
        "remove_qos_requirement",
    }
)


def run_startup_coroutine[T](coro: Coroutine[None, None, T]) -> T:
    """Run a coroutine from sync server-construction code, loop or no loop."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def onezone_configured_for_token_check() -> bool:
    """True when Onezone host + user token are set (gate for examining the provider token)."""

    host = os.getenv("ONEDATA_ONEZONE_HOST")
    token = os.getenv("ONEDATA_ONEZONE_TOKEN")
    return bool(host and is_valid_url(host) and token and token.strip())


async def resolve_register_write_tools() -> bool:
    """Whether to keep the mutating tools registered on the MCP server.

    Returns ``True`` (keep writers) when the check is skipped or the token is
    not read-only. Returns ``False`` only when the Oneprovider token carries a
    ``data.readonly`` caveat. Any failure to examine the token fails *open*
    (keep writers) so a transient Onezone hiccup never silently disables writes
    without telling the operator.
    """

    if not onezone_configured_for_token_check():
        return True

    provider_token = os.getenv("ONEDATA_ONEPROVIDER_TOKEN")
    if not provider_token or not provider_token.strip():
        logger.warning(
            "Onezone credentials set but ONEDATA_ONEPROVIDER_TOKEN is missing; "
            "cannot examine provider token — keeping all file tools"
        )
        return True

    try:
        examined = await examine_access_token(provider_token.strip())
    except (OnedataApiError, TypeError) as exc:
        logger.warning(
            "Could not examine Oneprovider token via Onezone (%s); keeping all file tools",
            exc,
        )
        return True

    if token_has_data_readonly_caveat(examined):
        logger.info(
            "Oneprovider token has data.readonly caveat; hiding write tools: %s",
            ", ".join(sorted(WRITE_TOOL_NAMES)),
        )
        return False

    return True


def resolve_register_write_tools_sync() -> bool:
    """Sync entry point for server construction."""

    return run_startup_coroutine(resolve_register_write_tools())
