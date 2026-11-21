"""Shared fixtures of the panel's tests: a fake federation, a fake clock, a config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from benchmark.k2val.config import K2valConfig, parse_config
from benchmark.k2val.federation import FedApi, provider_config
from benchmark.k2val.secrets import MASKER
from test.unit.k2val.fakefed import LOCAL_HOST, OWNER_HOST, ZONE_HOST, FakeClock, FakeFed

TOKEN_LOCAL = "tok-local-SECRET-7f3a91"
TOKEN_OWNER = "tok-owner-SECRET-5c2d08"
KEY_ANTHROPIC = "sk-ant-SECRET-key-91ab"
KEY_LLMLAB = "lk-SECRET-key-44cd"


@pytest.fixture(autouse=True)
def _never_the_real_judge(monkeypatch: pytest.MonkeyPatch):
    """A unit test never calls the real judge (design/08): its one call path raises. Tests of
    the judge's retry and error handling override `_ask` in a subclass, which this leaves be."""
    from benchmark.k2val import judge

    reached: list[str] = []

    async def refuse(self, spec, system, prompt):
        reached.append(prompt[:60])
        raise AssertionError("a unit test reached the real judge")

    monkeypatch.setattr(judge.ClaudeSdkJudge, "_ask", refuse)
    yield
    # the judge turns a raised error into a verdict's judge_error, so check after the test too
    assert reached == [], f"a unit test reached the real judge: {reached}"


@pytest.fixture(autouse=True)
def _masker():
    MASKER.clear()
    yield
    MASKER.clear()


@pytest.fixture
def secret_files(tmp_path: Path) -> dict[str, Path]:
    files = {
        "local": (tmp_path / "local.token", TOKEN_LOCAL),
        "owner": (tmp_path / "owner.token", TOKEN_OWNER),
        "anthropic": (tmp_path / "anthropic.key", KEY_ANTHROPIC),
        "lk": (tmp_path / "lk", KEY_LLMLAB),
    }
    for path, content in files.values():
        path.write_text(content + "\n")
    return {k: p for k, (p, _) in files.items()}


def raw_config(secret_files: dict[str, Path]) -> dict:
    price = {"input_low": 0.10, "output_low": 0.50, "input_high": 0.50, "output_high": 2.50}
    return {
        "federation": {
            "onezone": {"host": ZONE_HOST},
            "local": {
                "name": "vistula-dc",
                "host": LOCAL_HOST,
                "token_file": str(secret_files["local"]),
            },
            "owner": {
                "name": "nordic-marine-dc",
                "host": OWNER_HOST,
                "token_file": str(secret_files["owner"]),
            },
        },
        # wall_cap_s: far above any trial's own time on a loaded host (a 5 s cap failed a real-
        # tool trial at a load of 15); the tests of the cap set their own (0.2-0.3 s)
        "run": {"settle_s": 30, "wall_cap_s": 120, "tool_call_cap_s": 5},
        "spend": {"cap_usd": 1.0, "reserve_usd": 0.5},
        "models": {
            "sonnet-medium": {
                "backend": "anthropic",
                "control": True,
                "api_id": "claude-sonnet-5-5",
                "space": "k2val-sonnet",
                "key_file": str(secret_files["anthropic"]),
                "effort": "medium",
                "price": {"input_low": 2.0, "output_low": 10.0},
            },
            "glm": {
                "backend": "openai_compat",
                "api_id": "zai-org/GLM-5.3-Flash",
                "space": "k2val-glm",
                "base_url": "https://llmlab.example/api/v1",
                "key_file": str(secret_files["lk"]),
                "sampling": {"temperature": 1.0, "top_p": 0.95},
            },
            "qwen36": {
                "backend": "openai_compat",
                "api_id": "Qwen/Qwen3.6-35B-A3B",
                "space": "k2val-qwen36",
                "base_url": "https://llmlab.example/api/v1",
                "key_file": str(secret_files["lk"]),
                "sampling": {"temperature": 1.0},
            },
            "haiku-low": {
                "backend": "anthropic",
                "api_id": "claude-haiku-5-5",
                "space": "k2val-haiku-low",
                "key_file": str(secret_files["anthropic"]),
                "effort": "low",
                "price": price,
            },
            "haiku-medium": {
                "backend": "anthropic",
                "api_id": "claude-haiku-5-5",
                "space": "k2val-haiku-medium",
                "key_file": str(secret_files["anthropic"]),
                "effort": "medium",
                "price": price,
            },
            "haiku-high": {
                "backend": "anthropic",
                "api_id": "claude-haiku-5-5",
                "space": "k2val-haiku-high",
                "key_file": str(secret_files["anthropic"]),
                "effort": "high",
                "price": price,
            },
            "gpt-luna-low": {
                "backend": "codex",
                "binary": "codex",
                "api_id": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "space": "k2val-gpt",
            },
        },
    }


@pytest.fixture
def cfg(secret_files: dict[str, Path], tmp_path: Path) -> K2valConfig:
    return parse_config(raw_config(secret_files), path=tmp_path / "k2val.toml")


@pytest.fixture
def fed(httpx_mock: HTTPXMock) -> FakeFed:
    fake = FakeFed()
    for space in ("k2val-glm", "k2val-qwen36", "k2val-sonnet", "k2val-haiku-low", "k2val-gpt"):
        fake.add_space(space)
    httpx_mock.add_callback(fake, is_reusable=True, is_optional=True)
    yield fake
    assert fake.unexpected == []


@pytest.fixture
def clock(fed: FakeFed) -> FakeClock:
    return FakeClock(fed)


@pytest.fixture
def api(fed: FakeFed, clock: FakeClock) -> FedApi:
    return FedApi(
        provider_config(LOCAL_HOST, TOKEN_LOCAL, insecure_tls=False),
        provider_config(OWNER_HOST, TOKEN_OWNER, insecure_tls=False),
        clock=clock,
        poll_s=1.0,
    )


@pytest.fixture
def tools_app(monkeypatch: pytest.MonkeyPatch):
    """The in-process server with the real tool modules, pointed at the fake local provider
    (the file-tool scenarios' full trials, test_scenarios_{fa,lc,cd}.py)."""
    from benchmark.k2val.app import build_app

    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", LOCAL_HOST)
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", TOKEN_LOCAL)
    monkeypatch.setenv("ONEDATA_ONEZONE_HOST", ZONE_HOST)
    monkeypatch.setenv("ONEDATA_ONEZONE_TOKEN", TOKEN_LOCAL)
    monkeypatch.setenv("ONEDATA_ALLOW_INSECURE_TLS", "false")
    return build_app()
