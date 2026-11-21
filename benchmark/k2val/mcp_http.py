"""The MCP server of a CLI leg: onedata-mcp over streamable HTTP on 127.0.0.1, OUTSIDE the jail.

Two halves.

`python -m benchmark.k2val.mcp_http --config k2val.toml --tools a,b --port N --record FILE`
is the server process. It reads the token files itself, sets the `ONEDATA_*` variables and
`ONEDATA_MCP_TOOLS` (the scenario's tool set) in its own environment, adds a middleware that
records every tool call (name, arguments, start and end time, the full answer) to FILE, and
runs `onedata_mcp.main.main()` with `MCP_TRANSPORT=http`, whose launcher puts the DNS-rebinding
guard in front: a request must carry `Host: 127.0.0.1:<port>` or `localhost:<port>`. No token is
ever on its command line, and the CLI is given only the loopback URL.

`McpHttpServer` is the harness side: it picks an ephemeral port, spawns the process (without
any token in the environment), waits until the port answers, and reads the records back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

from benchmark.k2val.recording import CallRecord
from benchmark.k2val.secrets import redact

HOST = "127.0.0.1"
START_TIMEOUT_S = 40.0


def pick_port() -> int:
    """An ephemeral loopback port (the server binds it a moment later)."""
    with socket.socket() as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def make_recording_middleware(path: Path):
    from fastmcp.server.middleware import Middleware

    from benchmark.llm_adapters.openai_compat import _result_to_text

    class RecordingMiddleware(Middleware):
        async def on_call_tool(self, context, call_next):
            message = context.message
            row = {
                "tool": getattr(message, "name", "unknown"),
                "arguments": dict(getattr(message, "arguments", None) or {}),
                "started_at": time.time(),
                "ok": False,
                "result_text": None,
                "error": None,
            }
            try:
                result = await call_next(context)
                row["ok"] = True
                row["result_text"] = _result_to_text(result)
                return result
            except Exception as e:
                row["error"] = str(e)
                raise
            finally:
                row["ended_at"] = time.time()
                with path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    return RecordingMiddleware()


def serve(argv: list[str]) -> int:
    os.environ["ONEDATA_MCP_DOTENV"] = "0"  # the repository's `.env` is never read here
    p = argparse.ArgumentParser(prog="python -m benchmark.k2val.mcp_http")
    p.add_argument("--config", required=True)
    p.add_argument("--tools", required=True, help="comma-separated tool names to expose")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--record", required=True, help="JSONL file of recorded tool calls")
    a = p.parse_args(argv)

    from benchmark.k2val.app import configure_environment
    from benchmark.k2val.config import load_config

    cfg = load_config(a.config)  # the host guard runs here
    configure_environment(cfg)  # reads the token files, sets ONEDATA_*, re-checks the hosts
    os.environ["ONEDATA_MCP_TOOLS"] = a.tools
    os.environ["MCP_TRANSPORT"] = "http"
    os.environ["MCP_HOST"] = HOST
    os.environ["MCP_PORT"] = str(a.port)
    Path(a.record).touch()

    from onedata_mcp.main import main as server_main
    from onedata_mcp.main import mcp

    # The surface the model will see must be the scenario's set exactly: a server that would
    # expose another tool (or miss one) refuses to start, and the trial is a harness error.
    wanted = {t for t in a.tools.split(",") if t}
    if problem := served_surface_problem(asyncio.run(mcp.list_tools()), wanted):
        print(f"mcp_http: {problem}", file=sys.stderr)
        return 3
    mcp.add_middleware(make_recording_middleware(Path(a.record)))
    server_main()
    return 0


def served_surface_problem(listing: list, wanted: set[str]) -> str | None:
    """Why the listing a server would serve is not the scenario's: other names, or, for a set
    with a recorded hash, other descriptions or schemas (the runner checks the in-process
    listing before dispatch; this checks the one the Claude and CLI legs are served)."""
    if problem := surface_mismatch(wanted, {t.name for t in listing}):
        return problem
    from benchmark.k2val.surfaces import RECORDED_SURFACES, surface_problem

    if frozenset(wanted) in RECORDED_SURFACES:
        return surface_problem(listing, frozenset(wanted), RECORDED_SURFACES)[1]
    return None


def surface_mismatch(wanted: set[str], exposed: set[str]) -> str | None:
    problems = []
    if missing := sorted(wanted - exposed):
        problems.append(f"the server does not expose {missing}")
    if extra := sorted(exposed - wanted):
        problems.append(f"the server would also expose {extra}")
    return "; ".join(problems) or None


class McpHttpError(RuntimeError):
    pass


class McpHttpServer:
    """Async context manager around the server process of one CLI trial."""

    def __init__(self, config_path: str | Path, tools: frozenset[str], scratch: Path) -> None:
        self.config_path = str(config_path)
        self.tools = sorted(tools)
        self.scratch = scratch
        self.port = 0
        self.record_path = scratch / "mcp-calls.jsonl"
        self._log = scratch / "mcp-server.log"
        self._proc: asyncio.subprocess.Process | None = None

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/mcp"

    def command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "benchmark.k2val.mcp_http",
            "--config",
            self.config_path,
            "--tools",
            ",".join(self.tools),
            "--port",
            str(self.port),
            "--record",
            str(self.record_path),
        ]

    @staticmethod
    def child_env() -> dict[str, str]:
        """The server reads the token files itself: none travels in the environment."""
        return {k: v for k, v in os.environ.items() if not k.startswith("ONEDATA_")}

    async def __aenter__(self) -> McpHttpServer:
        self.port = pick_port()
        log = self._log.open("wb")
        self._proc = await asyncio.create_subprocess_exec(
            *self.command(), stdout=log, stderr=log, env=self.child_env()
        )
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                raise McpHttpError(f"MCP server exited early: {self._tail()}")
            try:
                _, writer = await asyncio.open_connection(HOST, self.port)
                writer.close()
                return self
            except OSError:
                await asyncio.sleep(0.3)
        await self.__aexit__(None, None, None)
        raise McpHttpError(f"MCP server did not listen on {HOST}:{self.port}: {self._tail()}")

    def _tail(self) -> str:
        try:
            lines = self._log.read_text(errors="replace").splitlines()[-15:]
        except OSError:
            return ""
        return redact(" | ".join(lines))

    async def __aexit__(self, *exc) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
                await proc.wait()

    def records(self) -> list[CallRecord]:
        out: list[CallRecord] = []
        if not self.record_path.exists():
            return out
        for i, line in enumerate(self.record_path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            out.append(
                CallRecord(
                    index=i,
                    tool=row["tool"],
                    arguments=row["arguments"],
                    started_at=row["started_at"],
                    ended_at=row.get("ended_at", 0.0),
                    ok=row["ok"],
                    result_text=row.get("result_text"),
                    error=row.get("error"),
                    source="mcp",
                )
            )
        return out


def new_scratch(prefix: str = "k2val-cli-") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1:]))
