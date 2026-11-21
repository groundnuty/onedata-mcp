"""The locality guard of the gated live tests (`test/live/test_*_live.py`), in one place.

A live test writes to a federation, so it must never reach anything but the local Compose one.
The guard has three parts, each answering a way a test could end up elsewhere:

- every host the test is given must be `127.0.0.1`, `localhost` or end in `.datahub.test`;
- the name must resolve ONLY to loopback addresses: a `*.datahub.test` name mapped to another
  machine (an /etc/hosts or DNS entry) passes the name check and is refused here;
- once the check has passed, name resolution for those hosts is pinned to the addresses that
  were checked (the TLS host name stays the URL's), and proxy variables are ignored, so the
  connection made later cannot go anywhere else, whatever DNS or a proxy says by then.

`enforce_local_federation` is the one call a live test's autouse fixture makes, with every host
URL the test uses (a test that talks to a second provider names that one too). It is not a
test module: nothing here is collected.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse

import pytest


class NotLoopback(Exception):
    """A configured host is not provably the local machine."""


def is_local_host(host: str | None) -> bool:
    return bool(host) and (host in {"127.0.0.1", "localhost"} or host.endswith(".datahub.test"))


def resolve_loopback_only(
    host: str, port: int, resolve: Callable[..., list] = socket.getaddrinfo
) -> tuple[str, ...]:
    """The addresses `host` resolves to, provided EVERY one is loopback; else NotLoopback.

    A name that does not resolve, or resolves to nothing, is refused too.
    """
    try:
        infos = resolve(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise NotLoopback(f"{host!r} does not resolve ({type(error).__name__})") from None
    addresses = sorted({str(info[4][0]).split("%", 1)[0] for info in infos})
    if not addresses:
        raise NotLoopback(f"{host!r} resolves to no address")
    others = []
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_loopback:
                others.append(address)
        except ValueError:
            others.append(address)
    if others:
        raise NotLoopback(f"{host!r} resolves to non-loopback address(es) {others}")
    return tuple(addresses)


def pin_resolution(monkeypatch: pytest.MonkeyPatch, pins: dict[str, tuple[str, ...]]) -> None:
    """Make `socket.getaddrinfo` answer for each pinned host name with the addresses that were
    checked, whatever DNS or /etc/hosts says by the time the connection is made. The URL keeps
    its host name, so TLS verifies against it."""
    real = socket.getaddrinfo

    def pinned(host, port, *args, **kwargs):  # noqa: ANN001, ANN202
        # anyio asks with the name as bytes
        name = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        addresses = pins.get(name.lower()) if isinstance(name, str) else None
        if addresses is None:
            return real(host, port, *args, **kwargs)
        found: list = []
        for address in addresses:
            found.extend(real(address, port, *args, **kwargs))
        return found

    monkeypatch.setattr(socket, "getaddrinfo", pinned)


def enforce_local_federation(
    monkeypatch: pytest.MonkeyPatch,
    urls: dict[str, str],
    resolve: Callable[..., list] = socket.getaddrinfo,
    *,
    what: str = "this live test writes to the federation",
) -> None:
    """Refuse (pytest.fail) unless every URL's host is local by name and loopback by address;
    then pin its resolution and ignore proxy variables.

    `urls` maps what each URL is called in the message (its environment variable) to the URL;
    a test that uses a second provider names that provider's URL here too. `what` completes
    "refusing to run: ..." with what the test does to the federation.
    """
    pins: dict[str, tuple[str, ...]] = {}
    for name, url in urls.items():
        parsed = urlparse(url)
        host = parsed.hostname
        if not is_local_host(host):
            pytest.fail(
                f"refusing to run: {what}, and runs only against 127.0.0.1, localhost or "
                f"*.datahub.test ({name} host: {host!r})",
                pytrace=False,
            )
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            pins[host.lower()] = resolve_loopback_only(host, port, resolve)
        except (ValueError, NotLoopback) as error:
            pytest.fail(
                f"refusing to run: {name} host is not loopback-only ({error})", pytrace=False
            )
    pin_resolution(monkeypatch, pins)
    # httpx trusts the environment's proxies; a proxy would carry the traffic elsewhere
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(var, "*")
