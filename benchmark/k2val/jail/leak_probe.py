"""The leak probe's OUTSIDE half: runs the jail with a probe in place of the CLI, checks the host
side, and prints a PASS/FAIL table. Entry point: leak-probe.sh.

    leak-probe.sh [--cli codex] [--cli agy] [--config benchmark/k2val/k2val.toml]

Per CLI, with the same jail arguments the harness uses (--cli, --workdir, --mcp-upstream):

1. host paths (the home, /mnt, /Users, ~/.ssh, the repo, the config, token and key files) are
   absent, and no host-identifying string shows in the jail's /proc or /etc;
2. the environment holds exactly the allowed variables;
3. the fresh home holds exactly that CLI's login files;
4. the host's loopback (127.0.0.1:31443/31444, 10.0.2.2, a canary listener started here) and
   the host's other addresses (a 0.0.0.0 canary via every host IPv4 address, the metadata
   service) are unreachable, while each target IS reachable from the host at that moment;
5. one TLS handshake each to api.openai.com, chatgpt.com, generativelanguage.googleapis.com;
6. 127.0.0.1:<P> inside the jail reaches a dummy HTTP server run here as the upstream;
7. host processes are not visible; and, beyond the brief: no capabilities, no user namespaces,
   the nft filter can be neither read nor flushed, no terminal, no stray file descriptors;
8. no staging dir and no process is left after a normal exit, a SIGTERM and a SIGKILL.

Exit 0 only if every row passes. Prints no credential and no host path.
"""

from __future__ import annotations

import argparse
import contextlib
import http.server
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
JAIL = HERE / "cli-jail.sh"
NEUTRAL_ROOT = "/tmp"
TLS_HOSTS = ["api.openai.com", "chatgpt.com", "generativelanguage.googleapis.com"]
HOSTILE_TIMEOUT_S = 60  # a hostile run that has not ended by then is a hang (a FAIL)
METADATA = ("169.254.169.254", 80)

# The login files cli-jail.sh copies (the first of each list is required, the rest if present).
LOGIN_FILES = {
    "codex": [".codex/auth.json", ".codex/installation_id"],
    "agy": [
        ".gemini/antigravity-cli/antigravity-oauth-token",
        ".gemini/antigravity-cli/installation_id",
        ".gemini/google_accounts.json",
        ".gemini/installation_id",
        ".gemini/settings.json",
    ],
}


def expected_home(cli: str, home: str) -> list[str]:
    return sorted(f for f in LOGIN_FILES[cli] if os.path.isfile(os.path.join(home, f)))


ALLOWED_ENV = {
    "codex": ["CODEX_HOME", "HOME", "LANG", "PATH"],
    "agy": ["HOME", "LANG", "PATH"],
}
EXPECTED_ETC = [
    "ca-certificates",
    "group",
    "hosts",
    "nsswitch.conf",
    "passwd",
    "resolv.conf",
    "ssl",
]


@dataclass
class Row:
    prop: str
    ok: bool
    detail: str


@dataclass
class Report:
    cli: str
    rows: list[Row] = field(default_factory=list)

    def add(self, prop: str, ok: bool, detail: str) -> None:
        self.rows.append(Row(prop, ok, detail))


# --- host-side helpers ------------------------------------------------------------------------


class Canary:
    """A TCP listener that counts connections: any connection during the jail run is a leak."""

    def __init__(self, host: str) -> None:
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (TimeoutError, OSError):
                continue
            self.hits += 1
            conn.close()

    def drain(self) -> None:
        """Let connections the host-side checks queued be accepted, then reset the count."""
        last, stable_since = -1, time.monotonic()
        while time.monotonic() - stable_since < 1.0:
            if self.hits != last:
                last, stable_since = self.hits, time.monotonic()
            time.sleep(0.1)
        self.hits = 0

    def close(self) -> None:
        self._stop = True
        self.sock.close()


class Upstream:
    """The dummy MCP upstream: answers every GET with a nonce."""

    def __init__(self, nonce: str) -> None:
        body = f"upstream {nonce}\n".encode()

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def host_ipv4_addresses() -> list[str]:
    ip = shutil.which("ip", path=os.environ.get("PATH", "") + ":/usr/sbin:/sbin")
    if not ip:
        return []
    out = subprocess.run([ip, "-j", "-4", "addr", "show"], capture_output=True, text=True).stdout
    return sorted(
        {
            a["local"]
            for row in json.loads(out or "[]")
            for a in row.get("addr_info", [])
            if a.get("family") == "inet" and a.get("local") and not a["local"].startswith("127.")
        }
    )


def repo_root() -> Path:
    try:
        out = subprocess.run(
            ["git", "-C", str(HERE), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(out)
    except (OSError, subprocess.CalledProcessError):
        return HERE.parents[2]


def secret_paths(config: Path | None) -> list[str]:
    """The token and key files named in the k2val config (paths only; never read)."""
    if not config or not config.is_file():
        return []
    with config.open("rb") as f:
        raw = tomllib.load(f)
    out = []
    fed = raw.get("federation", {})
    for key in ("onezone", "local", "owner"):
        if p := fed.get(key, {}).get("token_file"):
            out.append(p)
    for m in raw.get("models", {}).values():
        if p := m.get("key_file"):
            out.append(p)
    for s in raw.get("cli", {}).get("scan", {}).values():
        out.append(s)
    return [str(Path(p).expanduser()) for p in out if p]


def both(p: str | Path) -> list[str]:
    """A path and its resolved form (a symlinked home resolves elsewhere)."""
    p = str(p)
    return sorted({p, os.path.realpath(p)})


def jail_processes(markers: list[str]) -> list[int]:
    """Host pids whose command line holds a marker of this probe's jail."""
    out = []
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{p}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except OSError:
            continue
        if any(m in cmd for m in markers):
            out.append(int(p))
    return out


def force_rmtree(path: str) -> bool:
    """Remove a tree a jail wrote into, even with its permissions stripped: give every directory
    (never following a symlink) back u+rwx, top-down, then remove. True if it is gone."""

    def unlock(p: str) -> None:
        with contextlib.suppress(OSError):
            st = os.lstat(p)
            if not stat.S_ISDIR(st.st_mode):
                return
            os.chmod(p, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
            for entry in os.scandir(p):
                if entry.is_dir(follow_symlinks=False):
                    unlock(entry.path)

    if os.path.lexists(path):
        unlock(path)
        shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


SLIRP_SIGNATURE = "--exit-fd 7 --ready-fd 8"  # how cli-jail.sh starts slirp4netns


def jail_slirps() -> set[int]:
    """slirp4netns processes started the way cli-jail.sh starts them."""
    r = subprocess.run(["pgrep", "-x", "slirp4netns"], capture_output=True, text=True)
    out = set()
    for pid in r.stdout.split():
        with contextlib.suppress(OSError):
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            if SLIRP_SIGNATURE in cmd:
                out.add(int(pid))
    return out


@dataclass
class Leftovers:
    files: list[str]
    procs: list[int]
    slirps: set[int]

    @property
    def processes_gone(self) -> bool:
        return not self.procs and not self.slirps

    def describe(self) -> str:
        return (
            f"staging dirs left {len(self.files)}, jail processes left {len(self.procs)}, "
            f"slirp4netns left {len(self.slirps)}"
        )


# --- one CLI ------------------------------------------------------------------------------------


class CliProbe:
    def __init__(self, cli: str, config: Path | None) -> None:
        self.cli = cli
        self.config = config
        self.report = Report(cli)
        self.home = os.environ["HOME"]
        self.repo = repo_root()

    def probe_conf(self, upstream: Upstream, canary_lo: Canary, canary_any: Canary) -> dict:
        home = self.home
        forbidden_paths = sorted(
            {
                *both(home),
                "/mnt",
                "/Users",
                "/root",
                *both(f"{home}/.ssh"),
                *both(f"{home}/.codex"),
                *both(f"{home}/.gemini"),
                *both(self.repo),
                *(both(self.config) if self.config else []),
                *[q for p in secret_paths(self.config) for q in both(p)],
            }
        )
        user = os.environ.get("USER") or Path(home).name
        tmpdir = os.environ.get("TMPDIR", "")
        strings = {*both(home), *both(self.repo), "/mnt", "/Users", socket.gethostname()}
        if len(user) >= 3:
            strings.add(user)
        if tmpdir and os.path.realpath(tmpdir) != NEUTRAL_ROOT:
            strings.update(both(tmpdir))
        for binary in ("codex", "agy"):
            if b := shutil.which(binary):
                strings.add(os.path.dirname(os.path.realpath(b)))
        unreachable = [
            {"label": "host 127.0.0.1:31444", "host": "127.0.0.1", "port": 31444},
            {"label": "host 127.0.0.1:31443", "host": "127.0.0.1", "port": 31443},
            {"label": "jail lo canary port", "host": "127.0.0.1", "port": canary_lo.port},
            {"label": "10.0.2.2 canary port", "host": "10.0.2.2", "port": canary_lo.port},
            *[
                {"label": f"10.0.2.2:{p}", "host": "10.0.2.2", "port": p}
                for p in (22, 80, 443, 31443, 31444)
            ],
            {"label": "metadata 169.254.169.254:80", "host": METADATA[0], "port": METADATA[1]},
            *[
                {"label": f"host addr #{i} 0.0.0.0-canary", "host": a, "port": canary_any.port}
                for i, a in enumerate(host_ipv4_addresses())
            ],
            *[
                {"label": f"host addr #{i}:22", "host": a, "port": 22}
                for i, a in enumerate(host_ipv4_addresses())
            ],
        ]
        expected_root = ["bin", "dev", "etc", "home", "lib", "lib64", "opt", "proc", "run"]
        expected_root += ["sbin", "tmp", "usr", "work"]
        return {
            "mode": "probe",
            "cli": self.cli,
            "forbidden_paths": forbidden_paths,
            "forbidden_strings": sorted(s for s in strings if s),
            "host_hostname": socket.gethostname(),
            "host_marker": self.marker,
            "max_processes": 4,
            "allowed_env": ALLOWED_ENV[self.cli],
            "expected_home_files": expected_home(self.cli, self.home),
            "expected_root": expected_root,
            "expected_etc": EXPECTED_ETC,
            "unreachable": unreachable,
            "tls_hosts": TLS_HOSTS,
            "mcp": {"port": upstream.port, "nonce": self.nonce},
        }

    def host_reachability(self, conf: dict, canary_lo: Canary) -> dict[str, str]:
        """For each in-jail 'unreachable' target, what the HOST reaches right now."""
        out = {}
        for t in conf["unreachable"]:
            host, port = t["host"], t["port"]
            if host == "10.0.2.2":  # slirp's alias of the host's loopback
                host = "127.0.0.1"
            if t["label"] == "jail lo canary port":
                out[t["label"]] = "host canary listening" if reachable(host, port) else "DOWN"
                continue
            out[t["label"]] = "reachable from host" if reachable(host, port) else "not listening"
        return out

    def run_jail(self, conf: dict, root: str, workdir: str, upstream: Upstream | None):
        with tempfile.NamedTemporaryFile("w", suffix=".json", dir=root, delete=False) as f:
            json.dump(conf, f)
            probe = f.name
        cmd = [str(JAIL), "--probe", probe, "--cli", self.cli, "--workdir", workdir]
        if upstream:
            cmd += ["--mcp-upstream", f"127.0.0.1:{upstream.port}"]
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C.UTF-8",
            "HOME": self.home,
            "K2VAL_JAIL_TMPDIR": root,
        }
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            start_new_session=True,  # its own process group: a hang is killed whole (kill_group)
        )
        return proc, probe

    @staticmethod
    def kill_group(proc: subprocess.Popen) -> tuple[str, str]:
        """SIGKILL the jail script's whole process group (a hung child of it would otherwise keep
        its pipes open), then collect what it printed."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        return proc.communicate()

    def leftovers(self, root: str, probe: str, slirp_before: set[int]) -> Leftovers:
        deadline = time.monotonic() + 10
        while True:
            left = Leftovers(
                files=[n for n in os.listdir(root) if os.path.join(root, n) != probe],
                procs=jail_processes([root, "/opt/k2val/"]),
                slirps=jail_slirps() - slirp_before,
            )
            if left.processes_gone or time.monotonic() > deadline:
                return left
            time.sleep(0.3)

    def run(self) -> Report:
        rep = self.report
        self.nonce = uuid.uuid4().hex
        self.marker = f"k2val-probe-host-marker-{self.nonce}"
        # a host process with the marker in its command line: the jail must not see it
        host_proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)", self.marker]
        )
        upstream = Upstream(self.nonce)
        canary_lo, canary_any = Canary("127.0.0.1"), Canary("0.0.0.0")
        root = tempfile.mkdtemp(prefix="k2val-probe-root-", dir=NEUTRAL_ROOT)
        workdir = tempfile.mkdtemp(prefix="k2val-probe-work-", dir=NEUTRAL_ROOT)
        try:
            conf = self.probe_conf(upstream, canary_lo, canary_any)
            host_side = self.host_reachability(conf, canary_lo)
            canary_lo.drain()
            canary_any.drain()
            slirp_before = jail_slirps()

            proc, probe = self.run_jail(conf, root, workdir, upstream)
            try:
                out, err = proc.communicate(timeout=240)
            except subprocess.TimeoutExpired:
                out, err = self.kill_group(proc)
            results = {}
            for line in out.splitlines():
                with contextlib.suppress(ValueError):
                    r = json.loads(line)
                    results[r["id"]] = r
            if "done" not in results:
                tail = " | ".join(err.strip().splitlines()[-3:])
                rep.add("jail run", False, f"exit {proc.returncode}; probe incomplete: {tail}")
            self.rows_from(results, host_side, canary_lo, canary_any)
            left = self.leftovers(root, probe, slirp_before)
            rep.add(
                "8 leftovers after normal exit",
                proc.returncode == 0 and not left.files and left.processes_gone,
                f"exit {proc.returncode}; {left.describe()}",
            )
            os.unlink(probe)
            for sig in (signal.SIGTERM, signal.SIGKILL):
                self.signal_case(sig, conf, root, workdir, upstream)
            self.hostile_case(
                "hostile-perms",
                "8 login copies removed after chmod 000 (home, login dir, /work)",
                conf,
                root,
                upstream,
            )
            self.hostile_case(
                "hostile-plants",
                "8 no hang on a FIFO or /dev/zero planted at a login path",
                conf,
                root,
                upstream,
                expect_in_stderr=tuple(conf["expected_home_files"]),
            )
        finally:
            upstream.close()
            canary_lo.close()
            canary_any.close()
            host_proc.kill()
            host_proc.wait()
            force_rmtree(root)
            force_rmtree(workdir)
        return rep

    def hostile_case(
        self,
        mode: str,
        prop: str,
        conf: dict,
        root: str,
        upstream: Upstream,
        expect_in_stderr: tuple[str, ...] = (),
    ) -> None:
        """The probe plays a hostile CLI (`mode`), then exits 0. The jail script must still end
        in time and remove its staging dir; the work dir must still be removable by its owner;
        and the script's stderr must name each of `expect_in_stderr`."""
        workdir = tempfile.mkdtemp(prefix="k2val-probe-work-", dir=NEUTRAL_ROOT)
        slirp_before = jail_slirps()
        proc, probe = self.run_jail({**conf, "mode": mode}, root, workdir, upstream)
        t0 = time.monotonic()
        try:
            out, err = proc.communicate(timeout=HOSTILE_TIMEOUT_S)
            hung = False
        except subprocess.TimeoutExpired:
            out, err = self.kill_group(proc)
            hung = True
        elapsed = time.monotonic() - t0
        left = self.leftovers(root, probe, slirp_before)
        done = "HOSTILE-DONE" in out
        for n in left.files:  # whatever the script failed to remove (a FAIL below)
            force_rmtree(os.path.join(root, n))
        work_removed = force_rmtree(workdir)
        named = [s for s in expect_in_stderr if s in err]
        ok = done and not hung and proc.returncode == 0 and not left.files
        ok = ok and left.processes_gone and work_removed and len(named) == len(expect_in_stderr)
        detail = (
            f"exit {proc.returncode} after {elapsed:.1f}s{' (HUNG, killed)' if hung else ''}; "
            f"{left.describe()}; work dir removable: {work_removed}"
        )
        if expect_in_stderr:
            detail += f"; replaced logins named {len(named)}/{len(expect_in_stderr)}"
        if not done:
            detail = f"hostile probe did not finish: {' | '.join(err.splitlines()[-2:])}; " + detail
        self.report.add(prop, ok, detail)
        with contextlib.suppress(OSError):
            os.unlink(probe)
        self.last_hostile_stderr = err

    def signal_case(
        self, sig: signal.Signals, conf: dict, root: str, workdir: str, upstream: Upstream
    ) -> None:
        sleep_conf = {**conf, "mode": "sleep"}
        slirp_before = jail_slirps()
        proc, probe = self.run_jail(sleep_conf, root, workdir, upstream)
        started = proc.stdout.readline().strip() == "SLEEPING"
        running = jail_processes([root, "/opt/k2val/"])
        proc.send_signal(sig)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        left = self.leftovers(root, probe, slirp_before)
        name = signal.Signals(sig).name
        seen = f"{len(running)} jail processes while running; "
        if sig == signal.SIGKILL:
            # a SIGKILLed script cannot clean up: its staging dir stays in K2VAL_JAIL_TMPDIR,
            # which the caller removes (the harness removes its scratch dir; removed here)
            for n in left.files:
                shutil.rmtree(os.path.join(root, n), ignore_errors=True)
            ok = started and len(running) > 1 and left.processes_gone
            detail = seen + left.describe() + " (SIGKILL: the caller removes the staging dir)"
        else:
            ok = started and len(running) > 1 and not left.files and left.processes_gone
            detail = f"exit {proc.returncode}; " + seen + left.describe()
        self.report.add(f"8 leftovers after {name}", ok, detail if started else "never started")
        with contextlib.suppress(OSError):
            os.unlink(probe)

    def rows_from(self, results: dict, host_side: dict, lo: Canary, any_: Canary) -> None:
        rep = self.report

        def get(i: str) -> dict:
            return results.get(i, {"ok": False, "detail": "no result"})

        for i, prop in [
            ("paths", "1 host paths absent"),
            ("leak_strings", "1 no host string in /proc, /etc, hostname"),
            ("hostname", "1 own hostname"),
            ("root", "1 / holds only the jail's dirs"),
            ("etc", "1 /etc holds only generated + ssl files"),
            ("usr_local", "1 /usr/local hidden"),
            ("home_dir", "1 /home holds only the fresh home"),
            ("work_empty", "1 /work is empty"),
            ("env", "2 env = allowed vars only"),
            ("home", "3 fresh home = this CLI's login files only"),
        ]:
            r = get(i)
            rep.add(prop, r["ok"], r["detail"])
        for key in sorted(k for k in results if k.startswith("unreach:")):
            label = key.split(":", 1)[1]
            r = results[key]
            host = host_side.get(label, "?")
            vacuous = host == "DOWN"
            rep.add(f"4 unreachable: {label}", r["ok"] and not vacuous, f"{r['detail']} ({host})")
        rep.add(
            "4 canaries untouched during the run",
            lo.hits == 0 and any_.hits == 0,
            f"127.0.0.1 canary hits {lo.hits}, 0.0.0.0 canary hits {any_.hits}",
        )
        for h in TLS_HOSTS:
            r = get(f"tls:{h}")
            rep.add(f"5 outbound TLS {h}", r["ok"], r["detail"])
        r = get("mcp")
        rep.add("6 in-jail 127.0.0.1:P reaches the upstream", r["ok"], r["detail"])
        for i, prop in [
            ("processes", "7 host processes not visible"),
            ("caps", "7+ no capabilities"),
            ("no_userns", "7+ no new user namespaces"),
            ("nft_list", "7+ egress filter not readable"),
            ("nft_flush", "7+ egress filter not flushable"),
            ("no_tty", "7+ no controlling terminal"),
            ("fds", "7+ only fds 0,1,2"),
        ]:
            r = get(i)
            rep.add(prop, r["ok"], r["detail"])


def print_table(reports: list[Report]) -> bool:
    all_ok = True
    for rep in reports:
        width = max(len(r.prop) for r in rep.rows)
        print(f"\n== leak probe: {rep.cli} ==")
        print(f"{'property'.ljust(width)}  result  detail")
        for r in rep.rows:
            all_ok &= r.ok
            print(f"{r.prop.ljust(width)}  {'PASS' if r.ok else 'FAIL'}    {r.detail}")
        n_ok = sum(r.ok for r in rep.rows)
        print(f"-- {rep.cli}: {n_ok}/{len(rep.rows)} PASS")
    return all_ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="leak-probe.sh")
    p.add_argument("--cli", action="append", choices=["codex", "agy"])
    p.add_argument("--config", default="", help="k2val.toml: its token/key paths must be absent")
    a = p.parse_args(argv)
    config = Path(a.config).resolve() if a.config else None
    for tool in ("bwrap", "slirp4netns"):
        if not shutil.which(tool):
            print(f"leak-probe: {tool} not found", file=sys.stderr)
            return 2
    reports = [CliProbe(cli, config).run() for cli in a.cli or ["codex", "agy"]]
    return 0 if print_table(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
