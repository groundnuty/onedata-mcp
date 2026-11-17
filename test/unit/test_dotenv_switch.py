"""ONEDATA_MCP_DOTENV=0 stops `onedata_mcp.config` reading any `.env` file at import.

`config.py` calls python-dotenv's `load_dotenv()` at import. Its default search starts at the
directory of the file that calls it, the installed `onedata_mcp` package, and walks up to `/`,
so a `.env` in any ancestor of the package (an account's shared home, say) can set any
`ONEDATA_*` setting the launch leaves unset, with nothing in the launch saying so. A launch that
wants to say everything it sets puts `ONEDATA_MCP_DOTENV=0` in the process environment; unset,
the behaviour is the one other users of the server have always had.

Each case imports the config in a child Python started from a script file (not `-c`: with no
`__main__.__file__` python-dotenv searches from the working directory instead), on a copy of the
package planted under a temporary tree that holds the `.env` in an ancestor directory, and with
the working directory empty. The copy and the `__file__` assertion keep the test from passing
for want of a search that reached the file at all, and the unguarded case is its own control.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import onedata_mcp

NODE_NAMES = "ONEDATA_MCP_NODE_NAMES"
SWITCH = "ONEDATA_MCP_DOTENV"
UPSTREAM_SWITCH = "PYTHON_DOTENV_DISABLED"
NAMES = '{"node-1.site.example:8443": "provider-a"}'

DRIVER = """\
import json
import os
import sys

sys.path.insert(0, sys.argv[1])
before = dict(os.environ)
import onedata_mcp.config as config

print(
    json.dumps(
        {
            "file": config.__file__,
            "added": {k: v for k, v in os.environ.items() if k not in before},
            "changed": {k: v for k, v in os.environ.items() if k in before and before[k] != v},
            "removed": [k for k in before if k not in os.environ],
        }
    )
)
"""


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """`anc/.env` sets the node names; the package is copied to `anc/site`; `cwd` is empty."""
    ancestor = tmp_path / "anc"
    (ancestor / "site").mkdir(parents=True)
    shutil.copytree(
        Path(onedata_mcp.__file__).parent,
        ancestor / "site" / "onedata_mcp",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (ancestor / ".env").write_text(f"{NODE_NAMES}={NAMES}\n", encoding="utf-8")
    (tmp_path / "cwd").mkdir()
    (tmp_path / "driver.py").write_text(DRIVER, encoding="utf-8")
    return tmp_path


def import_config(tree: Path, switch: str | None) -> dict:
    """Import the planted `onedata_mcp.config` in a child with `switch` as the word and report
    what it did to the environment. The child's environment holds none of the three names, so
    the `.env` has something to add."""
    env = {k: v for k, v in os.environ.items() if k not in (NODE_NAMES, SWITCH, UPSTREAM_SWITCH)}
    if switch is not None:
        env[SWITCH] = switch
    done = subprocess.run(
        [sys.executable, str(tree / "driver.py"), str(tree / "anc" / "site")],
        env=env,
        cwd=tree / "cwd",
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    planted = Path(report["file"]).is_relative_to(tree / "anc" / "site")
    assert planted, "the planted copy is what was imported"
    return report


@pytest.mark.parametrize("switch", [None, "", "1", "true"], ids=["unset", "empty", "one", "true"])
def test_without_the_zero_word_the_env_file_in_an_ancestor_is_read_as_before(tree, switch):
    """Unset (or anything but 0) keeps today's behaviour: the `.env` found above the package
    sets what the environment left unset."""
    report = import_config(tree, switch)
    assert report["added"][NODE_NAMES] == NAMES
    assert report["changed"] == {} and report["removed"] == []


@pytest.mark.parametrize("switch", ["0", " 0 "], ids=["zero", "padded"])
def test_with_zero_no_env_file_is_read_and_the_environment_is_unchanged(tree, switch):
    """With ONEDATA_MCP_DOTENV=0 the same tree, the same `.env` above the package, changes
    nothing in os.environ."""
    report = import_config(tree, switch)
    assert report["added"] == {}
    assert report["changed"] == {} and report["removed"] == []


def test_the_word_is_read_from_the_process_environment_and_not_from_the_env_file(tree):
    """A `.env` that says ONEDATA_MCP_DOTENV=0 is read before anything could act on it, so it
    switches nothing off: the launch has to say it."""
    (tree / "anc" / ".env").write_text(f"{SWITCH}=0\n{NODE_NAMES}={NAMES}\n", encoding="utf-8")
    report = import_config(tree, None)
    assert report["added"] == {SWITCH: "0", NODE_NAMES: NAMES}
