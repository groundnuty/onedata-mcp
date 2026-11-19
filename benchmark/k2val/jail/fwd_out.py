"""The OUTSIDE forwarder of the CLI jail: a UNIX socket in front of the loopback MCP server.

    python3 fwd_out.py --socket <dir>/mcp.sock --upstream 127.0.0.1:<P> \
        --ready-fd N --parent-pid PID

Each connection accepted on the socket is relayed, byte for byte, to the MCP server on
127.0.0.1:<P>. The socket file is bind-mounted into the jail; the jail itself has its own
network namespace and cannot reach the host's loopback. The upstream must be a loopback
address: the forwarder relays to nothing else.

It writes one byte to `--ready-fd` once the socket listens, and dies with the jail script
(PR_SET_PDEATHSIG, and a check that `--parent-pid` is still its parent). Standard library only.

The jailed side is untrusted, so a connection is torn down WHOLE on the first EOF or error on
either side (no half-closed connection waits forever on the other), at most MAX_CONNECTIONS are
open at once (more are closed on accept), and a connection with no bytes either way for
IDLE_TIMEOUT_S is closed. That timeout is above a trial's 20-minute wall cap on purpose: a tool
call may run 300 s silently, and an MCP GET stream may idle for a whole trial.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import os
import signal
import sys
from collections.abc import Awaitable, Callable

LOOPBACK = ("127.0.0.1", "localhost")
CHUNK = 65536
PR_SET_PDEATHSIG = 1
MAX_CONNECTIONS = 16
IDLE_TIMEOUT_S = 1500.0
CLOSE_TIMEOUT_S = 2.0

Streams = tuple[asyncio.StreamReader, asyncio.StreamWriter]


def parse_upstream(value: str) -> tuple[str, int]:
    """`127.0.0.1:<port>` (or `localhost:<port>`); anything else is refused."""
    host, sep, port = value.rpartition(":")
    if not sep or host not in LOOPBACK or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"upstream must be 127.0.0.1:<port>, got {value!r}")
    return "127.0.0.1", int(port)


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, seen: list) -> None:
    """One direction, until EOF or an error (either ends the whole connection)."""
    loop = asyncio.get_running_loop()
    while data := await reader.read(CHUNK):
        seen[0] = loop.time()
        writer.write(data)
        await writer.drain()


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(writer.wait_closed(), CLOSE_TIMEOUT_S)


async def relay(a: Streams, b: Streams, idle_timeout: float = IDLE_TIMEOUT_S) -> None:
    """Both directions at once. The first EOF or error on either side, or `idle_timeout` with no
    byte either way, cancels the other direction and closes BOTH sides."""
    loop = asyncio.get_running_loop()
    seen = [loop.time()]
    tasks = [
        asyncio.create_task(_copy(a[0], b[1], seen)),
        asyncio.create_task(_copy(b[0], a[1], seen)),
    ]
    try:
        while True:
            done, _ = await asyncio.wait(
                tasks, timeout=min(idle_timeout, 1.0), return_when=asyncio.FIRST_COMPLETED
            )
            if done or loop.time() - seen[0] >= idle_timeout:
                break
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(_close(a[1]), _close(b[1]))


class Forwarder:
    """The connection handler: relays each accepted connection to `connect()`'s, at most
    `max_connections` at once. `active` and `refused` are counts (for the tests)."""

    def __init__(
        self,
        connect: Callable[[], Awaitable[Streams]],
        max_connections: int = MAX_CONNECTIONS,
        idle_timeout: float = IDLE_TIMEOUT_S,
    ) -> None:
        self.connect = connect
        self.max_connections = max_connections
        self.idle_timeout = idle_timeout
        self.active = 0
        self.refused = 0

    async def __call__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.active >= self.max_connections:
            self.refused += 1
            await _close(writer)
            return
        self.active += 1
        try:
            try:
                upstream = await self.connect()
            except OSError:
                await _close(writer)
                return
            await relay((reader, writer), upstream, self.idle_timeout)
        finally:
            self.active -= 1


def make_handler(host: str, port: int, **kwargs) -> Forwarder:
    return Forwarder(lambda: asyncio.open_connection(host, port), **kwargs)


def die_with_parent(parent_pid: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    if parent_pid and os.getppid() != parent_pid:  # the parent died before the prctl
        sys.exit(0)


async def serve(
    socket_path: str,
    host: str,
    port: int,
    ready_fd: int | None,
    stop: asyncio.Event | None = None,
) -> None:
    """Serve until `stop` is set (by default: until SIGTERM, SIGINT or SIGHUP)."""
    old = os.umask(0o177)  # the socket file: owner only
    try:
        server = await asyncio.start_unix_server(make_handler(host, port), path=socket_path)
    finally:
        os.umask(old)
    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            loop.add_signal_handler(sig, stop.set)
    if ready_fd is not None:
        os.write(ready_fd, b"1")
        os.close(ready_fd)
    try:
        await stop.wait()
    finally:
        # no wait for open connections: asyncio.run cancels their tasks on the way out
        server.close()
        with contextlib.suppress(OSError):
            os.unlink(socket_path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fwd_out.py")
    p.add_argument("--socket", required=True)
    p.add_argument("--upstream", required=True)
    p.add_argument("--ready-fd", type=int)
    p.add_argument("--parent-pid", type=int, default=0)
    a = p.parse_args(argv)
    try:
        host, port = parse_upstream(a.upstream)
    except ValueError as e:
        print(f"fwd_out: {e}", file=sys.stderr)
        return 2
    die_with_parent(a.parent_pid)
    asyncio.run(serve(a.socket, host, port, a.ready_fd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
