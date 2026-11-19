"""The in-process MCP server for the panel, and the process environment it reads.

`build_app` registers the same tool modules `onedata_mcp/main.py` does, without main's
import-time side effects (log setup, the read-only-token check at Onezone, telemetry). A
drift test keeps the module list equal to main's.

The tools read the Onedata settings from the process environment at call time, so
`configure_environment` sets them from `k2val.toml` and the token files. It also sets
`ONEDATA_MCP_DOTENV=0`: the repository's `.env` (the PPAM harness loads it) points at the live
federation and must never be read here.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP

from benchmark.k2val.config import K2valConfig, check_hosts, read_federation_secrets

MODULE_NAMES = ("files", "harvesters", "metadata", "qos", "readiness", "spaces", "transfers")

_ENV_FROM_HOSTS = {
    "ONEDATA_ONEPROVIDER_HOST": "ONEDATA_ONEPROVIDER_HOST (effective)",
    "ONEDATA_ONEZONE_HOST": "ONEDATA_ONEZONE_HOST (effective)",
}


def configure_environment(cfg: K2valConfig) -> None:
    """Point the process at the local federation, then check the EFFECTIVE values."""
    secrets = read_federation_secrets(cfg.federation)
    env = os.environ
    env["ONEDATA_MCP_DOTENV"] = "0"
    env["ONEDATA_ONEPROVIDER_HOST"] = cfg.federation.local.host
    env["ONEDATA_ONEPROVIDER_TOKEN"] = secrets.local_token.reveal()
    env["ONEDATA_ONEZONE_HOST"] = cfg.federation.onezone_host
    env["ONEDATA_ONEZONE_TOKEN"] = secrets.onezone_token.reveal()
    env["ONEDATA_ALLOW_INSECURE_TLS"] = "true" if cfg.federation.allow_insecure_tls else "false"
    for name in ("ONEDATA_MCP_NODE_NAMES", "ONEDATA_MCP_TOOLS", "ONEDATA_MCP_MATURITY"):
        env.pop(name, None)  # the allow-list is applied per scenario, not at launch
    check_hosts({label: env.get(var, "") for var, label in _ENV_FROM_HOSTS.items()})


def build_app() -> FastMCP:
    from onedata_mcp.modules import (  # imported late: after configure_environment
        files,
        harvesters,
        metadata,
        qos,
        readiness,
        spaces,
        transfers,
    )

    modules = {
        "files": files,
        "harvesters": harvesters,
        "metadata": metadata,
        "qos": qos,
        "readiness": readiness,
        "spaces": spaces,
        "transfers": transfers,
    }
    assert tuple(modules) == MODULE_NAMES
    app = FastMCP(name="Onedata MCP Server (k2val)")
    for module in modules.values():
        module.register_module(app)
    return app
