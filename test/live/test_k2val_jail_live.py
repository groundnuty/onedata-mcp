"""Live proof of the CLI jail: runs the leak probe (benchmark/k2val/jail/leak-probe.sh) and
asserts that every row of its table passes.

Skipped unless bwrap and slirp4netns are installed AND K2VAL_JAIL_LIVE=1. A CLI whose login is
missing under $HOME is skipped. The probe starts no CLI and sends no prompt: its only outbound
traffic is one TLS handshake each to api.openai.com, chatgpt.com and
generativelanguage.googleapis.com.

    make test-k2val-jail-live

K2VAL_CONFIG (optional): a k2val.toml whose token and key files must be absent in the jail too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PROBE = REPO / "benchmark/k2val/jail/leak-probe.sh"
LOGINS = {"codex": ".codex/auth.json", "agy": ".gemini/antigravity-cli/antigravity-oauth-token"}

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (shutil.which("bwrap") and shutil.which("slirp4netns")),
        reason="needs bwrap and slirp4netns",
    ),
    pytest.mark.skipif(os.environ.get("K2VAL_JAIL_LIVE") != "1", reason="set K2VAL_JAIL_LIVE=1"),
]


@pytest.mark.parametrize("cli", ["codex", "agy"])
def test_every_leak_probe_row_passes(cli: str) -> None:
    if not (Path.home() / LOGINS[cli]).is_file():
        pytest.skip(f"no {cli} login under $HOME")
    cmd = [str(PROBE), "--cli", cli]
    config = os.environ.get("K2VAL_CONFIG", "")
    if config and Path(config).is_file():
        cmd += ["--config", config]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    print(r.stdout)
    rows = [line for line in r.stdout.splitlines() if "  PASS  " in line or "  FAIL  " in line]
    assert rows, f"no table printed: {r.stderr[-2000:]}"
    failed = [line for line in rows if "  FAIL  " in line]
    assert not failed, "\n".join(failed)
    assert r.returncode == 0, r.stderr[-2000:]
