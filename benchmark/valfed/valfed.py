"""The project's own validation federation (design/10-tool-maturity.md, "Test environment").

    make valfed-up                bring it up (or check one that is up), cap it, mint the tokens
    make valfed-reset             empty every space; ids, users and tokens stay
    make valfed-down              remove it, its data and its tokens
    make valfed-status            containers, CPU and memory caps, peak memory, host load
    make valfed-check             no-model check of the panel's federation layer, in omcp-smoke
    make valfed-readiness-live    the gated live readiness test, in omcp-live
    make valfed-qos-live          the gated live test of remove_qos_requirement, in omcp-live

One Onezone and two Oneproviders of Onedata 25.0 in Docker Compose, published on 127.0.0.1 only,
as `layout.toml` beside this file declares them. The work is done by the Compose federation tool
of papers-experiments-core (`deploy/compose-federation`, see its README), fetched at a pinned
commit. This driver adds what the tool leaves to its user:

- a CPU cap per container (`CPUS`) with `docker update`, re-applied while `up` and `reset` run
  and after them, since a re-created container loses it;
- a guard: nothing starts while the host's 5-minute load is above `LOAD_LIMIT`, or when free
  memory would not hold the layout's limits with room to spare, and a start is stopped and the
  stack taken down if the load passes the limit meanwhile;
- two access tokens of the layout's one user, made with the tool's admin token through Onezone's
  REST API: `tools` (the MCP server's, at `local` and Onezone) and `writer` (the panel's fixtures,
  at `owner`);
- `benchmark/k2val/k2val.valfed.toml` (gitignored), the panel's config for this federation.

Environment:

- `VALFED_HOME` (required): an existing absolute directory with room. Everything goes below it:
  `core/` (the tool), `ca/` (the federation's CA, 0700, valid 30 days and kept across down/up),
  `fed/` (the federation's root; down removes it), `tokens/` (0600 token files) and
  `tls/ca-bundle.pem` (public roots plus the federation's CA).
- `VALFED_CORE_REPO` (optional): a local git clone of the core to `git archive` the pinned
  commit from; without it the commit's tarball is downloaded from GitHub.
- `VALFED_KEY_DIR` (optional): the directory of the panel's model key files, written into the
  generated config; without it the config keeps the example's `<key dir>` placeholder.

No token value is printed; token files are named, never read out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import signal
import ssl
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
LAYOUT = HERE / "layout.toml"
PANEL_EXAMPLE = REPO / "benchmark/k2val/k2val.toml.example"
PANEL_CONFIG = REPO / "benchmark/k2val/k2val.valfed.toml"

CORE_URL = "https://github.com/groundnuty/papers-experiments-core"
CORE_COMMIT = "c8c8a9f8e6aeb90829081c77dc6943ad81454862"
CORE_MARK = ".valfed-core-commit"
FEDERATION = "deploy/compose-federation/federation.py"

CPUS = {"zone": 1.0, "local": 1.5, "owner": 1.5}  # per Compose service; 4 in all
LOAD_LIMIT = 20.0  # the host's 5-minute load above which nothing starts and a start stops
MEMORY_HEADROOM = 2 << 30  # free memory beyond the layout's limits before a first start
WATCH_S = 5.0

USER = "omcp-tools"
LIVE_SPACE = "omcp-live"
SMOKE_SPACE = "omcp-smoke"
TOKENS = {"tools": "local", "writer": "owner"}  # token file stem -> the provider it works at
# Validation-panel config (k2val.toml.example) -> its space in layout.toml.
PANEL = {
    "sonnet-medium": "omcp-sonnet",
    "haiku-low": "omcp-haiku-low",
    "glm": "omcp-glm",
    "qwen36": "omcp-qwen36",
    "qwencoder": "omcp-qwencoder",
    "deepseek": "omcp-deepseek",
}


class ValfedError(RuntimeError):
    """A refusal or a failure; the message names files and settings, never a secret."""


class LoadStop(ValfedError):
    """The host's load passed LOAD_LIMIT while the stack started; it was taken down."""


# --- places --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Home:
    root: Path

    @classmethod
    def from_environment(cls) -> Home:
        value = os.environ.get("VALFED_HOME", "")
        if not value or not os.path.isabs(value) or not os.path.isdir(value):
            raise ValfedError("set VALFED_HOME to an existing absolute directory (see valfed.py)")
        return cls(Path(os.path.normpath(value)))

    core = property(lambda self: self.root / "core")
    ca = property(lambda self: self.root / "ca")  # the core's EXPCORE_FED_CA_DIR
    fed = property(lambda self: self.root / "fed")  # the core's EXPCORE_FED_ROOT
    tokens = property(lambda self: self.root / "tokens")
    bundle = property(lambda self: self.root / "tls" / "ca-bundle.pem")
    ca_pem = property(lambda self: self.ca / "federation-ca.pem")
    facts_file = property(lambda self: self.fed / "facts.json")

    def token_file(self, name: str) -> Path:
        return self.tokens / f"{name}.token"


def layout() -> dict[str, Any]:
    return tomllib.loads(LAYOUT.read_text(encoding="utf-8"))


def memory_bytes(limit: str) -> int:
    number, unit = re.fullmatch(r"([1-9][0-9]*)([mg])", limit).groups()
    return int(number) << (30 if unit == "g" else 20)


def layout_memory(spec: dict[str, Any]) -> int:
    default = spec.get("memory_limit", "6g")
    services = [spec["zone"], *spec["providers"].values()]
    return sum(memory_bytes(s.get("memory_limit", default)) for s in services)


# --- the core ------------------------------------------------------------------------------


def _extract(stream: Any, dest: Path, strip: int) -> None:
    with tarfile.open(fileobj=stream, mode="r|*") as tar:
        for member in tar:
            parts = Path(member.name).parts[strip:]
            if not parts:
                continue
            member.name = str(Path(*parts))
            tar.extract(member, dest, filter="data")


def fetch_core(home: Home) -> None:
    """The core at CORE_COMMIT in `core/`, with its own environment. A `core/` without the mark is
    not this driver's and is refused, not replaced."""
    mark = home.core / CORE_MARK
    if mark.is_file() and mark.read_text().strip() == CORE_COMMIT:
        return
    if home.core.exists() and not mark.is_file():
        raise ValfedError(f"{home.core} exists without {CORE_MARK}: not made here; remove it")
    staging = Path(tempfile.mkdtemp(prefix="core.", dir=home.root))
    try:
        repo = os.environ.get("VALFED_CORE_REPO", "")
        if repo:
            print(f"core: git archive {CORE_COMMIT[:12]} from VALFED_CORE_REPO")
            cmd = ["git", "-C", repo, "archive", "--format=tar", CORE_COMMIT]
            with subprocess.Popen(cmd, stdout=subprocess.PIPE) as proc:
                _extract(proc.stdout, staging, strip=0)
            if proc.returncode:
                raise ValfedError(f"git archive {CORE_COMMIT} failed in VALFED_CORE_REPO")
        else:
            print(f"core: downloading {CORE_COMMIT[:12]} from {CORE_URL}")
            with urllib.request.urlopen(f"{CORE_URL}/archive/{CORE_COMMIT}.tar.gz") as answer:
                _extract(answer, staging, strip=1)
        if home.core.exists():
            shutil.rmtree(home.core)  # marked, at another commit: this driver's own
        staging.rename(home.core)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    sync = ["uv", "sync", "--frozen", "--quiet"]
    subprocess.run(sync, cwd=home.core, env=core_env(home), check=True)
    mark.write_text(CORE_COMMIT + "\n")


def core_env(home: Home) -> dict[str, str]:
    """This process's environment for the core: no inherited EXPCORE_* setting (another
    federation's root, CA or import must never leak in), no virtualenv of ours, and our three."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("EXPCORE_")}
    env.pop("VIRTUAL_ENV", None)
    env.update(
        EXPCORE_FED_ROOT_PARENT=str(home.root),
        EXPCORE_FED_ROOT=str(home.fed),
        EXPCORE_FED_CA_DIR=str(home.ca),
    )
    return env


def core_cmd(home: Home, *args: str) -> list[str]:
    run = ["uv", "run", "--frozen", "--quiet", "python", FEDERATION]
    return [*run, "--layout", str(LAYOUT), *args]


def federation(home: Home, *args: str) -> None:
    result = subprocess.run(core_cmd(home, *args), cwd=home.core, env=core_env(home))
    if result.returncode:
        raise ValfedError(f"federation.py {' '.join(args)} failed (exit {result.returncode})")


def federation_watched(home: Home, *args: str) -> None:
    """Run a command that starts services, capping containers as they appear, and stop it and
    take the stack down if the host's load passes LOAD_LIMIT meanwhile."""
    proc = subprocess.Popen(
        core_cmd(home, *args), cwd=home.core, env=core_env(home), start_new_session=True
    )
    try:
        while proc.poll() is None:
            apply_caps(quiet=True)
            if load5() > LOAD_LIMIT:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=120)
                take_down_for_load(home)
            time.sleep(WATCH_S)
    except BaseException:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
        raise
    if proc.returncode:
        raise ValfedError(f"federation.py {' '.join(args)} failed (exit {proc.returncode})")


def take_down_for_load(home: Home) -> None:
    print(f"valfed: 5-minute load {load5():.1f} > {LOAD_LIMIT}: taking the stack down")
    federation(home, "down")
    raise LoadStop("stopped: the host's load passed the limit; the stack is down")


def wait_healthy(home: Home, timeout_s: float = 900.0) -> bool:
    """Whether every service's container reports healthy within the bound: its image's own
    marker that the service's deployment finished. False at once if one is not running."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        states = [inspect(cid)["State"] for cid, service in containers() if service in CPUS]
        if len(states) != len(CPUS) or any(s["Status"] != "running" for s in states):
            return False
        if all(s.get("Health", {}).get("Status") == "healthy" for s in states):
            return True
        apply_caps(quiet=True)
        if load5() > LOAD_LIMIT:
            take_down_for_load(home)
        time.sleep(WATCH_S)
    return False


# --- the host and the containers -----------------------------------------------------------


def load5() -> float:
    return float(Path("/proc/loadavg").read_text().split()[1])


def mem_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) << 10
    raise ValfedError("MemAvailable is not in /proc/meminfo")


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def containers() -> list[tuple[str, str]]:
    """(id, Compose service) of this project's containers, by its label: never by name."""
    label = f"label=com.docker.compose.project={layout()['project']}"
    fields = '{{.ID}} {{.Label "com.docker.compose.service"}}'
    out = docker("ps", "-a", "--filter", label, "--format", fields)
    return [tuple(line.split(maxsplit=1)) for line in out.splitlines() if line.strip()]


def inspect(cid: str) -> dict[str, Any]:
    return json.loads(docker("inspect", cid))[0]


def apply_caps(*, quiet: bool = False) -> None:
    for cid, service in containers():
        want = CPUS.get(service)
        if want is None:
            continue  # the tool's short-lived janitor
        if inspect(cid)["HostConfig"]["NanoCpus"] != int(want * 1e9):
            docker("update", "--cpus", str(want), cid)
            if not quiet:
                print(f"capped {service} at {want} CPUs")


def guard(*, fresh: bool) -> None:
    load = load5()
    if load > LOAD_LIMIT:
        raise ValfedError(f"refused: the host's 5-minute load is {load:.1f} (limit {LOAD_LIMIT})")
    if fresh:
        need, have = layout_memory(layout()) + MEMORY_HEADROOM, mem_available()
        if have < need:
            raise ValfedError(f"refused: {have >> 20} MiB available, {need >> 20} MiB needed")


def caps_table() -> list[str]:
    rows = []
    for cid, service in containers():
        info = inspect(cid)
        host, state = info["HostConfig"], info["State"]
        rows.append(
            f"{service:6} {state['Status']:8} cpus={host['NanoCpus'] / 1e9:g} "
            f"mem_limit={host['Memory'] / 2**30:.2f}GiB oom_killed={state['OOMKilled']}"
        )
    return rows


def require_caps() -> None:
    found = {service: inspect(cid) for cid, service in containers() if service in CPUS}
    if set(found) != set(CPUS):
        raise ValfedError(f"expected containers {sorted(CPUS)}, found {sorted(found)}")
    for service, info in found.items():
        if info["HostConfig"]["NanoCpus"] != int(CPUS[service] * 1e9):
            raise ValfedError(f"{service}: CPU cap not applied")
        if not info["HostConfig"]["Memory"]:
            raise ValfedError(f"{service}: no memory limit")


# --- Onezone, tokens and the panel's config ------------------------------------------------


def facts(home: Home) -> dict[str, Any]:
    if not home.facts_file.is_file():
        raise ValfedError("the federation is not up (no facts.json); run make valfed-up")
    return json.loads(home.facts_file.read_text(encoding="utf-8"))


def call(home: Home, url: str, token: str, method: str = "GET", body: Any = None) -> Any:
    """One REST call, verified against the federation's CA. Errors name the URL, not the token."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"X-Auth-Token": token, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    context = ssl.create_default_context(cafile=str(home.ca_pem))
    with urllib.request.urlopen(request, context=context, timeout=60) as answer:
        raw = answer.read()
    return json.loads(raw) if raw else None


def _authenticates(home: Home, zone: str, token: str, uid: str) -> bool:
    try:
        return call(home, f"{zone}/api/v3/onezone/user", token)["userId"] == uid
    except urllib.error.HTTPError as error:
        if error.code in (400, 401, 403):
            return False
        raise


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    descriptor, draft = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(descriptor, "w") as handle:  # mkstemp makes it 0600
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(draft, path)


def ensure_tokens(home: Home, found: dict[str, Any]) -> None:
    """Keep each token file whose token still signs in as the user; mint the others as named
    access tokens of the user, with the admin token. Then check each where it is used."""
    zone, uid = found["zone"]["url"], found["principals"][USER]["user_id"]
    admin_file = home.fed / found["credentials"]["admin_access_token_file"]
    admin = admin_file.read_text(encoding="utf-8").strip()
    for name in TOKENS:
        path = home.token_file(name)
        if path.is_file() and _authenticates(home, zone, path.read_text().strip(), uid):
            print(f"token {name}: kept ({path})")
            continue
        body = {"name": f"omcp-valfed-{name}-{secrets.token_hex(4)}", "type": {"accessToken": {}}}
        made = call(home, f"{zone}/api/v3/onezone/users/{uid}/tokens/named", admin, "POST", body)
        _write_secret(path, made["token"])
        print(f"token {name}: minted ({path})")
    for name, provider in TOKENS.items():
        url = found["providers"][provider]["url"]
        _check_at_provider(home, name, provider, f"{url}/api/v3/oneprovider/spaces")
        where = provider + (" and Onezone" if name == "tools" else "")
        print(f"token {name}: works at {where}; sees all {len(layout()['spaces'])} spaces")


def _check_at_provider(home: Home, name: str, provider: str, url: str, bound_s: float = 180):
    """The token lists every declared space at the provider. Right after up a provider answered a
    token minted a moment before with 401 for a while, so a refusal is tried again until the
    bound."""
    token, wanted = home.token_file(name).read_text().strip(), set(layout()["spaces"])
    end, missing = time.monotonic() + bound_s, "no answer"
    while time.monotonic() < end:
        try:
            listed = {s["name"] for s in call(home, url, token)}
            missing = sorted(wanted - listed)
            if not missing:
                return
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise
            missing = f"HTTP {error.code}"
        time.sleep(WATCH_S)
    raise ValfedError(f"token {name} at {provider} after {bound_s:.0f} s: {missing}")


def write_bundle(home: Home) -> None:
    """Public roots plus the federation's CA: an SSL_CERT_FILE that verifies this federation
    without breaking TLS to the model APIs."""
    import certifi

    home.bundle.parent.mkdir(exist_ok=True)
    text = Path(certifi.where()).read_text() + "\n" + home.ca_pem.read_text()
    home.bundle.write_text(text)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"no TOML form for {type(value).__name__}")


def _toml_tables(table: dict[str, Any], prefix: str = "") -> list[str]:
    lines = [f"{k} = {_toml_value(v)}" for k, v in table.items() if not isinstance(v, dict)]
    for key, sub in ((k, v) for k, v in table.items() if isinstance(v, dict)):
        name = f"{prefix}{key}"
        lines += ["", f"[{name}]", *_toml_tables(sub, f"{name}.")]
    return lines


def panel_config(
    found: dict[str, Any], names: dict[str, str], home: Home, key_dir: str = ""
) -> str:
    """The validation panel's config for this federation, from the committed example: its [run],
    [spend] and the PANEL configs, each in its own space here. `names` are the providers' names
    as Onezone lists them. Paths only, no secret."""
    example = tomllib.loads(PANEL_EXAMPLE.read_text(encoding="utf-8"))
    providers = found["providers"]

    def provider(role: str, token: str) -> dict[str, str]:
        url = providers[role]["url"]
        return {"name": names[role], "host": url, "token_file": str(home.token_file(token))}

    models = {}
    for config, space in PANEL.items():
        model = dict(example["models"][config], space=space)
        if key_dir and "key_file" in model:
            model["key_file"] = model["key_file"].replace("<key dir>", key_dir)
        models[config] = model
    doc = {
        "federation": {
            "allow_insecure_tls": False,
            "onezone": {"host": found["zone"]["url"], "token_file": str(home.token_file("tools"))},
            "local": provider("local", "tools"),
            "owner": provider("owner", "writer"),
        },
        "run": example["run"],
        "spend": example["spend"],
        "models": models,
    }
    header = [
        "# Written by `make valfed-up` (benchmark/valfed/valfed.py): the validation panel against",
        "# this project's own Compose federation. Gitignored; paths only, never a secret.",
        "# TLS is verified: run the panel with the federation's CA in the bundle,",
        f"#   SSL_CERT_FILE={home.bundle} \\",
        f"#     make k2val K2VAL_CONFIG={PANEL_CONFIG.relative_to(REPO)}",
    ]
    return "\n".join([*header, *_toml_tables(doc)]) + "\n"


def write_panel_config(home: Home, found: dict[str, Any]) -> None:
    admin = (home.fed / found["credentials"]["admin_access_token_file"]).read_text().strip()
    zone = found["zone"]["url"]
    names = {
        role: call(home, f"{zone}/api/v3/onezone/providers/{p['id']}", admin)["name"]
        for role, p in found["providers"].items()
    }
    text = panel_config(found, names, home, os.environ.get("VALFED_KEY_DIR", ""))
    from benchmark.k2val.config import parse_config  # its host guard and checks, before writing

    parse_config(tomllib.loads(text))
    PANEL_CONFIG.write_text(text)
    print(f"panel config: {PANEL_CONFIG.relative_to(REPO)} (TLS bundle {home.bundle})")


# --- the commands --------------------------------------------------------------------------


def up(home: Home) -> None:
    fresh = not containers()
    guard(fresh=fresh)
    fetch_core(home)
    federation(home, "ca")
    try:
        federation_watched(home, "up")
    except LoadStop:
        raise
    except ValfedError as error:
        # The tool's up can ask for a provider's storage before the image has finished deploying
        # it (seen under the CPU caps). Its up converges when run again, so after a failure it
        # runs once more when every service reports its deployment done; a second failure stands.
        if not wait_healthy(home):
            raise
        print(f"valfed: {error}; every service is healthy now, so up runs once more")
        federation_watched(home, "up")
    finish(home)


def reset(home: Home) -> None:
    guard(fresh=False)
    federation_watched(home, "reset", "--clean")
    finish(home)


def finish(home: Home) -> None:
    apply_caps()
    require_caps()
    found = facts(home)
    ensure_tokens(home, found)
    write_bundle(home)
    write_panel_config(home, found)
    status(home)


def down(home: Home) -> None:
    if home.core.joinpath(CORE_MARK).is_file():
        federation(home, "down")
    for name in TOKENS:
        home.token_file(name).unlink(missing_ok=True)
    PANEL_CONFIG.unlink(missing_ok=True)
    print("tokens and panel config removed")


def status(home: Home) -> None:
    if home.core.joinpath(CORE_MARK).is_file():
        federation(home, "status")
        if containers():
            federation(home, "memory")
    print(*(caps_table() or ["no container of the project"]), sep="\n")
    if home.ca_pem.is_file():
        cmd = ["openssl", "x509", "-enddate", "-noout", "-in", str(home.ca_pem)]
        end = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
        print(f"CA {home.ca_pem}: {end}")
    print(f"host 5-minute load: {load5():.2f} (limit {LOAD_LIMIT})")


# The gated live tests: name -> (test file, the variable that enables it, the one naming its
# space). Each runs in LIVE_SPACE.
LIVE_TESTS = {
    "readiness": (
        "test/live/test_readiness_live.py",
        "ONEDATA_MCP_LIVE_READINESS",
        "ONEDATA_MCP_LIVE_READINESS_SPACE",
    ),
    "qos": ("test/live/test_qos_live.py", "ONEDATA_MCP_LIVE_QOS", "ONEDATA_MCP_LIVE_QOS_SPACE"),
}


def live_test(home: Home, name: str) -> None:
    """A gated live test against omcp-live, TLS verified, the MCP server's token from its file
    (passed in the environment, never on a command line)."""
    test, enable, space = LIVE_TESTS[name]
    found = facts(home)
    tools = home.token_file("tools").read_text().strip()
    env = dict(
        os.environ,
        ONEDATA_MCP_DOTENV="0",
        ONEDATA_ALLOW_INSECURE_TLS="false",
        SSL_CERT_FILE=str(home.ca_pem),
        ONEDATA_ONEZONE_HOST=found["zone"]["url"],
        ONEDATA_ONEZONE_TOKEN=tools,
        ONEDATA_ONEPROVIDER_HOST=found["providers"]["local"]["url"],
        ONEDATA_ONEPROVIDER_TOKEN=tools,
        **{enable: "1", space: LIVE_SPACE},
    )
    rc = subprocess.run([sys.executable, "-m", "pytest", test, "-v", "-s"], cwd=REPO, env=env)
    if rc.returncode:
        raise ValfedError(f"the live {name} test failed (exit {rc.returncode})")


def readiness_live(home: Home) -> None:
    """test/live/test_readiness_live.py against omcp-live."""
    live_test(home, "readiness")


def qos_live(home: Home) -> None:
    """test/live/test_qos_live.py (remove_qos_requirement) against omcp-live."""
    live_test(home, "qos")


async def _fixture_check() -> None:
    from benchmark.k2val.clock import REAL_CLOCK
    from benchmark.k2val.config import load_config, read_federation_secrets
    from benchmark.k2val.federation import FedApi
    from benchmark.k2val.fixtures import FileSpec, FixtureBuilder, FixtureSpec
    from benchmark.k2val.scenarios import SCENARIOS

    cfg = load_config(PANEL_CONFIG)
    fed = FedApi.from_conf(cfg.federation, read_federation_secrets(cfg.federation))
    builder = FixtureBuilder(fed, SMOKE_SPACE, REAL_CLOCK)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    # V4's fixture, then the kinds it lacks: a transfer (V1), a QoS rule (V3), a file being written.
    cases = {
        "V4": SCENARIOS["V4"].fixture,
        "V1": SCENARIOS["V1"].fixture,
        "V3": SCENARIOS["V3"].fixture,
        "s2": FixtureSpec((FileSpec("20200104", "s2"),), writer_finish_s=30.0),
    }
    problems: list[str] = []
    try:
        for label, spec in cases.items():
            began = time.monotonic()
            fixture = await builder.build(spec, f"logs-valfed-{label.lower()}-{stamp}")
            kinds = ",".join(sorted({f.kind for f in spec.files}))
            took = time.monotonic() - began
            line = f"{label}: built {len(fixture.files)} files ({kinds}) in {took:.1f} s"
            if fixture.baseline_tree is not None:
                line += f", {len(fixture.baseline_tree)} entries seen at the local provider"
            if fixture.writer is not None:
                fixture.writer.start()
                await fixture.writer.wait()
                if not fixture.writer.finished:
                    problems.append(f"{label}: writer {fixture.writer.error}")
                line += f"; writer finished={fixture.writer.finished}"
            left = await builder.teardown(fixture)
            problems += [f"{label} teardown: {p}" for p in left]
            print(f"{line}; torn down ({len(left)} problems)")
    finally:
        problems += [f"sweep: {p}" for p in await builder.sweep_space()]
    root = await fed.lookup("owner", f"/{SMOKE_SPACE}")
    remaining = [k.get("name") for k in await fed.children("owner", root)]
    print(f"{SMOKE_SPACE} after the check: {len(remaining)} entries")
    if problems or remaining:
        raise ValfedError(f"fixture check: problems {problems}, left {remaining}")
    print("fixture check: passed")


def check(home: Home) -> None:
    """No model: build and tear down the panel's fixtures in omcp-smoke through its own
    FixtureBuilder, TLS verified against the bundle."""
    if not PANEL_CONFIG.is_file():
        raise ValfedError("no panel config; run make valfed-up")
    os.environ["SSL_CERT_FILE"] = str(home.bundle)
    asyncio.run(_fixture_check())


COMMANDS = {
    "up": up,
    "reset": reset,
    "down": down,
    "status": status,
    "check": check,
    "readiness-live": readiness_live,
    "qos-live": qos_live,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=sorted(COMMANDS))
    args = parser.parse_args(argv)
    try:
        COMMANDS[args.command](Home.from_environment())
    except (ValfedError, subprocess.CalledProcessError, OSError) as error:
        print(f"valfed {args.command}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
