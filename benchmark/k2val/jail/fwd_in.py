"""The INSIDE forwarder of the CLI jail, and the launcher of the CLI.

    /usr/bin/python3 -I fwd_in.py --listen 127.0.0.1:<P> --socket /run/k2val/mcp.sock -- <argv...>

It runs INSIDE the jail's own network namespace, under the jail's /usr/bin/python3. It binds
127.0.0.1:<P> there (the jail's loopback, not the host's), then forks:

- the child relays each connection to the UNIX socket that the outside forwarder serves;
- the parent execs <argv>, so the CLI is the jail's main process and its exit ends the jail.

Binding before the fork means the port already listens when the CLI starts. The child dies
with the CLI (PR_SET_PDEATHSIG), and the jail's PID namespace ends with it anyway.

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


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def relay(a_reader, a_writer, b_reader, b_writer) -> None:
    try:
        await asyncio.gather(pipe(a_reader, b_writer), pipe(b_reader, a_writer))
    finally:
        for w in (a_writer, b_writer):
            w.close()
            with contextlib.suppress(ConnectionError, OSError):
                await w.wait_closed()


def make_handler(socket_path: str):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_unix_connection(socket_path)
        except OSError:
            writer.close()
            return
        await relay(reader, writer, up_reader, up_writer)

    return handle


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
