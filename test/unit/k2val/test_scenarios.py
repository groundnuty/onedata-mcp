"""The briefs: they name everything the agent cannot discover with the tools it is offered,
and never name a tool."""

from __future__ import annotations

import re

import pytest

from benchmark.k2val.config import K1_TOOLS, K2_TOOLS
from benchmark.k2val.scenarios import SCENARIOS

DIR = "/k2val-x/logs-v-x-k0-abc123"


def brief(sid: str) -> str:
    return SCENARIOS[sid].render_brief(directory=DIR, local="vistula-dc")


def test_v1_names_all_six_files_because_the_k1_set_has_no_listing_tool() -> None:
    assert not {"list_children", "list_files_recursively"} & set(K1_TOOLS)
    text = brief("V1")
    assert text == (
        f"Which of these files in `{DIR}` are fully stored at your site vistula-dc: "
        + ", ".join(f"log_2020010{d}.dat" for d in range(1, 7))
        + "? List the names of those that are."
    )
    for f in SCENARIOS["V1"].fixture.files:
        assert f.name in text


@pytest.mark.parametrize("sid", ["V1", "V2", "V3"])
def test_a_k1_brief_names_every_file_of_its_fixture(sid: str) -> None:
    """With no listing tool in the K1 set, a file the brief does not name cannot be found."""
    for f in SCENARIOS[sid].fixture.files:
        assert f.name in brief(sid)
    assert DIR in brief(sid)


@pytest.mark.parametrize("sid", ["V4", "V5", "V6", "V7"])
def test_a_k2_brief_names_the_directory_and_the_dates_the_readiness_tools_resolve(
    sid: str,
) -> None:
    text = brief(sid)
    assert DIR in text and "1\u20135 January 2020" in text
    # the readiness tools list the directory themselves, from a date range: no file names needed
    assert not re.search(r"log_\d{8}", text)
    assert {"check_readiness", "ensure_readiness"} <= set(K2_TOOLS)


@pytest.mark.parametrize("sid", sorted(SCENARIOS))
def test_no_brief_names_a_tool(sid: str) -> None:
    text = brief(sid)
    for tool in K2_TOOLS:
        assert tool not in text
    assert "readiness" not in text.lower()
