"""The leak probe's INSIDE half: runs in the CLI jail in place of the CLI (`cli-jail.sh --probe`).

    /usr/bin/python3 -I /opt/k2val/probe_in.py /opt/k2val/probe.json

It checks what a jailed CLI could see and reach, and prints one JSON line per check
({"id", "ok", "detail"}). The outside half (leak_probe.py) writes probe.json, runs the jail, adds
its host-side checks and prints the table. In "sleep" mode it only starts a child that ignores
SIGTERM and sleeps, for the cleanup-on-signal checks. Self-contained, standard library only.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import time

CONNECT_TIMEOUT_S = 3.0


def emit(check_id: str, ok: bool, detail: str) -> None:
    print(json.dumps({"id": check_id, "ok": bool(ok), "detail": detail}), flush=True)


def read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError as e:
        return f"<unreadable: {e.strerror}>"


def listing(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def files_under(root: str) -> list[str]:
    out = []
    for d, _, files in os.walk(root):
        out += [os.path.relpath(os.path.join(d, f), root) for f in files]
    return sorted(out)


# --- (1) paths --------------------------------------------------------------------------------


def check_paths(conf: dict) -> None:
    present = [p for p in conf["forbidden_paths"] if os.path.lexists(p)]
    emit(
        "paths",
        not present,
        f"{len(conf['forbidden_paths'])} host paths absent"
        if not present
        else f"PRESENT: {present}",
    )
    tops = listing("/")
    emit("root", tops == sorted(conf["expected_root"]), f"/ = {tops}")
    etc = listing("/etc")
    emit("etc", etc == sorted(conf["expected_etc"]), f"/etc = {etc}")
    emit("home_dir", listing("/home") == ["agent"], f"/home = {listing('/home')}")
    emit(
        "work_empty",
        os.getcwd() == "/work" and listing("/work") == [],
        f"cwd={os.getcwd()} entries={len(listing('/work'))}",
    )
    emit(
        "usr_local", listing("/usr/local") == [], f"/usr/local entries={len(listing('/usr/local'))}"
    )


def check_cli_binaries() -> None:
    """/opt/cli holds the jail's own copies: no symlink (its target would name a host path), and
    no hard link (one inode shared with the host's binary)."""
    entries = listing("/opt/cli")
    bad = []
    for name in entries:
        st = os.lstat(f"/opt/cli/{name}")
        if stat.S_ISLNK(st.st_mode):
            bad.append(f"{name}: a symlink")
        elif not stat.S_ISREG(st.st_mode):
            bad.append(f"{name}: not a regular file")
        elif st.st_nlink != 1:
            bad.append(f"{name}: {st.st_nlink} links (an inode shared with the host)")
    emit("cli_binaries", bool(entries) and not bad,
         f"{len(entries)} files, own copies" if not bad else "; ".join(bad))  # fmt: skip


PEM_CERT = re.compile(rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.S)


def pem_certificates(data: bytes) -> list[bytes]:
    """The DER of every PEM certificate in `data`."""
    out = []
    for m in PEM_CERT.finditer(data):
        try:
            out.append(base64.b64decode(b"".join(m.group(1).split()), validate=True))
        except (ValueError, binascii.Error):
            continue
    return out


def check_etc_contents(conf: dict) -> None:
    """Nothing under the jail's /etc names the host or its user, and every CA certificate there is
    one of the distro's Mozilla set: no locally added CA, no host certificate (a snakeoil
    certificate carries the host name in its DER, so certificates are searched decoded too)."""
    mozilla = set(conf["mozilla_ca_sha256"])
    needles = [s.encode() for s in conf["forbidden_strings"] if s]
    bad, n_files, n_certs = [], 0, 0
    for d, _, files in os.walk("/etc"):
        for f in sorted(files):
            p = os.path.join(d, f)
            try:
                with open(p, "rb") as fh:
                    data = fh.read(32 << 20)
            except OSError:
                continue  # unreadable here is unreadable to the CLI as well
            n_files += 1
            certs = pem_certificates(data)
            n_certs += len(certs)
            for i, s in enumerate(needles):
                if any(s in blob for blob in (data, *certs)):
                    bad.append(f"{p}: host string #{i}")
            foreign = [c for c in certs if hashlib.sha256(c).hexdigest() not in mozilla]
            if foreign:
                bad.append(f"{p}: {len(foreign)} non-Mozilla certificate(s)")
    ssl_dirs = (listing("/etc/ssl"), listing("/etc/ssl/certs"))
    if ssl_dirs != (["certs"], ["ca-certificates.crt"]):
        bad.append(f"/etc/ssl = {ssl_dirs[0]}, /etc/ssl/certs has {len(ssl_dirs[1])} entries")
    more = f" (+{len(bad) - 6} more)" if len(bad) > 6 else ""
    detail = (
        "; ".join(bad[:6]) + more
        if bad
        else (f"{n_files} files, {n_certs} certificates, all Mozilla, no host string")
    )
    emit("etc_contents", not bad and n_certs > 0, detail)


def check_strings(conf: dict) -> None:
    sources = {
        "/proc/1/cmdline": read("/proc/1/cmdline"),
        "/proc/1/environ": read("/proc/1/environ"),
        "/proc/self/cmdline": read("/proc/self/cmdline"),
        "/proc/self/environ": read("/proc/self/environ"),
        "/proc/self/mountinfo": read("/proc/self/mountinfo"),
        "hostname": socket.gethostname(),
    }
    for name in ("resolv.conf", "hosts", "passwd", "group", "nsswitch.conf"):
        sources[f"/etc/{name}"] = read(f"/etc/{name}")
    hits = []
    for where, text in sources.items():
        for s in conf["forbidden_strings"]:
            if s and s in text:
                hits.append(f"{where} has forbidden string #{conf['forbidden_strings'].index(s)}")
    emit(
        "leak_strings",
        not hits,
        f"{len(sources)} sources x {len(conf['forbidden_strings'])} strings, no hit"
        if not hits
        else "; ".join(hits),
    )
    name = socket.gethostname()
    emit("hostname", name == "jail" and name != conf["host_hostname"], f"hostname={name}")


# --- (2) env, (3) home --------------------------------------------------------------------------


def check_env(conf: dict) -> None:
    names = sorted(os.environ)
    emit("env", names == sorted(conf["allowed_env"]), f"env names = {names}")


def check_home(conf: dict) -> None:
    home = os.environ.get("HOME", "")
    files = files_under(home) if home else []
    emit(
        "home",
        home == "/home/agent" and files == sorted(conf["expected_home_files"]),
        f"HOME={home} files={files}",
    )
    # the login copies never touch a disk: the home is a bind of a host tmpfs directory
    fstype = None
    for line in read("/proc/self/mountinfo").splitlines():
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) > 4 and fields[4] == home and right:
            fstype = right.split()[0]
    emit("home_tmpfs", fstype == "tmpfs", f"{home} is on {fstype}")


# --- (4) unreachable, (5) TLS, (6) MCP -----------------------------------------------------------


def connect_result(host: str, port: int) -> str:
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_S):
            return "OPEN"
    except TimeoutError:
        return "timeout"
    except OSError as e:
        return errno.errorcode.get(e.errno or 0, type(e).__name__)


def check_unreachable(conf: dict) -> None:
    for t in conf["unreachable"]:
        r = connect_result(t["host"], t["port"])
        emit(f"unreach:{t['label']}", r != "OPEN", f"{t['label']} -> {r}")


def check_tls(conf: dict) -> None:
    ctx = ssl.create_default_context()
    for host in conf["tls_hosts"]:
        try:
            with (
                socket.create_connection((host, 443), timeout=15) as raw,
                ctx.wrap_socket(raw, server_hostname=host) as s,
            ):
                emit(f"tls:{host}", True, f"{s.version()} {host}:443, certificate verified")
        except OSError as e:
            emit(f"tls:{host}", False, f"{host}: {type(e).__name__}: {e}")


def dns_answers(server: str, name: str, timeout: float = 3.0) -> bool:
    """Does `server` answer a plain A query for `name` over UDP? (A hand-built query: no
    resolver library, no search domains.)"""
    qid = os.getpid() & 0xFFFF
    qname = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    query = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.connect((server, 53))
            s.send(query)
            return s.recv(512)[:2] == query[:2]
    except OSError:  # timeout, or refused by the filter
        return False


def check_dns(conf: dict) -> None:
    """Every resolver on or near the host gets one query for a NEUTRAL public name and must not
    answer. No host-derived name is ever sent to any resolver."""
    name = conf["dns_public_name"]
    for res in conf["dns_resolvers"]:
        answered = dns_answers(res["ip"], name)
        emit(
            f"dns:{res['label']}",
            not answered,
            f"{name} -> {'ANSWERED' if answered else 'no answer'}",
        )


def check_mcp(conf: dict) -> None:
    mcp = conf.get("mcp")
    if not mcp:
        return
    try:
        c = http.client.HTTPConnection("127.0.0.1", mcp["port"], timeout=10)
        c.request("GET", "/probe")
        body = c.getresponse().read().decode("utf-8", "replace")
        ok = mcp["nonce"] in body
        emit(
            "mcp",
            ok,
            f"127.0.0.1:{mcp['port']} inside -> upstream answered "
            + ("with the nonce" if ok else "WITHOUT the nonce"),
        )
    except OSError as e:
        emit("mcp", False, f"127.0.0.1:{mcp['port']}: {type(e).__name__}: {e}")


# --- (7) processes, isolation ------------------------------------------------------------------


def check_processes(conf: dict) -> None:
    pids = sorted(int(p) for p in listing("/proc") if p.isdigit())
    cmdlines = {p: read(f"/proc/{p}/cmdline").strip() for p in pids}
    marker = conf["host_marker"]
    seen_marker = [p for p, c in cmdlines.items() if marker in c]
    first = cmdlines.get(1, "")
    is_bwrap = os.path.basename(first.split(" ")[0]) == "bwrap"
    ok = not seen_marker and is_bwrap and len(pids) <= conf["max_processes"]
    emit(
        "processes",
        ok,
        f"{len(pids)} processes visible, pid 1 is "
        f"{first.split(' ')[0] or '?'}, host marker seen: {bool(seen_marker)}",
    )


def check_fd_targets(conf: dict) -> None:
    """The target of every open fd of every process visible in the jail (bwrap's pid 1, the
    forwarder, the probe): none may name a host path (the home, the repo, the staging root, the
    host side of /work, ...). Pipes, sockets and anon inodes are fine. Paths are not printed."""
    forbidden = [s for s in conf["fd_forbidden"] if s]
    bad, n, unreadable, kinds = [], 0, [], set()
    for pid in sorted(int(p) for p in listing("/proc") if p.isdigit()):
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError as e:
            unreadable.append(f"pid {pid}: {errno.errorcode.get(e.errno or 0, '?')}")
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue  # closed meanwhile
            n += 1
            kinds.add(target.split(":", 1)[0] if not target.startswith("/") else "path")
            hits = [i for i, s in enumerate(forbidden) if s in target]
            if hits:
                bad.append(f"pid {pid} fd {fd}: a host path (forbidden #{hits[0]})")
    detail = "; ".join(bad) if bad else f"{n} fds, kinds {sorted(kinds)}, no host path"
    if unreadable:
        detail += f"; unreadable (so unreadable to the CLI too): {unreadable}"
    emit("fd_targets", not bad and n > 0, detail)


def check_isolation() -> None:
    status = dict(
        line.split(":\t", 1) for line in read("/proc/self/status").splitlines() if ":\t" in line
    )
    caps = {k: status.get(k, "?").strip() for k in ("CapEff", "CapPrm", "CapAmb")}
    nnp = status.get("NoNewPrivs", "?").strip()
    ok = all(int(v, 16) == 0 for v in caps.values() if v != "?") and nnp == "1"
    emit("caps", ok, f"{caps} NoNewPrivs={nnp}")

    try:
        os.unshare(os.CLONE_NEWUSER)
        emit("no_userns", False, "a new user namespace COULD be created")
    except OSError as e:
        emit("no_userns", True, f"unshare(CLONE_NEWUSER) -> {errno.errorcode.get(e.errno, e)}")

    for args in (["list", "ruleset"], ["flush", "ruleset"]):
        try:
            r = subprocess.run(["/usr/sbin/nft", *args], capture_output=True, text=True, timeout=10)
            ok = r.returncode != 0
            emit(f"nft_{args[0]}", ok, f"nft {' '.join(args)} -> exit {r.returncode}")
        except OSError as e:
            emit(f"nft_{args[0]}", True, f"nft not runnable: {e.strerror}")

    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        os.close(fd)
        emit("no_tty", False, "/dev/tty opened: the operator's terminal is reachable")
    except OSError as e:
        emit("no_tty", True, f"/dev/tty -> {errno.errorcode.get(e.errno, e)}")

    fds = sorted(int(f) for f in listing("/proc/self/fd"))
    targets = {
        f: os.readlink(f"/proc/self/fd/{f}") for f in fds if os.path.exists(f"/proc/self/fd/{f}")
    }
    extra = {f: t for f, t in targets.items() if f > 2}
    emit("fds", not extra, f"open fds {sorted(targets)}" + (f" extra: {extra}" if extra else ""))


def sleep_mode() -> int:
    if os.fork() == 0:  # a child that ignores SIGTERM: the jail must still end it
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        time.sleep(600)
        os._exit(0)
    print("SLEEPING", flush=True)
    time.sleep(600)
    return 0


def _lock(path: str) -> None:
    """A dir holding a file, then the dir's permissions stripped: what a hostile CLI can do to
    anything it owns in the jail, to keep the host from cleaning it up."""
    os.makedirs(os.path.join(path, "inner"), exist_ok=True)
    with open(os.path.join(path, "inner", "f"), "w") as f:
        f.write("x")
    os.chmod(os.path.join(path, "inner"), 0)
    os.chmod(path, 0)


def _deep_lock(top: str, depth: int = 3000) -> None:
    """A chain of `depth` dirs (past PATH_MAX, and deeper than a recursive remover goes) whose
    bottom holds a file and is chmod 0. Built by relative names, as only a relative walk can."""
    os.mkdir(top)
    fd = os.open(top, os.O_RDONLY)
    for _ in range(depth):
        os.mkdir("d", dir_fd=fd)
        nfd = os.open("d", os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = nfd
    os.close(os.open("f", os.O_CREAT | os.O_WRONLY, 0o600, dir_fd=fd))
    os.fchmod(fd, 0)
    os.close(fd)


def hostile_perms_mode(conf: dict) -> int:
    """Strip permissions in the home (the login's own dir, a new dir, the home itself) and in
    /work, and leave a 3000-deep chain with a locked bottom in each, then exit 0. The host must
    still remove every copy of the login, and everything else."""
    home = os.environ["HOME"]
    login_dir = os.path.join(home, conf["expected_home_files"][0].split("/")[0])
    _deep_lock(os.path.join(home, "deep"))
    _deep_lock(os.path.join("/work", "deep"))
    _lock(os.path.join(home, "locked"))
    _lock(os.path.join("/work", "locked"))
    os.chmod(login_dir, 0)
    os.chmod("/work", 0)
    os.chmod(home, 0)
    print("HOSTILE-DONE", flush=True)
    return 0


def hostile_plants_mode(conf: dict) -> int:
    """Replace the staged login files with things the host's post-exit check must not open: a
    FIFO (a read blocks forever), a symlink to /dev/zero (a read never ends), and symlinks to
    /proc/self/environ (a host file once the path resolves on the host). Then exit 0."""
    home = os.environ["HOME"]
    for i, rel in enumerate(conf["expected_home_files"]):
        path = os.path.join(home, rel)
        os.unlink(path)
        if i == 0:
            os.mkfifo(path)
        elif i == 1:
            os.symlink("/dev/zero", path)
        else:
            os.symlink("/proc/self/environ", path)
    print("HOSTILE-DONE", flush=True)
    return 0


def hostile_huge_mode(conf: dict) -> int:
    """Replace the first login file with a 64 GiB sparse regular file (no disk used): hashing it
    would take minutes and defer a SIGTERM past the harness's grace. Then exit 0."""
    path = os.path.join(os.environ["HOME"], conf["expected_home_files"][0])
    os.unlink(path)
    with open(path, "wb") as f:
        f.truncate(64 << 30)
    print("HOSTILE-DONE", flush=True)
    return 0


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as f:
        conf = json.load(f)
    if conf.get("mode") == "sleep":
        return sleep_mode()
    if conf.get("mode") == "hostile-perms":
        return hostile_perms_mode(conf)
    if conf.get("mode") == "hostile-plants":
        return hostile_plants_mode(conf)
    if conf.get("mode") == "hostile-huge":
        return hostile_huge_mode(conf)
    check_paths(conf)
    check_cli_binaries()
    check_etc_contents(conf)
    check_strings(conf)
    check_env(conf)
    check_home(conf)
    check_processes(conf)
    check_fd_targets(conf)
    check_isolation()
    check_mcp(conf)
    check_unreachable(conf)
    check_dns(conf)
    check_tls(conf)
    print(json.dumps({"id": "done", "ok": True, "detail": ""}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
