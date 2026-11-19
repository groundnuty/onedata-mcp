"""The CLI jail's helpers, without bwrap: the two forwarders, the netns ruleset and its check,
and cli-jail.sh's argument building and refusals. The live proof of the jail itself is
test/live/test_k2val_jail_live.py (the leak probe)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import ipaddress
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from benchmark.k2val.jail import (
    JailCleanupError,
    JailFileError,
    fwd_in,
    fwd_out,
    leak_probe,
    netns,
    read_jail_file,
    remove_tree,
)

JAIL_DIR = Path(fwd_out.__file__).resolve().parent
CLI_JAIL = JAIL_DIR / "cli-jail.sh"


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(b"echo:" + data)
        await writer.drain()
    writer.close()


@pytest.fixture
def short_dir():
    """A short directory for UNIX sockets (their paths are limited to about 108 bytes)."""
    with tempfile.TemporaryDirectory(prefix="k2j-", dir="/tmp") as d:
        yield Path(d)


# --- the outside forwarder ----------------------------------------------------------------


@pytest.mark.parametrize("value", ["127.0.0.1:8080", "localhost:1"])
def test_fwd_out_upstream_is_loopback_only(value) -> None:
    assert fwd_out.parse_upstream(value)[0] == "127.0.0.1"


@pytest.mark.parametrize(
    "value", ["10.0.0.1:80", "0.0.0.0:80", "example.org:443", "127.0.0.1", "127.0.0.1:0", ":80"]
)
def test_fwd_out_refuses_other_upstreams(value) -> None:
    with pytest.raises(ValueError):
        fwd_out.parse_upstream(value)


@pytest.mark.asyncio
async def test_fwd_out_relays_the_socket_to_the_upstream(short_dir) -> None:
    upstream = await asyncio.start_server(_echo, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    sock_path = str(short_dir / "mcp.sock")
    ready_r, ready_w = os.pipe()
    stop = asyncio.Event()
    task = asyncio.create_task(fwd_out.serve(sock_path, "127.0.0.1", port, ready_w, stop))
    try:
        await asyncio.get_running_loop().run_in_executor(None, os.read, ready_r, 1)
        assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600  # owner only
        reader, writer = await asyncio.open_unix_connection(sock_path)
        writer.write(b"POST /mcp HTTP/1.1\r\n")
        await writer.drain()
        assert await reader.readexactly(25) == b"echo:POST /mcp HTTP/1.1\r\n"
        writer.close()
    finally:
        stop.set()
        await task
        upstream.close()
        os.close(ready_r)
    assert not os.path.exists(sock_path)  # removed on stop


# --- the inside forwarder -----------------------------------------------------------------


def test_fwd_in_arguments_are_strict() -> None:
    assert fwd_in.parse_args(
        ["--listen", "127.0.0.1:4000", "--socket", "/run/k2val/mcp.sock", "--", "/opt/cli/x", "-a"]
    ) == (("127.0.0.1", 4000), "/run/k2val/mcp.sock", ["/opt/cli/x", "-a"])
    for bad in (
        ["--listen", "0.0.0.0:4000", "--socket", "/s", "--", "x"],  # only the jail's loopback
        ["--listen", "127.0.0.1:4000", "--socket", "/s"],  # no command
        ["--listen", "127.0.0.1:4000", "--socket", "/s", "--"],
        ["--listen", "127.0.0.1:4000", "--", "x"],
        ["--listen", "127.0.0.1:4000", "--socket", "/s", "--extra", "1", "--", "x"],
    ):
        with pytest.raises(ValueError):
            fwd_in.parse_args(bad)


CLIENT = """
import socket, sys
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
s.sendall(b"hello")
got = b""
while len(got) < 10:
    got += s.recv(100)
print(got.decode())
"""


@pytest.mark.asyncio
async def test_fwd_in_listens_before_it_execs_the_command_and_relays_to_the_socket(
    short_dir,
) -> None:
    """The real launcher: bind, fork the relay, exec the command (here a client of the port)."""
    sock_path = str(short_dir / "mcp.sock")
    server = await asyncio.start_unix_server(_echo, path=sock_path)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(JAIL_DIR / "fwd_in.py"),
        "--listen",
        f"127.0.0.1:{port}",
        "--socket",
        sock_path,
        "--",
        sys.executable,
        "-c",
        CLIENT,
        str(port),
        stdout=asyncio.subprocess.PIPE,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 20)
    server.close()
    assert proc.returncode == 0
    assert out.decode().strip() == "echo:hello"


# --- both forwarders: teardown, connection cap, idle timeout --------------------------------


async def _forwarder(kind: str, upstream_handler, short_dir: Path, **kwargs):
    """(forwarder, open-a-client, close-all) for the outside (UNIX front, TCP upstream) or the
    inside (TCP front, UNIX upstream) forwarder."""
    if kind == "fwd_out":
        up = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        fwd = fwd_out.make_handler("127.0.0.1", up.sockets[0].getsockname()[1], **kwargs)
        path = str(short_dir / "front.sock")
        front = await asyncio.start_unix_server(fwd, path=path)

        def client():
            return asyncio.open_unix_connection(path)
    else:
        up_path = str(short_dir / "up.sock")
        up = await asyncio.start_unix_server(upstream_handler, path=up_path)
        fwd = fwd_in.make_handler(up_path, **kwargs)
        front = await asyncio.start_server(fwd, "127.0.0.1", 0)
        port = front.sockets[0].getsockname()[1]

        def client():
            return asyncio.open_connection("127.0.0.1", port)

    def close() -> None:
        front.close()
        up.close()

    return fwd, client, close


async def _until(cond, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def _hold(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """An upstream that never closes on its own (it waits for the forwarder to close it)."""
    with contextlib.suppress(Exception):
        while await reader.read(65536):
            pass
        await asyncio.sleep(30)


def test_the_forwarders_bound_connections_and_idle_time() -> None:
    for mod in (fwd_out, fwd_in):
        assert mod.MAX_CONNECTIONS == 16
        assert mod.IDLE_TIMEOUT_S > 20 * 60  # above the trial wall cap: never cuts a trial


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fwd_out", "fwd_in"])
async def test_an_upstream_close_tears_down_the_whole_connection(kind, short_dir) -> None:
    async def bye(reader, writer):
        writer.write(b"bye")
        await writer.drain()
        writer.close()

    fwd, client, close = await _forwarder(kind, bye, short_dir)
    try:
        reader, writer = await client()  # the client never closes its side
        assert await asyncio.wait_for(reader.read(3), 3) == b"bye"
        assert await asyncio.wait_for(reader.read(), 3) == b""
        assert await _until(lambda: fwd.active == 0), "the relay waits on the open side"
        writer.close()
    finally:
        close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fwd_out", "fwd_in"])
async def test_a_client_close_tears_down_the_upstream_side(kind, short_dir) -> None:
    fwd, client, close = await _forwarder(kind, _hold, short_dir)
    try:
        reader, writer = await client()
        writer.write(b"GET / HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert await _until(lambda: fwd.active == 1)
        writer.close()
        assert await _until(lambda: fwd.active == 0), "the relay waits on the upstream"
    finally:
        close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fwd_out", "fwd_in"])
async def test_connections_beyond_the_cap_are_closed(kind, short_dir) -> None:
    fwd, client, close = await _forwarder(kind, _hold, short_dir, max_connections=3)
    try:
        held = [await client() for _ in range(3)]
        assert await _until(lambda: fwd.active == 3)
        reader, writer = await client()
        assert await asyncio.wait_for(reader.read(), 3) == b""  # closed at once
        assert fwd.refused == 1 and fwd.active == 3
        for _, w in [*held, (reader, writer)]:
            w.close()
    finally:
        close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["fwd_out", "fwd_in"])
async def test_an_idle_connection_is_closed(kind, short_dir) -> None:
    fwd, client, close = await _forwarder(kind, _hold, short_dir, idle_timeout=0.3)
    try:
        reader, writer = await client()
        assert await asyncio.wait_for(reader.read(), 5) == b""
        assert await _until(lambda: fwd.active == 0)
        writer.close()
    finally:
        close()


# --- the egress filter ----------------------------------------------------------------------


def test_the_ruleset_rejects_every_blocked_range_and_the_dns_relay() -> None:
    rules = netns.ruleset(["93.184.216.0/24"])
    for net in netns.BLOCKED_V4:
        assert net in rules
    assert "93.184.216.0/24" in rules
    chain = rules[rules.index("chain output") :]
    assert chain.index('oifname "lo" accept') < chain.index("ip daddr @blocked_v4 reject")
    # no exception for slirp's DNS relay (it answers from the host's resolver)
    assert "10.0.2.3" not in rules and "dport 53" not in rules
    assert not netns.outside_blocked_ranges([f"{netns.SLIRP_DNS_RELAY}/32"])
    assert "meta nfproto ipv6 reject" in chain
    for must_block in ("169.254.169.254", "100.64.0.1", "10.0.2.2", "172.17.0.1", "192.168.1.1"):
        assert not netns.outside_blocked_ranges([f"{must_block}/32"])  # already blocked
    assert netns.outside_blocked_ranges(["8.8.8.0/24", "10.1.2.0/24"]) == ["8.8.8.0/24"]


IP_ADDR = json.dumps(
    [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
        {  # a public subnet: its neighbours must be blocked too, not only the host's /32
            "ifname": "eth0",
            "addr_info": [{"family": "inet", "local": "93.184.216.34", "prefixlen": 24}],
        },
        {
            "ifname": "eth1",
            "addr_info": [{"family": "inet", "local": "192.168.5.7", "prefixlen": 24}],
        },
        {  # a point-to-point tunnel with a public peer
            "ifname": "tun0",
            "addr_info": [
                {"family": "inet", "local": "10.8.0.2", "address": "198.18.0.9", "prefixlen": 32},
                {"family": "inet", "local": "5.6.7.8", "address": "45.33.32.156", "prefixlen": 32},
            ],
        },
    ]
)


def test_every_host_subnet_and_peer_outside_the_blocked_ranges_is_blocked() -> None:
    nets = netns.host_ipv4_networks(IP_ADDR)
    assert "93.184.216.0/24" in nets and "192.168.5.0/24" in nets and "127.0.0.0/8" in nets
    assert netns.outside_blocked_ranges(nets) == [
        "45.33.32.156/32",
        "5.6.7.8/32",
        "93.184.216.0/24",
    ]


@pytest.mark.parametrize("failure", ["missing", "exit 1", "timeout", "bad json", "empty"])
def test_the_filter_fails_closed_without_the_hosts_networks(monkeypatch, failure) -> None:
    def run(*a, **k):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("ip", 10)
        out = {"bad json": "not json", "empty": "[]"}.get(failure, IP_ADDR)
        return subprocess.CompletedProcess(a, 1 if failure == "exit 1" else 0, out, "")

    monkeypatch.setattr(
        netns.shutil, "which", lambda *a, **k: None if failure == "missing" else "/x/ip"
    )
    monkeypatch.setattr(netns.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        netns.host_ipv4_networks()
    # and the jail's filter step exits non-zero, before touching any namespace
    assert netns.main(["filter", "--pid", "999999"]) == 3


# --- the host's resolvers -----------------------------------------------------------------


def test_every_host_resolver_but_the_jails_own_is_blocked(tmp_path, monkeypatch) -> None:
    stub = tmp_path / "resolv.conf"
    stub.write_text("# stub\nnameserver 127.0.0.53\noptions edns0\nsearch example.internal\n")
    upstream = tmp_path / "upstream.conf"
    upstream.write_text("nameserver 192.0.2.10\nnameserver 8.8.8.8\nnameserver 2001:db8::1\n")
    monkeypatch.setattr(netns.shutil, "which", lambda *a, **k: None)  # no resolvectl
    found = netns.host_nameservers((str(stub), str(upstream)))
    assert found == ["127.0.0.53", "192.0.2.10", "8.8.8.8"]  # IPv4 only (all IPv6 is rejected)
    assert netns.resolvers_to_block(found) == ["127.0.0.53/32", "192.0.2.10/32"]

    # a PUBLIC resolver the host uses (an institution's, say) is blocked too
    monkeypatch.setattr(netns, "host_ipv4_networks", lambda: ["127.0.0.0/8"])
    monkeypatch.setattr(netns, "host_nameservers", lambda: ["9.9.9.9", "1.1.1.1", "127.0.0.53"])
    assert netns.host_blocked_ranges() == ["9.9.9.9/32"]
    assert "9.9.9.9/32" in netns.ruleset(netns.host_blocked_ranges())


def test_per_link_resolvers_are_read_from_resolvectl(tmp_path, monkeypatch) -> None:
    (tmp_path / "r").write_text("nameserver 127.0.0.53\n")
    out = "Global:\nLink 2 (eth0): 198.51.100.7 8.8.8.8\nLink 5 (tun0): 100.100.100.100 fd00::53\n"
    monkeypatch.setattr(netns.shutil, "which", lambda *a, **k: "/x/resolvectl")
    monkeypatch.setattr(
        netns.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, out, "")
    )
    assert netns.host_nameservers((str(tmp_path / "r"),)) == [
        "100.100.100.100",
        "127.0.0.53",
        "198.51.100.7",
        "8.8.8.8",
    ]


@pytest.mark.parametrize("failure", ["exit 1", "timeout"])
def test_a_failing_resolvectl_fails_closed(tmp_path, monkeypatch, failure) -> None:
    """Present but failing: its per-link resolvers would be missed, so the filter refuses."""
    (tmp_path / "r").write_text("nameserver 127.0.0.1\n")

    def run(*a, **k):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("resolvectl", 5)
        return subprocess.CompletedProcess(a, 1, "", "")

    monkeypatch.setattr(netns.shutil, "which", lambda *a, **k: "/x/resolvectl")
    monkeypatch.setattr(netns.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="resolvectl"):
        netns.host_nameservers((str(tmp_path / "r"),))


def test_an_unreadable_resolv_conf_fails_closed(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="cannot read"):
        netns.host_nameservers((str(tmp_path / "absent"),))


def test_the_jails_resolvers_are_the_same_everywhere() -> None:
    script = CLI_JAIL.read_text()
    line = next(x for x in script.splitlines() if "nameserver" in x and "printf" in x)
    assert re.findall(r"nameserver (\S+?)\\n", line) == list(netns.JAIL_RESOLVERS)
    assert leak_probe.JAIL_RESOLVERS == netns.JAIL_RESOLVERS


def test_the_probe_never_hands_a_host_derived_name_to_a_resolver(monkeypatch) -> None:
    """Inside the jail every lookup goes to PUBLIC resolvers: the probe must only ever resolve
    public vendor names and one neutral name, and otherwise use numeric addresses."""
    monkeypatch.setattr(socket, "gethostname", lambda: "host-of-someone")
    probe = leak_probe.CliProbe("codex", None)
    probe.nonce, probe.marker = "n", "m"
    upstream = leak_probe.Upstream("n")
    lo, any_ = leak_probe.Canary("127.0.0.1"), leak_probe.Canary("0.0.0.0")
    try:
        conf = probe.probe_conf(upstream, lo, any_)
    finally:
        upstream.close()
        lo.close()
        any_.close()
    assert "host_only_names" not in conf
    assert conf["dns_public_name"] == "example.com"
    assert set(conf["tls_hosts"]) <= {
        "api.openai.com",
        "chatgpt.com",
        "generativelanguage.googleapis.com",
    }
    for host in [t["host"] for t in conf["unreachable"]] + [r["ip"] for r in conf["dns_resolvers"]]:
        ipaddress.ip_address(host)  # numeric: never a name lookup
    resolver_ips = {r["ip"] for r in conf["dns_resolvers"]}
    assert {"10.0.2.3", "127.0.0.53", "100.100.100.100"} <= resolver_ips
    assert not resolver_ips & set(netns.JAIL_RESOLVERS)


def _listing(elems: list) -> str:
    return json.dumps(
        {
            "nftables": [
                {"table": {"family": "inet", "name": netns.TABLE}},
                {"set": {"name": "blocked_v4", "elem": elems}},
                {"chain": {"name": "output", "hook": "output"}},
                {"rule": {"expr": [{"match": {}}, {"reject": None}]}},
                {"rule": {"expr": [{"match": {}}, {"reject": None}]}},
            ]
        }
    )


def _prefix(net: str) -> dict:
    addr, length = net.split("/")
    return {"prefix": {"addr": addr, "len": int(length)}}


def test_the_table_check_reads_back_merged_ranges() -> None:
    elems = [_prefix(n) for n in netns.BLOCKED_V4 if n not in ("224.0.0.0/4", "240.0.0.0/4")]
    elems.append({"range": ["224.0.0.0", "255.255.255.255"]})  # nft auto-merges 224/4 + 240/4
    elems.append("203.0.113.9")
    netns.check_table(_listing(elems), ["203.0.113.9"])

    missing = [e for e in elems if e != _prefix("169.254.0.0/16")]
    with pytest.raises(RuntimeError, match="169.254.0.0/16"):
        netns.check_table(_listing(missing), [])
    no_chain = json.loads(_listing(elems))
    no_chain["nftables"] = [r for r in no_chain["nftables"] if "chain" not in r]
    with pytest.raises(RuntimeError, match="output chain"):
        netns.check_table(json.dumps(no_chain), [])


def test_slirp_gets_the_owning_user_namespace() -> None:
    assert netns.slirp_argv(["/usr/bin/slirp4netns", "--configure", "42", "tap0"], 7) == [
        "/usr/bin/slirp4netns",
        "--userns-path=/proc/self/fd/7",
        "--configure",
        "42",
        "tap0",
    ]
    with pytest.raises(ValueError):
        netns.slirp_argv(["slirp4netns", "--userns-path=/x"], 7)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["filter"],
        ["filter", "--pid", "1"],
        ["filter", "--pid", "x"],
        ["filter", "--pid", "42", "extra"],
        ["slirp", "--pid", "42"],
        ["slirp", "--pid", "42", "slirp4netns"],
        ["bogus", "--pid", "42"],
    ],
)
def test_netns_arguments_are_strict(argv) -> None:
    with pytest.raises(ValueError):
        netns.parse(argv)


# --- host-side reads of jail-written files -------------------------------------------------


def test_a_plain_jail_file_is_read_and_a_missing_one_is_none(tmp_path) -> None:
    (tmp_path / "last_message.txt").write_text("OK\n")
    assert read_jail_file(tmp_path, "last_message.txt") == "OK\n"
    assert read_jail_file(tmp_path, "absent.txt") is None
    with pytest.raises(ValueError):
        read_jail_file(tmp_path, "../x")


@pytest.mark.parametrize(
    "plant",
    [
        "symlink-to-host-file",
        "symlink-to-environ",
        "symlink-to-dev-zero",
        "fifo",
        "dir",
        "hardlink",
    ],
)
def test_a_jail_file_that_is_not_a_plain_file_is_refused_unread(tmp_path, plant) -> None:
    work = tmp_path / "work"
    work.mkdir()
    secret = tmp_path / "host-secret.token"
    secret.write_text("HOST-SECRET-VALUE")
    target = work / "last_message.txt"
    if plant == "symlink-to-host-file":
        target.symlink_to(secret)
    elif plant == "symlink-to-environ":
        target.symlink_to("/proc/self/environ")
    elif plant == "symlink-to-dev-zero":
        target.symlink_to("/dev/zero")
    elif plant == "fifo":
        os.mkfifo(target)  # a blocking read here would hang the harness
    elif plant == "dir":
        target.mkdir()
    else:
        os.link(work / ".." / "host-secret.token", target)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(read_jail_file, work, "last_message.txt")
        with pytest.raises(JailFileError):
            future.result(timeout=5)


def test_an_oversized_jail_file_is_refused(tmp_path) -> None:
    (tmp_path / "big").write_bytes(b"x" * 2048)
    with pytest.raises(JailFileError, match="larger"):
        read_jail_file(tmp_path, "big", limit=1024)
    assert read_jail_file(tmp_path, "big", limit=2048) == "x" * 2048


# --- removing what the jail wrote into ----------------------------------------------------


def test_remove_tree_removes_dirs_the_jail_stripped_of_permissions(tmp_path) -> None:
    root = tmp_path / "scratch"
    (root / "work" / "locked" / "inner").mkdir(parents=True)
    (root / "work" / "locked" / "inner" / "f").write_text("x")
    (root / "home" / ".codex").mkdir(parents=True)
    (root / "home" / ".codex" / "auth.json").write_text("login copy")
    for d in (
        root / "work" / "locked" / "inner",
        root / "work" / "locked",
        root / "home" / ".codex",
        root / "home",
        root / "work",
    ):
        d.chmod(0)
    # a symlink to a host dir: its permissions are never touched, the dir never removed
    outside = tmp_path / "host-dir"
    outside.mkdir()
    (outside / "keep").write_text("x")
    outside.chmod(0o500)
    (root / "link").symlink_to(outside)

    remove_tree(root)

    assert not root.exists()
    assert stat.S_IMODE(outside.stat().st_mode) == 0o500 and (outside / "keep").exists()
    outside.chmod(0o700)


def test_remove_tree_removes_a_chain_deeper_than_path_max_with_a_locked_bottom(tmp_path) -> None:
    """Python's recursive rmtree raises RecursionError here, and absolute paths pass PATH_MAX:
    the fallback (find -execdir, rm -rf) walks by relative names."""
    top = tmp_path / "deep"
    top.mkdir()
    fd = os.open(top, os.O_RDONLY)
    for _ in range(3000):
        os.mkdir("d", dir_fd=fd)
        nfd = os.open("d", os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = nfd
    os.close(os.open("f", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
    os.fchmod(fd, 0)
    os.close(fd)

    remove_tree(top)

    assert not top.exists()


def test_remove_tree_is_loud_when_something_is_left(tmp_path, monkeypatch) -> None:
    (tmp_path / "d").mkdir()
    monkeypatch.setattr("shutil.rmtree", lambda *a, **k: None)
    monkeypatch.setattr("subprocess.run", lambda *a, **k: None)  # no fallback either
    with pytest.raises(JailCleanupError, match="login"):
        remove_tree(tmp_path / "d")


# --- cli-jail.sh ------------------------------------------------------------------------------


def run_jail(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    base = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], "LANG": "C.UTF-8"}
    return subprocess.run(
        ["bash", str(CLI_JAIL), *args],
        capture_output=True,
        text=True,
        env={**base, **(env or {})},
        timeout=30,
    )


def print_args(*args: str) -> tuple[list[str], list[str], list[str]]:
    r = run_jail("--print-args", *args)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    i, j = lines.index("--- env"), lines.index("--- command")
    return lines[:i], lines[i + 1 : j], lines[j + 1 :]


def test_the_bwrap_arguments_isolate_the_cli() -> None:
    args, env, command = print_args(
        "--cli", "codex", "--workdir", "/tmp/w", "--mcp-upstream", "127.0.0.1:41999",
        "--", "codex", "exec", "--json", "hi",
    )  # fmt: skip
    for flag in (
        "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net",
        "--disable-userns", "--die-with-parent", "--new-session", "--info-fd", "--block-fd",
        "--sync-fd",
    ):  # fmt: skip
        assert flag in args
    assert "--share-net" not in args and "--unshare-all" not in args
    assert args[args.index("--hostname") + 1] == "jail"
    pairs = list(zip(args, args[1:], args[2:], strict=False))
    bind_flags = ("--bind", "--ro-bind", "--ro-bind-try")
    binds = [(a, src, dst) for a, src, dst in pairs if a in bind_flags]
    # read-write: only the fresh home and the work dir; nothing from the host's home or /mnt
    assert sorted((src, dst) for a, src, dst in binds if a == "--bind") == [
        ("/tmp/w", "/work"),
        ("<home>", "/home/agent"),
    ]
    host_sources = {src for _, src, _ in binds if not src.startswith(("<staging>", "<home>"))}
    # nothing of the host's /etc: its /etc/ssl holds local CAs and a certificate naming the host
    assert host_sources == {"/usr", "/tmp/w"}
    assert (
        "--ro-bind",
        "<staging>/etc/ca-certificates.crt",
        "/etc/ssl/certs/ca-certificates.crt",
    ) in binds
    assert ("--ro-bind", "<staging>/sock", "/run/k2val") in binds
    assert args[args.index("--tmpfs") + 1] == "/usr/local"  # hidden
    assert env == [
        "PATH=/usr/bin:/bin",
        "LANG=C.UTF-8",
        "HOME=/home/agent",
        "CODEX_HOME=/home/agent/.codex",
    ]
    assert command[:3] == ["/usr/bin/env", "-u", "PWD"]
    assert command[3:9] == [
        "/usr/bin/python3",
        "-I",
        "/opt/k2val/fwd_in.py",
        "--listen",
        "127.0.0.1:41999",
        "--socket",
    ]
    assert command[-4:] == ["/opt/cli/codex", "exec", "--json", "hi"]  # the staged binary


def test_without_an_upstream_there_is_no_socket_and_no_forwarder() -> None:
    args, env, command = print_args("--cli", "agy", "--workdir", "/tmp/w", "--", "agy", "-p=x")
    assert "/run/k2val" not in args
    assert env == ["PATH=/usr/bin:/bin", "LANG=C.UTF-8", "HOME=/home/agent"]
    assert command == ["/usr/bin/env", "-u", "PWD", "/opt/cli/agy", "-p=x"]


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--cli", "claude", "--workdir", "/tmp", "--", "x"], "--cli must be"),
        (["--cli", "codex", "--", "x"], "--workdir is required"),
        (["--cli", "codex", "--workdir", "/tmp"], "no CLI command"),
        (["--cli", "codex", "--workdir", "/tmp", "--bogus", "--", "x"], "unknown argument"),
        # removed: nothing of the jail's home is ever copied out
        (["--cli", "codex", "--workdir", "/tmp", "--keep-home", "/tmp/k", "--", "x"],
         "unknown argument"),
        (
            ["--cli", "codex", "--workdir", "/tmp", "--mcp-upstream", "10.0.0.1:80", "--", "x"],
            "127.0.0.1",
        ),
        (["--cli", "codex", "--workdir", "/tmp", "--home-file", "../x=/etc/hosts", "--", "x"],
         "plain relative path"),
        (["--cli", "codex", "--workdir", "/tmp", "--home-file", "/abs=/etc/hosts", "--", "x"],
         "plain relative path"),
        (["--cli", "codex", "--workdir", "/tmp", "--home-file", "a/./b=/etc/hosts", "--", "x"],
         "plain relative path"),
        (["--cli", "codex", "--workdir", "/tmp", "--home-file", "a=/nonexistent", "--", "x"],
         "not a regular file"),
    ],
)  # fmt: skip
def test_cli_jail_refuses_bad_arguments(args, needle) -> None:
    r = run_jail(*args)
    assert r.returncode == 2 and needle in r.stderr


def test_cli_jail_refuses_a_workdir_or_staging_root_that_names_a_private_path(tmp_path) -> None:
    home_dir = Path(os.environ["HOME"])
    for workdir in (str(home_dir), "/mnt", str(tmp_path)):  # tmp_path names the user
        r = run_jail("--cli", "codex", "--workdir", workdir, "--", "codex")
        assert r.returncode == 2, workdir
        assert "neutral temp path" in r.stderr or "does not exist" in r.stderr
    with tempfile.TemporaryDirectory(prefix="k2j-", dir="/tmp") as w:
        env = {"K2VAL_JAIL_TMPDIR": str(home_dir)}
        r = run_jail("--cli", "codex", "--workdir", w, "--", "codex", env=env)
        assert r.returncode == 2 and "staging root" in r.stderr


def test_cli_jail_refuses_a_home_root_that_is_not_a_tmpfs() -> None:
    """The login copies may never touch a disk."""
    with (
        tempfile.TemporaryDirectory(prefix="k2j-", dir="/tmp") as w,
        tempfile.TemporaryDirectory(prefix="k2j-", dir="/tmp") as disk,
    ):
        fstype = subprocess.run(["stat", "-f", "-c", "%T", disk], capture_output=True, text=True)
        if fstype.stdout.strip() == "tmpfs":
            pytest.skip("/tmp is a tmpfs on this host")
        r = run_jail(
            "--cli", "codex", "--workdir", w, "--", "codex", env={"K2VAL_JAIL_SHMDIR": disk}
        )
        assert r.returncode == 2 and "must be a tmpfs" in r.stderr


def test_cli_jail_refuses_a_non_empty_workdir() -> None:
    with tempfile.TemporaryDirectory(prefix="k2j-", dir="/tmp") as w:
        (Path(w) / "something").write_text("x")
        r = run_jail("--cli", "codex", "--workdir", w, "--", "codex")
        assert r.returncode == 2 and "must be empty" in r.stderr
