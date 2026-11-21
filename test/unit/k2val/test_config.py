"""Configuration, the host guard, secrets and spend tracking."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from benchmark.k2val.config import (
    ConfigError,
    HostGuardError,
    PriceTiers,
    check_hosts,
    hostname_of,
    is_local_host,
    load_config,
    parse_config,
    read_federation_secrets,
    read_model_key,
)
from benchmark.k2val.secrets import (
    MASK,
    MASKER,
    SecretFileError,
    SecretStr,
    read_secret_file,
    redact,
    say,
)
from benchmark.k2val.spend import SpendTracker
from benchmark.llm_adapters.anthropic_api import AdapterStop
from test.unit.k2val.conftest import KEY_ANTHROPIC, TOKEN_LOCAL, TOKEN_OWNER, raw_config

EXAMPLE = Path(__file__).resolve().parents[3] / "benchmark/k2val/k2val.toml.example"


# --- the host guard ----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8443",
        "http://localhost",
        "https://oneprovider.vistula.datahub.test",
        "https://ONEPROVIDER.VISTULA.DATAHUB.TEST.",
    ],
)
def test_local_hosts_pass(url: str) -> None:
    check_hosts({"x": url})


@pytest.mark.parametrize(
    "url",
    [
        "https://data.spice-platform.eu",
        "https://127.0.0.1.evil.example",
        "https://evil.example/.datahub.test",
        "https://evil.datahub.test.evil.example",
        "https://datahub.test",
        "https://user:pw@127.0.0.1",
        "127.0.0.1:8443",
        "https://[::1]:8443",
        "",
    ],
)
def test_non_local_hosts_are_refused(url: str) -> None:
    with pytest.raises(HostGuardError, match="refusing to run"):
        check_hosts({"federation.local.host": url})


def test_the_guard_names_the_key_and_never_echoes_credentials_in_a_url() -> None:
    with pytest.raises(HostGuardError) as err:
        check_hosts({"federation.owner.host": "https://user:supersecret@127.0.0.1"})
    assert "federation.owner.host" in str(err.value)
    assert "supersecret" not in str(err.value)


def test_helpers() -> None:
    assert hostname_of("https://A.b.example:99/x") == "a.b.example"
    assert hostname_of("ftp://127.0.0.1") is None
    assert is_local_host("localhost") and not is_local_host(None)


@pytest.mark.parametrize("which", ["onezone", "local", "owner"])
def test_every_federation_host_is_guarded_the_zone_too(secret_files, which: str) -> None:
    raw = raw_config(secret_files)
    if which == "onezone":
        raw["federation"]["onezone"]["host"] = "https://onezone.example.org"
    else:
        raw["federation"][which]["host"] = "https://op.example.org"
    with pytest.raises(HostGuardError):
        parse_config(raw)


# --- parsing --------------------------------------------------------------------------


def test_the_panel_models_and_their_settings(cfg) -> None:
    m = cfg.models
    assert m["glm"].top_p == 0.95 and m["glm"].temperature == 1.0
    assert m["glm"].extra_body == {}  # reasoning stays at the server default
    assert m["qwen36"].top_p is None and m["qwen36"].extra_body == {}
    assert m["haiku-low"].effort == "low" and m["haiku-high"].effort == "high"
    assert m["sonnet-medium"].control and m["sonnet-medium"].api_id == "claude-sonnet-5-5"
    assert m["gpt-luna-low"].is_cli


def test_qwencoder_sampling_goes_to_the_right_places(secret_files) -> None:
    raw = raw_config(secret_files)
    raw["models"]["qwencoder"] = {
        "backend": "openai_compat",
        "api_id": "Qwen/Qwen3-Coder-30B-A3B-Instruct",
        "space": "k2val-qwencoder",
        "base_url": "https://llmlab.example/api/v1",
        "key_file": str(secret_files["lk"]),
        "sampling": {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "repetition_penalty": 1.05},
    }
    conf = parse_config(raw).models["qwencoder"]
    assert (conf.temperature, conf.top_p) == (0.7, 0.8)
    assert conf.extra_body == {"top_k": 20, "repetition_penalty": 1.05}


def test_the_example_config_is_valid_and_lists_every_config_and_space() -> None:
    cfg = load_config(EXAMPLE)
    assert set(cfg.models) == {
        "sonnet-medium",
        "glm",
        "qwen36",
        "qwencoder",
        "deepseek",
        "haiku-low",
        "haiku-medium",
        "haiku-high",
        "gpt-luna-low",
        "gemini-flash-low",
    }
    assert {m.space for m in cfg.models.values()} == {
        "k2val-sonnet",
        "k2val-glm",
        "k2val-qwen36",
        "k2val-qwencoder",
        "k2val-deepseek",
        "k2val-haiku-low",
        "k2val-haiku-medium",
        "k2val-haiku-high",
        "k2val-gpt",
        "k2val-gemini",
    }
    assert cfg.models["deepseek"].api_id == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert cfg.models["deepseek"].top_p == 0.95
    assert cfg.models["qwencoder"].extra_body == {"top_k": 20, "repetition_penalty": 1.05}
    assert cfg.models["sonnet-medium"].control
    assert cfg.spend.cap_usd == 50.0 and cfg.spend.reserve_usd == 0.5
    assert cfg.limits.concurrency_per_model == 1
    assert cfg.jail.wrapper == ""  # the CLI legs are off until a wrapper is set
    # no secret in the example: only placeholder paths
    assert "SECRET" not in EXAMPLE.read_text()


@pytest.mark.parametrize("value", [0, 2, 9])
def test_concurrency_per_config_is_one_and_cannot_be_raised(secret_files, value) -> None:
    raw = raw_config(secret_files)
    raw["run"]["concurrency_per_model"] = value
    with pytest.raises(ConfigError, match="concurrency_per_model must be 1"):
        parse_config(raw)
    raw["run"]["concurrency_per_model"] = 1
    assert parse_config(raw).limits.concurrency_per_model == 1
    del raw["run"]["concurrency_per_model"]
    assert parse_config(raw).limits.concurrency_per_model == 1


def test_the_spend_cap_is_a_config_value_with_a_default_of_50(secret_files) -> None:
    raw = raw_config(secret_files)
    del raw["spend"]
    assert parse_config(raw).spend.cap_usd == 50.0
    raw["spend"] = {"cap_usd": 7.5}
    assert parse_config(raw).spend.cap_usd == 7.5


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda r: r["models"]["glm"].update(backend="nope"), "backend"),
        (lambda r: r["models"]["glm"].update(space="k2val-qwen36"), "own space"),
        (lambda r: r["models"]["glm"].pop("base_url"), "needs base_url"),
        (lambda r: r["models"]["haiku-low"].pop("price"), "needs key_file, effort and"),
        (lambda r: r["models"]["glm"]["sampling"].update(min_p=0.1), "unknown keys"),
        (lambda r: r["models"].clear(), "empty"),
        (lambda r: r["federation"].pop("owner"), "federation.owner"),
    ],
)
def test_config_errors_name_the_key(secret_files, mutate, needle: str) -> None:
    raw = copy.deepcopy(raw_config(secret_files))
    mutate(raw)
    with pytest.raises(ConfigError, match=needle):
        parse_config(raw)


def test_price_tiers() -> None:
    p = PriceTiers(0.10, 0.50, 0.50, 2.50, 200_000)
    assert p.cost(100_000, 1_000_000) == pytest.approx(0.01 + 0.50)  # low tier
    assert p.cost(200_001, 1_000_000) == pytest.approx(0.50 * 0.200001 + 2.50)  # high tier
    assert p.cost(200_000, 0) == pytest.approx(0.02)  # the boundary is in the low tier


# --- secrets ----------------------------------------------------------------------------


def test_secret_files_are_read_at_run_time_and_masked(cfg) -> None:
    secrets = read_federation_secrets(cfg.federation)
    assert secrets.local_token.reveal() == TOKEN_LOCAL
    assert secrets.onezone_token.reveal() == TOKEN_LOCAL  # defaults to the local provider's
    assert TOKEN_OWNER not in repr(secrets) and TOKEN_LOCAL not in repr(secrets)
    assert (
        redact(f"auth {TOKEN_LOCAL} failed for {TOKEN_OWNER}") == f"auth {MASK} failed for {MASK}"
    )
    assert read_model_key(cfg.models["haiku-low"]).reveal() == KEY_ANTHROPIC
    assert KEY_ANTHROPIC not in redact(f"key={KEY_ANTHROPIC}")


def test_masker_catches_escaped_and_url_encoded_forms() -> None:
    MASKER.register('tok"en/with spaces&more')
    assert "with spaces" not in redact('{"k": "tok\\"en/with spaces&more"}')
    assert "with%20spaces" not in redact("?t=tok%22en%2Fwith%20spaces%26more")


def test_short_values_are_not_registered_so_text_is_not_corrupted() -> None:
    MASKER.register("abc")
    assert redact("abc abc") == "abc abc"


def test_secret_str_shows_nothing() -> None:
    s = SecretStr("hunter2hunter2")
    assert "hunter2" not in repr(s) and "hunter2" not in str(s) and "hunter2" not in f"{s}"


def test_a_bad_secret_file_error_never_holds_content(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    with pytest.raises(SecretFileError, match="empty"):
        read_secret_file(empty)
    with pytest.raises(SecretFileError, match="cannot read"):
        read_secret_file(tmp_path / "missing")


def test_say_redacts(capsys, cfg) -> None:
    read_federation_secrets(cfg.federation)
    say(f"the token is {TOKEN_LOCAL}")
    out = capsys.readouterr().out
    assert TOKEN_LOCAL not in out and MASK in out


# --- spend ------------------------------------------------------------------------------

PRICE = PriceTiers(0.10, 0.50, 0.50, 2.50)


def test_spend_is_recorded_per_call_and_the_run_total_is_kept(tmp_path: Path) -> None:
    t = SpendTracker(50.0, 0.5, tmp_path)

    c1 = t.record("haiku-low", "msg_1", 100_000, 0, PRICE)
    t.record("sonnet-medium", "msg_2", 0, 100_000, PRICE)

    assert c1 == pytest.approx(0.01)
    assert t.spent_usd == pytest.approx(0.06)
    assert t.per_model == {"haiku-low": pytest.approx(0.01), "sonnet-medium": pytest.approx(0.05)}
    rows = (tmp_path / "spend.jsonl").read_text().splitlines()
    assert len(rows) == 2 and '"run_total_usd"' in rows[1]
    # a resumed run reloads its spend: the cap is per run id, not per process
    assert SpendTracker(50.0, 0.5, tmp_path).spent_usd == pytest.approx(0.06)


def test_a_trial_is_refused_when_spend_plus_the_reserve_would_exceed_the_cap() -> None:
    t = SpendTracker(cap_usd=1.0, reserve_usd=0.5)
    assert t.try_begin_trial()  # 0 + 0.5 <= 1.0
    t.end_trial()
    t.record("m", "r", 0, 1_000_000, PriceTiers(0, 0.6, 0, 0.6))  # spend 0.6
    assert not t.try_begin_trial()  # 0.6 + 0.5 > 1.0
    assert t.in_flight == 0  # a refusal reserves nothing


def test_exactly_the_cap_with_the_reserve_still_starts() -> None:
    t = SpendTracker(cap_usd=1.0, reserve_usd=0.5)
    t.record("m", "r", 0, 1_000_000, PriceTiers(0, 0.5, 0, 0.5))  # spend 0.5
    assert t.try_begin_trial()  # 0.5 + 0.5 == 1.0, not above


def test_trials_in_flight_count_against_the_cap() -> None:
    t = SpendTracker(cap_usd=1.0, reserve_usd=0.5)
    assert t.try_begin_trial()
    assert t.try_begin_trial()  # 2 x 0.5 == 1.0
    assert not t.try_begin_trial()  # a third would not fit
    t.end_trial()
    assert t.try_begin_trial()


def test_no_call_is_made_once_the_cap_is_reached() -> None:
    t = SpendTracker(cap_usd=1.0, reserve_usd=0.5)
    t.before_call()
    t.record("m", "r", 0, 1_000_000, PriceTiers(0, 1.0, 0, 1.0))
    with pytest.raises(AdapterStop) as stop:
        t.before_call()
    assert stop.value.reason == "budget_exhausted"


def test_the_judge_section_defaults_to_sonnet_at_medium_and_is_configurable(secret_files) -> None:
    raw = raw_config(secret_files)
    raw.pop("judge", None)
    judge = parse_config(raw).judge
    assert (judge.model, judge.effort, judge.concurrency) == ("claude-sonnet-5-5", "medium", 2)
    raw["judge"] = {"model": "another-model", "effort": "high", "concurrency": 1}
    judge = parse_config(raw).judge
    assert (judge.model, judge.effort, judge.concurrency) == ("another-model", "high", 1)
    raw["judge"] = {"effort": "huge"}
    with pytest.raises(ConfigError, match="judge.effort"):
        parse_config(raw)
