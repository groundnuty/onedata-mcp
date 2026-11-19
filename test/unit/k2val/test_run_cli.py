"""The command line, the make target, and what a run does with the CLI legs."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from benchmark.k2val import run as run_module
from benchmark.k2val.app import MODULE_NAMES, build_app
from test.unit.k2val.conftest import TOKEN_LOCAL
from test.unit.k2val.fakefed import LOCAL_HOST

REPO = Path(__file__).resolve().parents[3]


def test_help_works(capsys) -> None:
    with pytest.raises(SystemExit) as exit_:
        run_module.main(["--help"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--config", "--models", "--scenarios", "--k", "--run-id"):
        assert flag in out
    assert "scan" in out and "change" in out


def test_the_module_runs_as_a_command() -> None:
    out = subprocess.run(
        [str(REPO / ".venv/bin/python"), "-m", "benchmark.k2val.run", "--help"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    assert out.returncode == 0 and "--scenarios" in out.stdout


def test_make_has_the_k2val_targets() -> None:
    out = subprocess.run(
        ["make", "-n", "k2val", "K2VAL_MODELS=glm,haiku-low", "K2VAL_K=1", "K2VAL_RID=r1"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert "benchmark.k2val.run --config benchmark/k2val/k2val.toml" in out.stdout
    assert "--models glm,haiku-low" in out.stdout and "--run-id r1" in out.stdout
    assert "--scenarios V1..V7 --k 1" in out.stdout
    scan = subprocess.run(["make", "-n", "k2val-scan"], capture_output=True, text=True, cwd=REPO)
    assert scan.returncode == 0 and "k2val.run scan" in scan.stdout


def test_the_secret_and_local_config_are_gitignored() -> None:
    ignore = (REPO / ".gitignore").read_text().splitlines()
    assert "benchmark/k2val/k2val.toml" in ignore and "artefacts/k2val/" in ignore


def test_the_example_config_is_committed_and_has_only_placeholder_paths() -> None:
    text = (REPO / "benchmark/k2val/k2val.toml.example").read_text()
    keys = r"(?:token_file|key_file|tokens_file|patterns_file)"
    paths = re.findall(rf'{keys} = "([^"]+)"', text)
    assert paths
    assert all(re.match(r"<[a-z ]+ dir>/", p) for p in paths), paths
    # the jail wrapper is the one path that is not local: the script in this repository
    assert re.findall(r'wrapper = "([^"]+)"', text) == ["benchmark/k2val/jail/cli-jail.sh"]
    assert not re.search(r"/(?:home|[U]sers|mnt)/", text)


def test_the_app_registers_the_modules_main_registers() -> None:
    """A drift guard: `build_app` copies main's module list instead of importing main (whose
    import has side effects)."""
    main_src = (REPO / "onedata_mcp/main.py").read_text()
    in_main = tuple(re.findall(r"^\s+(\w+)\.register_module\(mcp\)", main_src, flags=re.M))
    assert in_main == MODULE_NAMES


@pytest.mark.asyncio
async def test_the_app_offers_every_tool_of_the_study_sets(cfg, monkeypatch) -> None:
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_HOST", LOCAL_HOST)
    monkeypatch.setenv("ONEDATA_ONEPROVIDER_TOKEN", TOKEN_LOCAL)
    names = {t.name for t in await build_app().list_tools()}
    assert set(cfg.tools_k2) <= names
    assert len(cfg.tools_k1) == 11 and len(cfg.tools_k2) == 13


def test_the_cli_legs_are_skipped_and_the_run_goes_on_with_the_api_configs(
    tmp_path, monkeypatch, capsys
) -> None:
    from benchmark.k2val import runner

    token = tmp_path / "t"
    token.write_text(TOKEN_LOCAL)
    key = tmp_path / "k"
    key.write_text("lk-key-for-the-test")
    toml = tmp_path / "k2val.toml"
    toml.write_text(
        f"""
[federation.onezone]
host = "https://zone.k2val.datahub.test"
[federation.local]
name = "vistula-dc"
host = "https://local.k2val.datahub.test"
token_file = "{token}"
[federation.owner]
name = "nordic-marine-dc"
host = "https://owner.k2val.datahub.test"
token_file = "{token}"
[run]
artefact_root = "{tmp_path / "artefacts"}"
research_dir = "{tmp_path / "research"}"
[models.glm]
backend = "openai_compat"
api_id = "zai-org/GLM-5.3-Flash"
space = "k2val-glm"
base_url = "https://llmlab.example/api/v1"
key_file = "{key}"
[models.gpt-luna-low]
backend = "codex"
binary = "codex"
api_id = "gpt-5.6-luna"
reasoning_effort = "low"
space = "k2val-gpt"
"""
    )
    for var in (
        "ONEDATA_MCP_DOTENV",
        "ONEDATA_ONEPROVIDER_HOST",
        "ONEDATA_ONEPROVIDER_TOKEN",
        "ONEDATA_ONEZONE_HOST",
        "ONEDATA_ONEZONE_TOKEN",
        "ONEDATA_ALLOW_INSECURE_TLS",
    ):
        monkeypatch.setenv(var, "restored-by-monkeypatch")
    seen = {}

    async def fake_sweep(deps, models, scenarios, k, *, on_trial=None):
        seen["models"] = [m.name for m in models]
        seen["scenarios"] = [s.id for s in scenarios]
        return []

    monkeypatch.setattr(runner, "run_sweep", fake_sweep)

    code = run_module.main(
        ["--config", str(toml), "--scenarios", "V4", "--k", "1", "--run-id", "r1"]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert seen == {"models": ["glm"], "scenarios": ["V4"]}
    assert "SKIPPED gpt-luna-low: wave 2, disabled: [cli.jail] wrapper is not set" in out
    assert (tmp_path / "artefacts" / "r1" / "tools_snapshot.json").exists()
    assert (tmp_path / "research" / "k2val-r1.md").exists()
    assert TOKEN_LOCAL not in out


def test_a_non_local_host_stops_the_run_before_anything_happens(tmp_path, capsys) -> None:
    toml = tmp_path / "k2val.toml"
    toml.write_text(
        """
[federation.onezone]
host = "https://data.spice-platform.eu"
[federation.local]
name = "a"
host = "https://op1.spice-platform.eu"
token_file = "/nonexistent"
[federation.owner]
name = "b"
host = "https://op2.spice-platform.eu"
token_file = "/nonexistent"
[models.glm]
backend = "openai_compat"
api_id = "x"
space = "s"
base_url = "https://llmlab.example/api/v1"
key_file = "/nonexistent"
"""
    )
    code = run_module.main(["--config", str(toml)])
    assert code == 2
    assert "refusing to run" in capsys.readouterr().err
