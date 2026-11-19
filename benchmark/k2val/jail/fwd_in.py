"""The INSIDE forwarder of the CLI jail, and the launcher of the CLI.

    /usr/bin/python3 -I fwd_in.py --listen 127.0.0.1:<P> --socket /run/k2val/mcp.sock -- <argv...>

It runs INSIDE the jail's own network namespace, under the jail's /usr/bin/python3. It binds
127.0.0.1:<P> there (the jail's loopback, not the host's), then forks:

- the child relays each connection to the UNIX socket that the outside forwarder serves;
- the parent execs <argv>, so the CLI is the jail's main process and its exit ends the jail.

Binding before the fork means the port already listens when the CLI starts. The child dies
with the CLI (PR_SET_PDEATHSIG), and the jail's PID namespace ends with it anyway.

As in fwd_out.py: a connection is torn down WHOLE on the first EOF or error on either side, at
most MAX_CONNECTIONS are open at once, and one idle for IDLE_TIMEOUT_S (above a trial's wall cap)
is closed.

Self-contained, standard library only: this file is copied into the jail's staging directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import signal
import socket
import sys

CHUNK = 65536
PR_SET_PDEATHSIG = 1
MAX_CONNECTIONS = 16
IDLE_TIMEOUT_S = 1500.0
CLOSE_TIMEOUT_S = 2.0
USAGE = "usage: fwd_in.py --listen 127.0.0.1:<port> --socket <path> -- <argv...>"


def parse_args(argv: list[str]) -> tuple[tuple[str, int], str, list[str]]:
    """(listen address, socket path, command). Strict: the listen host must be 127.0.0.1."""
    if "--" not in argv:
        raise ValueError(USAGE)
    i = argv.index("--")
    opts, command = argv[:i], argv[i + 1 :]
    values = dict(zip(opts[0::2], opts[1::2], strict=False))
    if len(opts) != 4 or set(values) != {"--listen", "--socket"} or not command:
        raise ValueError(USAGE)
    host, sep, port = values["--listen"].rpartition(":")
    if host != "127.0.0.1" or not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"--listen must be 127.0.0.1:<port>, got {values['--listen']!r}")
    return (host, int(port)), values["--socket"], command


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


async def relay(a, b, idle_timeout: float = IDLE_TIMEOUT_S) -> None:
    """Both directions of (reader, writer) pairs `a` and `b`. The first EOF or error on either
    side, or `idle_timeout` with no byte either way, cancels the other direction and closes BOTH
    sides."""
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

    def __init__(self, connect, max_connections=MAX_CONNECTIONS, idle_timeout=IDLE_TIMEOUT_S):
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


def make_handler(socket_path: str, **kwargs) -> Forwarder:
    return Forwarder(lambda: asyncio.open_unix_connection(socket_path), **kwargs)


async def serve(listener: socket.socket, socket_path: str) -> None:
    server = await asyncio.start_server(make_handler(socket_path), sock=listener)
    async with server:
        await server.serve_forever()


def listen(address: tuple[str, int]) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(address)
    s.listen(64)
    return s  # non-inheritable (CLOEXEC), as Python creates every socket


def run_relay_child(listener: socket.socket, socket_path: str, parent: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    if os.getppid() != parent:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):  # the CLI's stdout/stderr stay the CLI's alone
        os.dup2(devnull, fd)
    try:
        asyncio.run(serve(listener, socket_path))
    finally:
        os._exit(0)


def main(argv: list[str]) -> int:
    try:
        address, socket_path, command = parse_args(argv)
    except ValueError as e:
        print(f"fwd_in: {e}", file=sys.stderr)
        return 2
    listener = listen(address)
    parent = os.getpid()
    if os.fork() == 0:
        run_relay_child(listener, socket_path, parent)
    listener.close()
    try:
        os.execv(command[0], command)
    except OSError as e:
        print(f"fwd_in: cannot start {command[0]}: {e.strerror}", file=sys.stderr)
        return 127
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
