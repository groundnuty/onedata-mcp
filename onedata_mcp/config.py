import json
import logging
import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

from .utils import is_valid_url


@dataclass(frozen=True)
class OnedataConfig:
    base_url: str
    auth_headers: dict[str, str]
    verify_ssl: bool
    # The name this server declares for the service a call is for ("onezone" or
    # "oneprovider"). Error messages carry it instead of the host or the URL.
    service: str = "onedata"
    # The deploy's `(real name, declared name)` pairs for the federation's nodes, from
    # ONEDATA_MCP_NODE_NAMES (see `get_node_names`). Empty when none is given (Compose).
    node_names: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class OnedataRuntimeConfig:
    onezone: OnedataConfig
    oneprovider: OnedataConfig
    verify_ssl: bool


logger = logging.getLogger(__name__)


load_dotenv()

NODE_NAMES_ENV = "ONEDATA_MCP_NODE_NAMES"
_NODE_NAMES_FORMAT = (
    f"{NODE_NAMES_ENV} must be a JSON object of real node name -> declared name, e.g. "
    '{"node-1.site.example:8443": "provider-a"}; each key is a host name or an address, '
    "optionally with a port, and each value a non-empty string"
)
_BRACKETED_RE = re.compile(r"\[(?P<host>[^\[\]]+)\](?::\d{1,5})?")
_HOST_PORT_KEY_RE = re.compile(r"(?P<host>[^:]+):\d{1,5}")


def _node_host(key: str) -> str:
    """The host or address part of a node key: its port and IPv6 brackets removed, lower-cased."""
    key = key.strip()
    for pattern in (_BRACKETED_RE, _HOST_PORT_KEY_RE):
        matched = pattern.fullmatch(key)
        if matched:
            return matched.group("host").lower()
    return key.lower()


def get_node_names() -> tuple[tuple[str, str], ...]:
    """The federation's real node names with the name each is declared under.

    Read from ONEDATA_MCP_NODE_NAMES, which the deploy sets from the federation's
    state: a JSON object whose keys are the real host names and addresses of the
    nodes (each optionally with a port, `host:port` or `[ipv6]:port`) and whose
    values are the declared names the agent may see (the zone's declared name, a
    provider's layout domain). Unset or empty means no mapping, as on Compose,
    where the names are already the declared ones.

    Returns `(host, declared)` pairs, hosts lower-cased and without ports. Raises
    ValueError, naming the variable and the expected format but never its value
    (which holds the real names), when the value is malformed.
    """
    raw = os.getenv(NODE_NAMES_ENV, "").strip()
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ValueError(f"{NODE_NAMES_ENV} is not valid JSON. {_NODE_NAMES_FORMAT}") from None
    if not isinstance(parsed, dict):
        raise ValueError(f"{NODE_NAMES_ENV} is not a JSON object. {_NODE_NAMES_FORMAT}")
    pairs: dict[str, str] = {}
    for key, declared in parsed.items():
        host = _node_host(key)
        if (
            not host
            or any(c.isspace() or c in "/\\\"'" for c in host)
            or not isinstance(declared, str)
            or not declared.strip()
        ):
            raise ValueError(f"{NODE_NAMES_ENV} has an invalid entry. {_NODE_NAMES_FORMAT}")
        if pairs.setdefault(host, declared) != declared:
            raise ValueError(
                f"{NODE_NAMES_ENV} declares one node under two names. {_NODE_NAMES_FORMAT}"
            )
    return tuple(sorted(pairs.items()))


def get_onezone_config() -> OnedataConfig:
    """Get the configuration for the onedata Onezone MCP server."""

    onezone_host = os.getenv("ONEDATA_ONEZONE_HOST")
    onezone_token = os.getenv("ONEDATA_ONEZONE_TOKEN")
    allow_insecure_tls = os.getenv("ONEDATA_ALLOW_INSECURE_TLS", "false")

    if not onezone_host or not is_valid_url(onezone_host):
        logger.warning("ONEDATA_ONEZONE_HOST is missing or invalid (value: %s)", onezone_host)

    base_url = f"{onezone_host.rstrip('/')}/api/v3/onezone"
    auth_headers: dict[str, str] = {
        "X-Auth-Token": onezone_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    verify_ssl = allow_insecure_tls.lower() != "true"

    return OnedataConfig(
        base_url=base_url,
        auth_headers=auth_headers,
        verify_ssl=verify_ssl,
        service="onezone",
        node_names=get_node_names(),
    )


def get_oneprovider_config() -> OnedataConfig:
    """Get the configuration for the onedata Oneprovider MCP server."""

    oneprovider_host = os.getenv("ONEDATA_ONEPROVIDER_HOST")
    oneprovider_token = os.getenv("ONEDATA_ONEPROVIDER_TOKEN")
    allow_insecure_tls = os.getenv("ONEDATA_ALLOW_INSECURE_TLS", "false")

    if not oneprovider_host or not is_valid_url(oneprovider_host):
        logger.warning(
            "ONEDATA_ONEPROVIDER_HOST is missing or invalid (value: %s)", oneprovider_host
        )

    base_url = f"{oneprovider_host.rstrip('/')}/api/v3/oneprovider"
    auth_headers: dict[str, str] = {
        "X-Auth-Token": oneprovider_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    verify_ssl = allow_insecure_tls.lower() != "true"

    return OnedataConfig(
        base_url=base_url,
        auth_headers=auth_headers,
        verify_ssl=verify_ssl,
        service="oneprovider",
        node_names=get_node_names(),
    )
