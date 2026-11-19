"""`k2val scan` and the loopback MCP server of the CLI legs."""

from __future__ import annotations

import asyncio
import json
import re
import socket
from pathlib import Path

import httpx
import pytest
from fastmcp import Client, FastMCP
from starlette.middleware import Middleware

from benchmark.k2val.config import ConfigError
from benchmark.k2val.mcp_http import (
    McpHttpServer,
    make_recording_middleware,
    pick_port,
)
from benchmark.k2val.scan import (
    FastMcpHttpCaller,
    build_record,
    collect_sources,
    compile_patterns,
    read_lines,
    scan_texts,
    write_scan_record,
)
from onedata_mcp._dns_rebinding import DnsRebindingProtection
from test.unit.k2val.conftest import TOKEN_LOCAL

# --- the scan -----------------------------------------------------------------------------


def test_scan_counts_tokens_case_insensitively_and_patterns_per_category() -> None:
    sources = {
        "prompts": "nothing here",
        "answer:list_space_providers": "Provider ACME-Lab at host-7 and again acme-lab",
        "ids": "id-0042 id-0043",
    }
    result = scan_texts(sources, ["acme-lab"], compile_patterns([r"id-00\d\d"]))

    assert result.categories == {
        "prompts": {"tokens": 0, "patterns": 0},
        "answer:list_space_providers": {"tokens": 2, "patterns": 0},
        "ids": {"tokens": 0, "patterns": 2},
    }
    assert result.total == 4


def test_the_record_and_the_printed_counts_never_hold_the_matched_text(tmp_path, cfg) -> None:
    secret = "ACME-LAB-INTERNAL-NAME"
    conf = cfg.models["gpt-luna-low"]
    result = scan_texts({"answer:check_readiness": f"see {secret}"}, [secret.lower()], [])
    record = build_record(cfg, conf, result, 1, 0)

    path = write_scan_record(tmp_path, tmp_path / "run", record)

    for written in (path, tmp_path / "run" / "scan-gpt-luna-low.json"):
        text = written.read_text()
        assert secret not in text and secret.lower() not in text
        assert json.loads(text)["categories"]["answer:check_readiness"]["tokens"] == 1
    assert record["clean"] is False and record["total_matches"] == 1


def test_a_scan_with_an_empty_token_list_is_never_clean(cfg) -> None:
    conf = cfg.models["gpt-luna-low"]
    record = build_record(cfg, conf, scan_texts({"prompts": "x"}, [], []), 0, 0)
    assert record["total_matches"] == 0 and record["clean"] is False


def test_token_and_pattern_files(tmp_path: Path) -> None:
    f = tmp_path / "tokens.txt"
    f.write_text("alpha\n\n# a comment\n  beta  \n")
    assert read_lines(str(f)) == ["alpha", "beta"]
    assert read_lines("") == []
    with pytest.raises(ConfigError) as err:
        compile_patterns(["fine", "(unclosed-SECRET"])
    assert "line 2" in str(err.value) and "SECRET" not in str(err.value)


class FakeCaller:
    def __init__(self, leak: str | None = None) -> None:
        self.leak = leak
        self.calls: list[tuple[str, dict]] = []

    async def list_definitions(self) -> str:
        return json.dumps([{"name": "check_readiness", "description": "Check whether..."}])

    async def call(self, name: str, arguments: dict) -> str:
        self.calls.append((name, arguments))
        return json.dumps({"tool": name, "note": self.leak or "ok"})


@pytest.mark.asyncio
async def test_the_scan_collects_what_a_cli_will_see(cfg) -> None:
    conf = cfg.models["gpt-luna-low"]
    caller = FakeCaller(leak="vendor-sensitive-name")

    sources = await collect_sources(
        cfg,
        conf,
        caller,
        fixture_dir="/k2val-gpt/logs-scan-1",
        sample_file="/k2val-gpt/logs-scan-1/log_20200101.dat",
        ids=["pOwner", "pLocal", "nordic-marine-dc", "vistula-dc"],
    )

    assert set(sources) == {
        "prompts",
        "tool_definitions",
        "ids",
        "answer:list_space_providers",
        "answer:get_file_distribution",
        "answer:check_readiness",
    }
    assert [c[0] for c in caller.calls] == [
        "list_space_providers",
        "get_file_distribution",
        "check_readiness",
    ]
    assert caller.calls[0][1] == {"space_id": "k2val-gpt"}
    assert caller.calls[2][1] == {"extent": [{"dataset": "/k2val-gpt/logs-scan-1"}]}
    # the prompts include the system prompt and every brief, rendered for this config's space
    assert "vistula-dc" in sources["prompts"] and "/k2val-gpt/logs-sample" in sources["prompts"]
    assert "pOwner" in sources["ids"] and "k2val-gpt" in sources["ids"]
    result = scan_texts(sources, ["vendor-sensitive-name"], [])
    assert {c for c, n in result.categories.items() if n["tokens"]} == {
        "answer:list_space_providers",
        "answer:get_file_distribution",
        "answer:check_readiness",
    }


# --- the loopback server ------------------------------------------------------------------


def test_the_server_command_has_no_secret_and_its_env_has_no_onedata_variable(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", TOKEN_LOCAL)
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", "https://zone.k2val.datahub.test")
    monkeypatch.setenv("UNRELATED", "kept")
    server = McpHttpServer("/cfg/k2val.toml", frozenset({"b", "a"}), tmp_path)
    server.port = 41999

    cmd = server.command()

    assert cmd[1:3] == ["-m", "benchmark.k2val.mcp_http"]
    assert cmd[cmd.index("--tools") + 1] == "a,b"
    assert cmd[cmd.index("--config") + 1] == "/cfg/k2val.toml"
    assert TOKEN_LOCAL not in " ".join(cmd)
    env = McpHttpServer.child_env()
    assert not any(k.startswith("ONEDATA_") for k in env) and env["UNRELATED"] == "kept"
    assert server.url == "http://127.0.0.1:41999/mcp"


def test_pick_port_is_loopback_and_free() -> None:
    port = pick_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def dummy_app(record_path: Path) -> FastMCP:
    app = FastMCP("dummy")

    @app.tool
    def ping(x: int) -> dict:
        return {"ready": False, "x": x}

    app.add_middleware(make_recording_middleware(record_path))
    return app


@pytest.mark.asyncio
async def test_the_recording_middleware_keeps_every_call_with_its_times(tmp_path) -> None:
    record = tmp_path / "calls.jsonl"
    async with Client(dummy_app(record)) as client:
        await client.call_tool("ping", {"x": 7})
        await client.call_tool("ping", {"x": "not an int"}, raise_on_error=False)

    rows = [json.loads(line) for line in record.read_text().splitlines()]
    assert [r["tool"] for r in rows] == ["ping", "ping"]
    assert rows[0]["arguments"] == {"x": 7} and rows[0]["ok"] is True
    assert json.loads(rows[0]["result_text"]) == {"ready": False, "x": 7}
    assert rows[0]["started_at"] <= rows[0]["ended_at"]
    assert rows[1]["ok"] is False and rows[1]["error"]


@pytest.mark.asyncio
async def test_over_http_on_loopback_with_the_dns_rebinding_guard(tmp_path) -> None:
    """What a CLI leg relies on: a client reaches the server by its loopback URL, the calls are
    recorded, and a request with a foreign Host header is refused."""
    record = tmp_path / "calls.jsonl"
    app = dummy_app(record)
    port = pick_port()
    guard = [Middleware(DnsRebindingProtection, host="127.0.0.1", port=port)]
    task = asyncio.create_task(
        app.run_async(
            transport="http", host="127.0.0.1", port=port, middleware=guard, show_banner=False
        )
    )
    try:
        for _ in range(100):
            try:
                _, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        caller = FastMcpHttpCaller(f"http://127.0.0.1:{port}/mcp")

        answer = await caller.call("ping", {"x": 1})
        definitions = json.loads(await caller.list_definitions())

        assert json.loads(answer) == {"ready": False, "x": 1}
        assert [d["name"] for d in definitions] == ["ping"]
        async with httpx.AsyncClient() as http:
            evil = await http.post(
                f"http://127.0.0.1:{port}/mcp",
                headers={
                    "Host": "evil.example",
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )
        assert evil.status_code == 403
        rows = [json.loads(line) for line in record.read_text().splitlines()]
        assert [r["tool"] for r in rows] == ["ping"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_the_server_binds_loopback_only() -> None:
    import benchmark.k2val.mcp_http as mod

    src = Path(mod.__file__).read_text()
    assert 'HOST = "127.0.0.1"' in src
    assert re.search(r'MCP_HOST"\] = HOST', src)
