"""The locality guard of the three gated live tests that write to a federation.

A live test must never reach anything but the local Compose federation. The guard lives in
`test/live/local_federation.py` and each of `test_qos_live.py`, `test_files_live.py` and
`test_transfer_live.py` runs it, as an autouse fixture, over every host it connects to. None of
them can run without a federation, so what is checked here is the guard, what each test hands
it, and that the autouse fixture is wired to it. (`test_qos.py` checks the guard's parts through
`test_qos_live`.)
"""

from __future__ import annotations

import ast
import importlib.util
import os
import socket
import sys
from pathlib import Path

import pytest

LIVE = Path(__file__).resolve().parents[2] / "live"
MODULES = {
    "qos": ("test_qos_live.py", {"ONEDATA_ONEPROVIDER_HOST", "ONEDATA_ONEZONE_HOST"}),
    "files": ("test_files_live.py", {"ONEDATA_ONEPROVIDER_HOST", "ONEDATA_ONEZONE_HOST"}),
    "transfer": (
        "test_transfer_live.py",
        {
            "ONEDATA_ONEPROVIDER_HOST",
            "ONEDATA_ONEZONE_HOST",
            "ONEDATA_MCP_LIVE_TRANSFER_SOURCE_HOST",
        },
    ),
}
SOURCE_ENV = "ONEDATA_MCP_LIVE_TRANSFER_SOURCE_HOST"


def _load(name: str):
    filename, _ = MODULES[name]
    spec = importlib.util.spec_from_file_location(f"{name}_live_guard_under_test", LIVE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # a dataclass in it looks its module up here
    spec.loader.exec_module(module)
    return module


def _resolver(**by_host: str):
    """A resolver that answers each host with one address; any other host is not found."""

    def resolve(host, port, *args, **kwargs):
        if host not in by_host:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (by_host[host], port))]

    return resolve


def _env(monkeypatch: pytest.MonkeyPatch, **hosts: str) -> None:
    for var in (
        "ONEDATA_ONEPROVIDER_HOST",
        "ONEDATA_ONEZONE_HOST",
        SOURCE_ENV,
    ):
        monkeypatch.delenv(var, raising=False)
    for var, value in hosts.items():
        monkeypatch.setenv(var, value)


@pytest.mark.parametrize("name", MODULES)
def test_each_live_test_hands_the_guard_every_host_it_connects_to(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    live = _load(name)
    _, expected = MODULES[name]
    _env(
        monkeypatch,
        ONEDATA_ONEPROVIDER_HOST="https://op.datahub.test",
        ONEDATA_ONEZONE_HOST="https://oz.datahub.test",
        **{SOURCE_ENV: "https://src.datahub.test"},
    )

    assert set(live.federation_urls()) == expected
    # an unset Onezone is simply not part of it; the provider is always checked, set or not
    monkeypatch.delenv("ONEDATA_ONEZONE_HOST")
    assert "ONEDATA_ONEZONE_HOST" not in live.federation_urls()
    assert "ONEDATA_ONEPROVIDER_HOST" in live.federation_urls()


@pytest.mark.parametrize("name", MODULES)
def test_each_live_test_runs_the_guard_as_an_autouse_fixture(name: str) -> None:
    """The fixture exists, is autouse, and calls `enforce_local`: nothing in the module can run
    before the guard has."""
    filename, _ = MODULES[name]
    tree = ast.parse((LIVE / filename).read_text())
    fixture = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_local_federation_only"
    )
    autouse = any(
        isinstance(d, ast.Call)
        and isinstance(d.func, ast.Attribute)
        and d.func.attr == "fixture"
        and any(
            k.arg == "autouse" and isinstance(k.value, ast.Constant) and k.value.value is True
            for k in d.keywords
        )
        for d in fixture.decorator_list
    )
    calls = {
        c.func.id for c in ast.walk(fixture) if isinstance(c, ast.Call) and hasattr(c.func, "id")
    }
    assert autouse and "enforce_local" in calls


@pytest.mark.parametrize("name", MODULES)
def test_a_datahub_test_name_that_resolves_off_machine_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Passes the by-name check (`*.datahub.test`), and is not the local machine."""
    live = _load(name)
    _env(
        monkeypatch,
        ONEDATA_ONEPROVIDER_HOST="https://op.datahub.test:8443",
        **{SOURCE_ENV: "https://src.datahub.test:8443"},
    )
    before = socket.getaddrinfo

    with pytest.raises(
        pytest.fail.Exception, match="ONEDATA_ONEPROVIDER_HOST host is not loopback"
    ):
        live.enforce_local(monkeypatch, resolve=_resolver(**{"op.datahub.test": "10.1.2.3"}))

    assert socket.getaddrinfo is before  # nothing was pinned


@pytest.mark.parametrize("name", MODULES)
@pytest.mark.parametrize("url", ["https://example.org", "", "https://127.0.0.1.example.org"])
def test_a_host_that_is_not_local_by_name_is_refused(
    monkeypatch: pytest.MonkeyPatch, name: str, url: str
) -> None:
    live = _load(name)
    _env(monkeypatch, ONEDATA_ONEPROVIDER_HOST=url, **{SOURCE_ENV: "https://127.0.0.1:1"})

    with pytest.raises(pytest.fail.Exception, match="refusing to run") as caught:
        live.enforce_local(monkeypatch, resolve=_resolver(**{"127.0.0.1": "127.0.0.1"}))

    assert "runs only against 127.0.0.1, localhost or *.datahub.test" in str(caught.value)
    assert live.WHAT in str(caught.value)  # says what this test does to the federation


def test_the_transfer_test_checks_and_pins_the_source_provider_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe files are written and deleted through the source provider: a source name that
    resolves off-machine is refused, and one that passes is pinned with the rest."""
    live = _load("transfer")
    _env(
        monkeypatch,
        ONEDATA_ONEPROVIDER_HOST="https://op.datahub.test",
        ONEDATA_ONEZONE_HOST="https://oz.datahub.test",
        **{SOURCE_ENV: "https://src.datahub.test"},
    )

    with pytest.raises(pytest.fail.Exception, match=f"{SOURCE_ENV} host is not loopback-only"):
        live.enforce_local(
            monkeypatch,
            resolve=_resolver(
                **{
                    "op.datahub.test": "127.0.0.1",
                    "oz.datahub.test": "127.0.0.1",
                    "src.datahub.test": "192.0.2.50",
                }
            ),
        )

    before = socket.getaddrinfo
    monkeypatch.setenv("HTTPS_PROXY", "http://203.0.113.9:3128")
    live.enforce_local(
        monkeypatch,
        resolve=_resolver(
            **{
                "op.datahub.test": "127.0.0.1",
                "oz.datahub.test": "127.0.0.1",
                "src.datahub.test": "127.0.0.2",
            }
        ),
    )

    assert socket.getaddrinfo is not before  # pinned
    for host, address in (
        ("op.datahub.test", "127.0.0.1"),
        ("oz.datahub.test", "127.0.0.1"),
        ("src.datahub.test", "127.0.0.2"),
    ):
        assert {info[4][0] for info in socket.getaddrinfo(host, 443)} == {address}
    assert os.environ["NO_PROXY"] == "*" and os.environ["no_proxy"] == "*"


def test_the_files_test_pins_its_hosts_and_ignores_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    live = _load("files")
    _env(
        monkeypatch,
        ONEDATA_ONEPROVIDER_HOST="https://op.datahub.test",
        ONEDATA_ONEZONE_HOST="http://localhost:8080",
    )

    live.enforce_local(
        monkeypatch, resolve=_resolver(**{"op.datahub.test": "127.0.0.1", "localhost": "127.0.0.1"})
    )

    assert {info[4][0] for info in socket.getaddrinfo("op.datahub.test", 443)} == {"127.0.0.1"}
    assert os.environ["NO_PROXY"] == "*"


def test_the_files_bad_token_pin_names_a_client_error_never_a_server_error() -> None:
    """The made-up pagination token must answer a 4xx: a 5xx cannot satisfy the pin (the swagger
    lists 400; the error id is a TODO for the first live run)."""
    live = _load("files")

    assert 400 <= live.BAD_TOKEN_STATUS < 500
    assert live.BAD_TOKEN_ERROR_ID is None or isinstance(live.BAD_TOKEN_ERROR_ID, str)
