"""The validation federation's driver (benchmark/valfed): its layout, its caps and the panel
config it writes. No Docker, no network."""

from __future__ import annotations

import io
import tarfile
import tomllib
from pathlib import Path

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
