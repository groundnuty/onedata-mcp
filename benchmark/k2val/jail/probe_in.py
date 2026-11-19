"""The leak probe's INSIDE half: runs in the CLI jail in place of the CLI (`cli-jail.sh --probe`).

    /usr/bin/python3 -I /opt/k2val/probe_in.py /opt/k2val/probe.json

It checks what a jailed CLI could see and reach, and prints one JSON line per check
({"id", "ok", "detail"}). The outside half (leak_probe.py) writes probe.json, runs the jail, adds
its host-side checks and prints the table. In "sleep" mode it only starts a child that ignores
SIGTERM and sleeps, for the cleanup-on-signal checks. Self-contained, standard library only.
"""

from __future__ import annotations

import errno
import http.client
import json
import os
import signal
import socket
import ssl
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


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as f:
        conf = json.load(f)
    if conf.get("mode") == "sleep":
        return sleep_mode()
    check_paths(conf)
    check_strings(conf)
    check_env(conf)
    check_home(conf)
    check_processes(conf)
    check_isolation()
    check_mcp(conf)
    check_unreachable(conf)
    check_tls(conf)
    print(json.dumps({"id": "done", "ok": True, "detail": ""}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
