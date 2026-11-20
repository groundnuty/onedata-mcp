"""The validation federation's driver (benchmark/valfed): its layout, its caps and the panel
config it writes. No Docker, no network."""

from __future__ import annotations

import io
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmark.k2val.config import load_config, parse_config
from benchmark.valfed import valfed

LAYOUT = valfed.layout()
SPACES = set(LAYOUT["spaces"])


def test_every_panel_config_has_its_own_declared_space() -> None:
    assert set(valfed.PANEL.values()) <= SPACES
    assert len(set(valfed.PANEL.values())) == len(valfed.PANEL)
    assert {valfed.LIVE_SPACE, valfed.SMOKE_SPACE} <= SPACES
    declared = {*valfed.PANEL.values(), valfed.LIVE_SPACE, valfed.SMOKE_SPACE}
    assert declared == SPACES


def test_data_are_written_at_owner_and_read_at_local() -> None:
    assert set(LAYOUT["providers"]) == set(valfed.TOKENS.values()) == {"local", "owner"}
    for space in LAYOUT["spaces"].values():
        assert space["owner"] == "owner"
        assert space["mirrors"] == ["local"]
        assert space["baseline"] is False


def test_the_user_is_a_member_of_every_space_with_the_panel_rights() -> None:
    user = LAYOUT["principals"][valfed.USER]
    assert set(user["spaces"]) == SPACES
    assert set(user["privileges"]) == {
        "space_view",
        "space_read_data",
        "space_write_data",
        "space_view_qos",
        "space_manage_qos",
        "space_view_transfers",
        "space_schedule_replication",
    }


def test_caps_fit_the_host_budget() -> None:
    services = {"zone", *LAYOUT["providers"]}
    assert set(valfed.CPUS) == services
    assert sum(valfed.CPUS.values()) <= 4
    assert valfed.layout_memory(LAYOUT) <= 10 << 30
    ports = [LAYOUT["zone"]["port"], *(p["port"] for p in LAYOUT["providers"].values())]
    assert ports == [27443, 27444, 27445]


def test_memory_units() -> None:
    assert valfed.memory_bytes("3g") == 3 << 30
    assert valfed.memory_bytes("3584m") == 3584 << 20
    with pytest.raises(AttributeError):
        valfed.memory_bytes("3.5g")


FACTS = {
    "zone": {"url": "https://127.0.0.1:27443"},
    "providers": {
        "local": {"url": "https://127.0.0.1:27444", "id": "p-local"},
        "owner": {"url": "https://127.0.0.1:27445", "id": "p-owner"},
    },
}


def _config(tmp_path: Path, key_dir: str = "") -> str:
    home = valfed.Home(tmp_path)
    names = {"local": "local", "owner": "owner"}
    return valfed.panel_config(FACTS, names, home, key_dir)


def test_panel_config_passes_the_panels_own_checks(tmp_path: Path) -> None:
    text = _config(tmp_path, key_dir="/keys")
    cfg = parse_config(tomllib.loads(text))
    assert cfg.federation.allow_insecure_tls is False
    assert cfg.federation.local.host == "https://127.0.0.1:27444"
    assert cfg.federation.owner.token_file == str(tmp_path / "tokens" / "writer.token")
    assert cfg.federation.local.token_file == str(tmp_path / "tokens" / "tools.token")
    assert cfg.federation.onezone_token_file == cfg.federation.local.token_file
    assert {m.name: m.space for m in cfg.models.values()} == valfed.PANEL
    # the Claude configs run on the subscription (claude_sdk) and have no key file
    keyed = [m for m in cfg.models.values() if not m.is_claude_sdk]
    assert keyed and all(m.key_file.startswith("/keys/") for m in keyed)
    assert all(not m.key_file for m in cfg.models.values() if m.is_claude_sdk)


def test_panel_config_keeps_the_example_settings(tmp_path: Path) -> None:
    example = load_config(valfed.PANEL_EXAMPLE)
    cfg = parse_config(tomllib.loads(_config(tmp_path)))
    assert cfg.limits == example.limits
    assert cfg.spend == example.spend
    for name, model in cfg.models.items():
        assert model.api_id == example.models[name].api_id
        assert model.extra_body == example.models[name].extra_body
        assert model.key_file == example.models[name].key_file  # the placeholder stays


def test_core_env_drops_inherited_federation_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("EXPCORE_FED_CA_DIR", "/elsewhere/ca")
    monkeypatch.setenv("EXPCORE_FED_STUDY_ARTIFACTS", "/elsewhere/study")
    monkeypatch.setenv("VIRTUAL_ENV", "/elsewhere/venv")
    env = valfed.core_env(valfed.Home(tmp_path))
    assert env["EXPCORE_FED_CA_DIR"] == str(tmp_path / "ca")
    assert env["EXPCORE_FED_ROOT"] == str(tmp_path / "fed")
    assert env["EXPCORE_FED_ROOT_PARENT"] == str(tmp_path)
    assert "EXPCORE_FED_STUDY_ARTIFACTS" not in env
    assert "VIRTUAL_ENV" not in env


def test_extract_strips_the_tarball_top_directory(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in (("top/README.md", b"core"), ("top/deploy/x.py", b"x = 1")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    valfed._extract(buffer, tmp_path, strip=1)
    assert (tmp_path / "README.md").read_bytes() == b"core"
    assert (tmp_path / "deploy" / "x.py").read_bytes() == b"x = 1"


@pytest.mark.parametrize(
    ("command", "test", "enable", "space"),
    [
        (
            "readiness-live",
            "test/live/test_readiness_live.py",
            "ONEDATA_MCP_LIVE_READINESS",
            "ONEDATA_MCP_LIVE_READINESS_SPACE",
        ),
        (
            "qos-live",
            "test/live/test_qos_live.py",
            "ONEDATA_MCP_LIVE_QOS",
            "ONEDATA_MCP_LIVE_QOS_SPACE",
        ),
        (
            "files-live",
            "test/live/test_files_live.py",
            "ONEDATA_MCP_LIVE_FILES",
            "ONEDATA_MCP_LIVE_FILES_SPACE",
        ),
    ],
)
def test_a_live_test_runs_in_the_live_space_with_the_token_in_the_environment_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    test: str,
    enable: str,
    space: str,
) -> None:
    """No federation: `facts` and `subprocess.run` are faked."""
    token = "tok-tools-SECRET-1f2e3d"
    home = valfed.Home(tmp_path)
    home.tokens.mkdir()
    home.token_file("tools").write_text(token + "\n")
    monkeypatch.setattr(valfed, "facts", lambda _home: FACTS)
    runs: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv, cwd, env):
        runs.append((argv, env))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(valfed.subprocess, "run", fake_run)

    valfed.COMMANDS[command](home)

    ((argv, env),) = runs
    assert argv[-3:] == [test, "-v", "-s"]
    assert not any(token in part for part in argv)
    assert env[enable] == "1" and env[space] == valfed.LIVE_SPACE
    assert env["ONEDATA_ONEPROVIDER_HOST"] == FACTS["providers"]["local"]["url"]
    assert env["ONEDATA_ONEZONE_HOST"] == FACTS["zone"]["url"]
    assert env["ONEDATA_ONEPROVIDER_TOKEN"] == env["ONEDATA_ONEZONE_TOKEN"] == token
    assert env["ONEDATA_ALLOW_INSECURE_TLS"] == "false"
    assert env["SSL_CERT_FILE"] == str(home.ca_pem)
    assert env["ONEDATA_MCP_DOTENV"] == "0"


def test_the_transfer_live_test_gets_the_owner_and_the_writer_token_in_the_environment_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its probe files are written through `owner`, so that a copy to `local` has data to move."""
    tools, writer = "tok-tools-SECRET-1f2e3d", "tok-writer-SECRET-9a8b7c"
    home = valfed.Home(tmp_path)
    home.tokens.mkdir()
    home.token_file("tools").write_text(tools + "\n")
    home.token_file("writer").write_text(writer + "\n")
    monkeypatch.setattr(valfed, "facts", lambda _home: FACTS)
    runs: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv, cwd, env):
        runs.append((argv, env))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(valfed.subprocess, "run", fake_run)

    valfed.COMMANDS["transfer-live"](home)

    ((argv, env),) = runs
    assert argv[-3:] == ["test/live/test_transfer_live.py", "-v", "-s"]
    assert not any(tools in part or writer in part for part in argv)
    assert env["ONEDATA_MCP_LIVE_TRANSFER"] == "1"
    assert env["ONEDATA_MCP_LIVE_TRANSFER_SPACE"] == valfed.LIVE_SPACE
    assert env["ONEDATA_ONEPROVIDER_HOST"] == FACTS["providers"]["local"]["url"]
    assert env["ONEDATA_ONEPROVIDER_TOKEN"] == tools
    assert env["ONEDATA_MCP_LIVE_TRANSFER_SOURCE_HOST"] == FACTS["providers"]["owner"]["url"]
    assert env["ONEDATA_MCP_LIVE_TRANSFER_SOURCE_TOKEN"] == writer
    assert env["SSL_CERT_FILE"] == str(home.ca_pem)


def test_a_failed_live_test_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = valfed.Home(tmp_path)
    home.tokens.mkdir()
    home.token_file("tools").write_text("tok\n")
    monkeypatch.setattr(valfed, "facts", lambda _home: FACTS)
    monkeypatch.setattr(valfed.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))

    with pytest.raises(valfed.ValfedError, match="the live qos test failed"):
        valfed.qos_live(home)
