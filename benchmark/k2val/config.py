"""Configuration of the validation panel: `k2val.toml` (see `k2val.toml.example`).

The file holds paths, never secrets. Tokens and keys are read from the files it names, when
they are needed (`read_federation_secrets`, `read_model_key`).

The host guard refuses any federation host that is not `127.0.0.1`, `localhost` or `*.datahub.test`:
the panel writes files and QoS rules and must never reach a real federation.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from benchmark.k2val.secrets import SecretStr, read_secret_file

LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
LOCAL_SUFFIX = ".datahub.test"

K1_TOOLS = (
    "add_file_qos_requirement",
    "get_file_attributes",
    "get_file_distribution",
    "get_file_metadata",
    "get_file_qos_summary",
    "get_qos_requirement",
    "get_transfer",
    "list_space_providers",
    "list_space_transfers",
    "remove_qos_requirement",
    "schedule_file_replication",
)
K2_TOOLS = (*K1_TOOLS, "check_readiness", "ensure_readiness")

BACKENDS = ("openai_compat", "anthropic", "claude_sdk", "codex", "agy")
CLI_BACKENDS = ("codex", "agy")
# `--effort` of the Claude Code CLI. The SDK's own type lists four of them (no "xhigh"), but it
# forwards the string unchanged.
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
CLAUDE_THINKING_MODES = ("adaptive", "disabled")
# Hard ceiling of the guard: a config may lower the concurrency per model, never raise it.
MAX_CONCURRENCY_PER_MODEL = 2


class ConfigError(ValueError):
    """The configuration is wrong. The message names the key, never a secret."""


class HostGuardError(ConfigError):
    """A federation host is not local: the panel refuses to run."""


def hostname_of(url: str) -> str | None:
    """The host name of an http(s) URL, lower-cased, without a trailing dot; None if not one."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    return parsed.hostname.lower().rstrip(".")


def is_local_host(host: str | None) -> bool:
    return bool(host) and (host in LOCAL_HOSTS or host.endswith(LOCAL_SUFFIX))


def check_hosts(named: dict[str, str]) -> None:
    """Raise HostGuardError unless every URL's host is local. `named` maps a label to a URL."""
    for label, url in named.items():
        host = hostname_of(url)
        if not is_local_host(host):
            raise HostGuardError(
                f"refusing to run: {label} must be an http(s) URL on 127.0.0.1, localhost or "
                f"*{LOCAL_SUFFIX} (no credentials in the URL); its host is {host!r}"
            )


@dataclass(frozen=True)
class ProviderConf:
    name: str
    host: str
    token_file: str


@dataclass(frozen=True)
class FederationConf:
    onezone_host: str
    onezone_token_file: str  # empty: the local provider's token is used at the zone too
    local: ProviderConf
    owner: ProviderConf
    allow_insecure_tls: bool = True

    def hosts(self) -> dict[str, str]:
        return {
            "federation.onezone_host": self.onezone_host,
            "federation.local.host": self.local.host,
            "federation.owner.host": self.owner.host,
        }


@dataclass(frozen=True)
class RunLimits:
    max_tool_rounds: int = 40
    wall_cap_s: float = 1200.0
    tool_call_cap_s: float = 300.0
    # After the final answer the federation may still be settling; the federation axis is
    # read once more after this long (the writer keeps running meanwhile).
    settle_s: float = 60.0
    concurrency_per_model: int = MAX_CONCURRENCY_PER_MODEL


@dataclass(frozen=True)
class SpendConf:
    """Spend of the `anthropic` backend (an API key billed per token). The cap is a config value.
    The `claude_sdk` backend runs on the subscription and is not counted."""

    cap_usd: float = 50.0
    reserve_usd: float = 0.50


@dataclass(frozen=True)
class PriceTiers:
    """USD per million tokens. The rate card is chosen by the size of the call's prompt."""

    input_low: float
    output_low: float
    input_high: float
    output_high: float
    threshold_tokens: int = 200_000

    def cost(self, prompt_tokens: int, output_tokens: int) -> float:
        high = prompt_tokens > self.threshold_tokens
        rate_in = self.input_high if high else self.input_low
        rate_out = self.output_high if high else self.output_low
        return (prompt_tokens * rate_in + output_tokens * rate_out) / 1_000_000


@dataclass(frozen=True)
class ModelConf:
    name: str
    backend: str
    api_id: str
    space: str
    control: bool = False
    max_tokens: int = 16_000
    # openai_compat
    base_url: str = ""
    key_file: str = ""
    temperature: float = 1.0
    top_p: float | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    # anthropic, claude_sdk
    effort: str = ""
    price: PriceTiers | None = None
    # claude_sdk: "adaptive", "disabled", or a thinking budget in tokens; "" = not passed
    thinking: str = ""
    # codex / agy; claude_sdk (optional): the `claude` executable, else the one on PATH
    binary: str = ""
    reasoning_effort: str = ""

    @property
    def is_cli(self) -> bool:
        return self.backend in CLI_BACKENDS

    @property
    def is_anthropic(self) -> bool:
        """Billed per token to an API key: the only backend the `[spend]` cap applies to."""
        return self.backend == "anthropic"

    @property
    def is_claude_sdk(self) -> bool:
        """Claude Code on the operator's subscription (`claude_sdk`): no key, no price."""
        return self.backend == "claude_sdk"


@dataclass(frozen=True)
class JailConf:
    wrapper: str = ""  # benchmark/k2val/jail/cli-jail.sh (relative to the repository root)


@dataclass(frozen=True)
class ScanConf:
    tokens_file: str = ""
    patterns_file: str = ""


@dataclass(frozen=True)
class K2valConfig:
    federation: FederationConf
    models: dict[str, ModelConf]
    limits: RunLimits = field(default_factory=RunLimits)
    spend: SpendConf = field(default_factory=SpendConf)
    jail: JailConf = field(default_factory=JailConf)
    scan: ScanConf = field(default_factory=ScanConf)
    tools_k1: tuple[str, ...] = K1_TOOLS
    tools_k2: tuple[str, ...] = K2_TOOLS
    artefact_root: Path = Path("artefacts/k2val")
    research_dir: Path = Path("research")
    path: Path | None = None


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table")
    return value


def _provider(raw: dict[str, Any], key: str) -> ProviderConf:
    table = _table(raw, key)
    for need in ("name", "host", "token_file"):
        if not table.get(need):
            raise ConfigError(f"federation.{key}.{need} is required")
    return ProviderConf(table["name"], table["host"], table["token_file"])


def _thinking(name: str, value: Any) -> str:
    """`thinking` as a string: "adaptive", "disabled" or a positive token budget; "" if unset."""
    if value is None or value == "":
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ConfigError(f"models.{name}.thinking must be a string or an integer")
    if isinstance(value, int) or str(value).isdigit():
        if int(value) <= 0:
            raise ConfigError(f"models.{name}.thinking: a token budget must be positive")
        return str(int(value))
    if value not in CLAUDE_THINKING_MODES:
        raise ConfigError(
            f"models.{name}.thinking must be one of {CLAUDE_THINKING_MODES} or a token budget; "
            f"got {value!r}"
        )
    return value


def _check_claude_sdk(name: str, conf: ModelConf) -> None:
    """A `claude_sdk` config rides on the subscription. A key or a price on it would only
    mislead (nothing reads them, and nothing is billed per token), so they are refused."""
    if conf.effort not in CLAUDE_EFFORTS:
        raise ConfigError(
            f"models.{name}.effort is required for claude_sdk and must be one of "
            f"{CLAUDE_EFFORTS}; got {conf.effort!r}"
        )
    for key, present in (("key_file", conf.key_file), ("price", conf.price)):
        if present:
            raise ConfigError(
                f"models.{name} (claude_sdk) runs on the subscription: remove {key} "
                "(it is for the 'anthropic' backend, which bills an API key)"
            )


def _model(name: str, raw: dict[str, Any]) -> ModelConf:
    backend = raw.get("backend", "")
    if backend not in BACKENDS:
        raise ConfigError(f"models.{name}.backend must be one of {BACKENDS}; got {backend!r}")
    for need in ("api_id", "space"):
        if not raw.get(need):
            raise ConfigError(f"models.{name}.{need} is required")
    sampling = raw.get("sampling", {})
    known = {"temperature", "top_p", "top_k", "repetition_penalty"}
    unknown = set(sampling) - known
    if unknown:
        raise ConfigError(f"models.{name}.sampling has unknown keys {sorted(unknown)}")
    # top_k and repetition_penalty are vLLM fields: they travel in extra_body.
    extra_body = {k: sampling[k] for k in ("top_k", "repetition_penalty") if k in sampling}
    extra_body.update(raw.get("extra_body", {}))
    price = None
    if "price" in raw:
        p = raw["price"]
        try:
            price = PriceTiers(
                input_low=float(p["input_low"]),
                output_low=float(p["output_low"]),
                input_high=float(p.get("input_high", p["input_low"])),
                output_high=float(p.get("output_high", p["output_low"])),
                threshold_tokens=int(p.get("threshold_tokens", 200_000)),
            )
        except KeyError as e:
            raise ConfigError(f"models.{name}.price needs {e.args[0]}") from None
    thinking = _thinking(name, raw.get("thinking"))
    conf = ModelConf(
        name=name,
        backend=backend,
        api_id=raw["api_id"],
        space=raw["space"],
        control=bool(raw.get("control", False)),
        max_tokens=int(raw.get("max_tokens", 16_000)),
        base_url=raw.get("base_url", ""),
        key_file=raw.get("key_file", ""),
        temperature=float(sampling.get("temperature", 1.0)),
        top_p=float(sampling["top_p"]) if "top_p" in sampling else None,
        extra_body=extra_body,
        effort=raw.get("effort", ""),
        price=price,
        thinking=thinking,
        binary=raw.get("binary", ""),
        reasoning_effort=raw.get("reasoning_effort", ""),
    )
    if backend == "openai_compat" and not (conf.base_url and conf.key_file):
        raise ConfigError(f"models.{name} (openai_compat) needs base_url and key_file")
    if backend == "anthropic" and not (conf.key_file and conf.effort and conf.price):
        raise ConfigError(f"models.{name} (anthropic) needs key_file, effort and [price]")
    if backend == "claude_sdk":
        _check_claude_sdk(name, conf)
    elif conf.thinking:
        raise ConfigError(f"models.{name}.thinking is only for the claude_sdk backend")
    if conf.is_cli and not conf.binary:
        raise ConfigError(f"models.{name} ({backend}) needs binary")
    # The jail gives the CLI a fresh home per run, and agy's MCP server is written into it, so
    # there is one way left. "settings" (the old per-session mode) is accepted and means that.
    if raw.get("mcp_attach", "home") not in ("home", "settings"):
        raise ConfigError(
            f"models.{name}.mcp_attach: only 'home' is supported (the jailed CLI gets a fresh "
            "home per run; `agy mcp add` would register the server in a home that is then "
            "deleted). Remove the key."
        )
    return conf


def parse_config(raw: dict[str, Any], *, path: Path | None = None) -> K2valConfig:
    fed = _table(raw, "federation")
    zone = fed.get("onezone", {})
    if not zone.get("host"):
        raise ConfigError("federation.onezone.host is required")
    federation = FederationConf(
        onezone_host=zone["host"],
        onezone_token_file=zone.get("token_file", ""),
        local=_provider(fed, "local"),
        owner=_provider(fed, "owner"),
        allow_insecure_tls=bool(fed.get("allow_insecure_tls", True)),
    )
    check_hosts(federation.hosts())

    models_raw = _table(raw, "models")
    if not models_raw:
        raise ConfigError("[models.*] is empty")
    models = {name: _model(name, table) for name, table in models_raw.items()}
    spaces = [m.space for m in models.values()]
    if len(set(spaces)) != len(spaces):
        raise ConfigError("every model needs its own space (models.*.space repeats)")

    run = _table(raw, "run")
    limits = RunLimits(
        max_tool_rounds=int(run.get("max_tool_rounds", 40)),
        wall_cap_s=float(run.get("wall_cap_s", 1200)),
        tool_call_cap_s=float(run.get("tool_call_cap_s", 300)),
        settle_s=float(run.get("settle_s", 60)),
        concurrency_per_model=min(
            int(run.get("concurrency_per_model", MAX_CONCURRENCY_PER_MODEL)),
            MAX_CONCURRENCY_PER_MODEL,
        ),
    )
    spend_raw = _table(raw, "spend")
    spend = SpendConf(
        cap_usd=float(spend_raw.get("cap_usd", 50.0)),
        reserve_usd=float(spend_raw.get("reserve_usd", 0.50)),
    )
    jail_raw = _table(_table(raw, "cli"), "jail")
    scan_raw = _table(_table(raw, "cli"), "scan")
    tools = _table(raw, "tools")
    return K2valConfig(
        federation=federation,
        models=models,
        limits=limits,
        spend=spend,
        jail=JailConf(wrapper=jail_raw.get("wrapper", "")),
        scan=ScanConf(
            tokens_file=scan_raw.get("tokens_file", ""),
            patterns_file=scan_raw.get("patterns_file", ""),
        ),
        tools_k1=tuple(tools.get("k1", K1_TOOLS)),
        tools_k2=tuple(tools.get("k2", K2_TOOLS)),
        artefact_root=Path(run.get("artefact_root", "artefacts/k2val")),
        research_dir=Path(run.get("research_dir", "research")),
        path=path,
    )


def load_config(path: str | Path) -> K2valConfig:
    file = Path(path)
    try:
        with file.open("rb") as f:
            raw = tomllib.load(f)
    except OSError as e:
        raise ConfigError(f"cannot read config {file} ({type(e).__name__})") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config {file} is not valid TOML: {e}") from None
    return parse_config(raw, path=file)


@dataclass(frozen=True)
class FederationSecrets:
    local_token: SecretStr
    owner_token: SecretStr
    onezone_token: SecretStr


def read_federation_secrets(fed: FederationConf) -> FederationSecrets:
    """Read the token files now. The onezone token defaults to the local provider's: an
    Onedata access token is valid at the zone and at its providers."""
    local = read_secret_file(fed.local.token_file)
    owner = read_secret_file(fed.owner.token_file)
    zone = read_secret_file(fed.onezone_token_file) if fed.onezone_token_file else local
    return FederationSecrets(local, owner, zone)


def read_model_key(model: ModelConf) -> SecretStr:
    return read_secret_file(model.key_file)
