"""The network of the CLI jail, set up from OUTSIDE it: the egress filter, then slirp4netns.

    python3 netns.py filter --pid <pid>                      # install + verify the nft filter
    python3 netns.py slirp --pid <pid> -- slirp4netns <args> # exec slirp in the right userns
    python3 netns.py print                                   # print the ruleset

<pid> is bwrap's child: a process in the jail's network namespace. bwrap (unprivileged) creates
a user namespace that owns the jail's netns, then puts the jailed process in a NESTED user
namespace, which has no capability over the netns. Both commands therefore work through the
OWNING user namespace (NS_GET_USERNS on the netns), not the process's own.

`filter`. slirp4netns gives the jail outbound internet, and `--disable-host-loopback` stops it
reaching the host's 127.0.0.1. It does NOT stop the jail reaching the host's other addresses
(LAN, container bridges, VPN and tailnet interfaces), other hosts on those networks, or the
cloud metadata service at 169.254.169.254: slirp opens those connections from the host. This
filter closes that gap. It joins the owning user namespace and the netns, keeps CAP_NET_ADMIN
across exec as an ambient capability, runs `nft -f -` there, and reads the table back to check
it. The jail script runs it while the netns is still offline, and starts nothing if it fails.

The ruleset (OUTPUT hook): loopback accepted (the in-jail forwarder); DNS to slirp's resolver
10.0.2.3 accepted; every private, shared, link-local, loopback, reserved and multicast IPv4
range rejected, plus the whole subnet of every host interface (local/prefixlen, so the host's
neighbours too) and any point-to-point peer; all IPv6 rejected; the rest (public IPv4) accepted.
The host's networks come from `ip -j addr`: if `ip` is missing, fails or times out, the filter
fails, and so does the jail (fail closed).

`slirp`. Opens the owning user namespace, keeps that fd open across exec, and execs the given
slirp4netns command with `--userns-path=/proc/self/fd/<fd>` added (slirp would otherwise join
the process's nested user namespace and fail to enter the netns).

Standard library only.
"""

from __future__ import annotations

import ctypes
import fcntl
import ipaddress
import json
import os
import shutil
import subprocess
import sys

TABLE = "k2val_egress"
SLIRP_DNS = "10.0.2.3"
NS_GET_USERNS = 0xB701
CAP_NET_ADMIN = 12
PR_CAP_AMBIENT = 47
PR_CAP_AMBIENT_RAISE = 2
LINUX_CAPABILITY_VERSION_3 = 0x20080522
SBIN = ":/usr/sbin:/sbin"
USAGE = (
    "usage: netns.py filter --pid <pid> | netns.py slirp --pid <pid> -- <slirp4netns argv...>"
    " | netns.py print"
)

# Never reachable from the jail (RFC 6890 special-purpose IPv4, and multicast/reserved).
BLOCKED_V4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.88.99.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


# --- the ruleset --------------------------------------------------------------------------------


def ip_addr_json() -> str:
    """`ip -j -4 addr show` of the HOST. Fails closed (RuntimeError): without the host's own
    networks the filter would be incomplete, and the jail must then not start."""
    ip = shutil.which("ip", path=os.environ.get("PATH", "") + SBIN)
    if not ip:
        raise RuntimeError("`ip` not found: the host's own networks cannot be listed")
    try:
        r = subprocess.run(
            [ip, "-j", "-4", "addr", "show"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"`ip -j addr` failed: {e}") from None
    if r.returncode != 0:
        raise RuntimeError(f"`ip -j addr` exited {r.returncode}")
    return r.stdout


def host_ipv4_networks(text: str | None = None) -> list[str]:
    """Every IPv4 network on the host's interfaces, as its subnet (local/prefixlen: the host's
    neighbours too), plus a point-to-point peer as /32. Fails closed (RuntimeError)."""
    try:
        rows = json.loads(ip_addr_json() if text is None else text)
        nets: set[str] = set()
        for row in rows:
            for a in row.get("addr_info", []):
                if a.get("family") != "inet" or not a.get("local"):
                    continue
                prefix = int(a.get("prefixlen", 32))
                nets.add(str(ipaddress.ip_network(f"{a['local']}/{prefix}", strict=False)))
                peer = a.get("address")
                if peer and peer != a["local"]:
                    nets.add(str(ipaddress.ip_network(f"{peer}/32")))
    except (ValueError, TypeError, AttributeError, KeyError) as e:
        raise RuntimeError(f"cannot read the host's networks from `ip -j addr`: {e}") from None
    if not nets:
        raise RuntimeError("`ip -j addr` listed no IPv4 network (not even loopback)")
    return sorted(nets)


def outside_blocked_ranges(networks: list[str]) -> list[str]:
    """The host networks not already inside a blocked range (a public subnet, say)."""
    blocked = [ipaddress.ip_network(n) for n in BLOCKED_V4]
    return [n for n in networks if not any(ipaddress.ip_network(n).subnet_of(b) for b in blocked)]


def ruleset(extra_blocked: list[str] | tuple[str, ...] = ()) -> str:
    """The table; `extra_blocked` are CIDR networks (the host's own, outside BLOCKED_V4)."""
    blocked = ", ".join([*BLOCKED_V4, *extra_blocked])
    return f"""table inet {TABLE} {{
    set blocked_v4 {{
        type ipv4_addr
        flags interval
        auto-merge
        elements = {{ {blocked} }}
    }}
    chain output {{
        type filter hook output priority 0; policy accept;
        oifname "lo" accept
        ip daddr {SLIRP_DNS} udp dport 53 accept
        ip daddr {SLIRP_DNS} tcp dport 53 accept
        ip daddr @blocked_v4 reject
        meta nfproto ipv6 reject
    }}
}}
"""


def _element_range(elem) -> tuple[int, int]:
    """An element of an nft interval set, as `nft -j` reads it back: an address, a prefix
    (`{"prefix": {"addr", "len"}}`) or a range (`{"range": [lo, hi]}`)."""
    if isinstance(elem, dict) and "elem" in elem:
        elem = elem["elem"].get("val", elem["elem"])
    if isinstance(elem, str):
        a = int(ipaddress.ip_address(elem))
        return a, a
    if "prefix" in elem:
        n = ipaddress.ip_network(f"{elem['prefix']['addr']}/{elem['prefix']['len']}")
        return int(n.network_address), int(n.broadcast_address)
    if "range" in elem:
        lo, hi = elem["range"]
        return int(ipaddress.ip_address(lo)), int(ipaddress.ip_address(hi))
    raise ValueError(f"unexpected set element {elem!r}")


def check_table(listing: str, extra_blocked: list[str]) -> None:
    """The table read back from the netns holds the chain on the output hook, the reject rules,
    and a set covering every blocked range."""
    rules = json.loads(listing)["nftables"]
    chains = [r["chain"] for r in rules if "chain" in r]
    if not any(c["name"] == "output" and c.get("hook") == "output" for c in chains):
        raise RuntimeError("the output chain is missing after nft -f")
    sets = [r["set"] for r in rules if "set" in r and r["set"]["name"] == "blocked_v4"]
    if not sets:
        raise RuntimeError("the blocked set is missing after nft -f")
    covered = [_element_range(e) for e in sets[0].get("elem", [])]
    for net in [*BLOCKED_V4, *extra_blocked]:
        n = ipaddress.ip_network(net)
        lo, hi = int(n.network_address), int(n.broadcast_address)
        if not any(a <= lo and hi <= b for a, b in covered):
            raise RuntimeError(f"blocked range {net} missing after nft -f")
    reject = [r for r in rules if "rule" in r and "reject" in json.dumps(r["rule"]["expr"])]
    if len(reject) < 2:
        raise RuntimeError("the reject rules are missing after nft -f")


# --- namespaces ---------------------------------------------------------------------------------


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _fail(what: str) -> None:
    err = ctypes.get_errno()
    raise OSError(err, f"{what}: {os.strerror(err)}")


def owning_userns(pid: int) -> tuple[int, int]:
    """(netns fd, fd of the user namespace that owns it)."""
    net_fd = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
    return net_fd, fcntl.ioctl(net_fd, NS_GET_USERNS)


def enter_netns(pid: int) -> None:
    """Join the owning user namespace, then the netns, and keep CAP_NET_ADMIN for the programs
    run from here (an ambient capability survives exec for a non-root uid)."""
    net_fd, user_fd = owning_userns(pid)
    os.setns(user_fd, os.CLONE_NEWUSER)
    os.setns(net_fd, os.CLONE_NEWNET)
    os.close(user_fd)
    os.close(net_fd)
    libc = ctypes.CDLL(None, use_errno=True)
    header = _CapHeader(LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapData * 2)()
    if libc.capget(ctypes.byref(header), data) != 0:
        _fail("capget")
    data[0].inheritable |= 1 << CAP_NET_ADMIN
    if libc.capset(ctypes.byref(header), data) != 0:
        _fail("capset")
    if libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, CAP_NET_ADMIN, 0, 0) != 0:
        _fail("prctl(PR_CAP_AMBIENT_RAISE)")


def install_filter(pid: int) -> None:
    extra = outside_blocked_ranges(host_ipv4_networks())  # read in the HOST netns, first
    nft = shutil.which("nft", path=os.environ.get("PATH", "") + SBIN)
    if not nft:
        raise RuntimeError("nft not found")
    enter_netns(pid)
    subprocess.run([nft, "-f", "-"], input=ruleset(extra), text=True, check=True, timeout=20)
    listing = subprocess.run(
        [nft, "-j", "list", "table", "inet", TABLE],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    ).stdout
    check_table(listing, extra)


def slirp_argv(argv: list[str], userns_fd: int) -> list[str]:
    """The slirp4netns command with the owning user namespace given by fd."""
    if not argv or any(a.startswith("--userns-path") for a in argv):
        raise ValueError("expected a slirp4netns command without --userns-path")
    return [argv[0], f"--userns-path=/proc/self/fd/{userns_fd}", *argv[1:]]


def exec_slirp(pid: int, argv: list[str]) -> None:
    net_fd, user_fd = owning_userns(pid)
    os.close(net_fd)
    os.set_inheritable(user_fd, True)
    command = slirp_argv(argv, user_fd)
    os.execv(command[0], command)


def parse(argv: list[str]) -> tuple[str, int, list[str]]:
    if argv[:1] == ["print"] and len(argv) == 1:
        return "print", 0, []
    if len(argv) < 3 or argv[0] not in ("filter", "slirp") or argv[1] != "--pid":
        raise ValueError(USAGE)
    if not argv[2].isdigit() or int(argv[2]) <= 1:
        raise ValueError("--pid must be a process id > 1")
    rest = argv[3:]
    if argv[0] == "filter" and rest:
        raise ValueError(USAGE)
    if argv[0] == "slirp" and (rest[:1] != ["--"] or len(rest) < 2):
        raise ValueError(USAGE)
    return argv[0], int(argv[2]), rest[1:]


def main(argv: list[str] | None = None) -> int:
    try:
        command, pid, rest = parse(sys.argv[1:] if argv is None else argv)
    except ValueError as e:
        print(f"netns: {e}", file=sys.stderr)
        return 2
    try:
        if command == "print":
            print(ruleset(outside_blocked_ranges(host_ipv4_networks())), end="")
        elif command == "filter":
            install_filter(pid)
        else:
            exec_slirp(pid, rest)  # does not return
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as e:
        print(f"netns: {command} failed: {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
