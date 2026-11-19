"""The OUTSIDE forwarder of the CLI jail: a UNIX socket in front of the loopback MCP server.

    python3 fwd_out.py --socket <dir>/mcp.sock --upstream 127.0.0.1:<P> \
        --ready-fd N --parent-pid PID

Each connection accepted on the socket is relayed, byte for byte, to the MCP server on
127.0.0.1:<P>. The socket file is bind-mounted into the jail; the jail itself has its own
network namespace and cannot reach the host's loopback. The upstream must be a loopback
address: the forwarder relays to nothing else.

It writes one byte to `--ready-fd` once the socket listens, and dies with the jail script
(PR_SET_PDEATHSIG, and a check that `--parent-pid` is still its parent). Standard library only.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import os
import signal
import sys

LOOPBACK = ("127.0.0.1", "localhost")
CHUNK = 65536
PR_SET_PDEATHSIG = 1


def parse_upstream(value: str) -> tuple[str, int]:
    """`127.0.0.1:<port>` (or `localhost:<port>`); anything else is refused."""
    host, sep, port = value.rpartition(":")
    if not sep or host not in LOOPBACK or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"upstream must be 127.0.0.1:<port>, got {value!r}")
    return "127.0.0.1", int(port)


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy until EOF, then half-close the other side."""
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def relay(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    """Both directions at once; both sides are closed when both directions end."""
    try:
        await asyncio.gather(pipe(a_reader, b_writer), pipe(b_reader, a_writer))
    finally:
        for w in (a_writer, b_writer):
            w.close()
            with contextlib.suppress(ConnectionError, OSError):
                await w.wait_closed()


def make_handler(host: str, port: int):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            up_reader, up_writer = await asyncio.open_connection(host, port)
        except OSError:
            writer.close()
            return
        await relay(reader, writer, up_reader, up_writer)

    return handle


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
