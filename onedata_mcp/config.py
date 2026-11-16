import logging
import os
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


@dataclass(frozen=True)
class OnedataRuntimeConfig:
    onezone: OnedataConfig
    oneprovider: OnedataConfig
    verify_ssl: bool


logger = logging.getLogger(__name__)


load_dotenv()


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
    )
