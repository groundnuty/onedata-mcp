"""The jail of the CLI legs (wave 2): one function, and no fallback.

The codex and agy CLIs send everything they see to OpenAI or Google, so they run ONLY inside
`cli-jail.sh` (this directory): bwrap with its own network namespace (slirp4netns, an nft egress
filter, no host loopback), a fresh home holding only the CLI's login files, a minimal env, an
empty work dir, and no host path visible. The MCP server stays outside on 127.0.0.1:<P>; the jail
relays 127.0.0.1:<P> inside to it through a UNIX socket. See `cli-jail.sh` and design/08.

The wrapper comes from `[cli.jail] wrapper` in `k2val.toml` (a path relative to the repository
root, or absolute) and is invoked as

    <wrapper> --cli codex|agy --workdir <dir> [--mcp-upstream 127.0.0.1:<P>]
              [--home-file <rel>=<file>]... [--keep-home <dir>] -- <argv...>

Nothing in the CLI legs starts a process except through `jail_cmd`; with no wrapper set it
raises, so a CLI is never run unjailed.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from pathlib import Path

from benchmark.k2val.config import JailConf

REPO_ROOT = Path(__file__).resolve().parents[3]
CLIS = ("codex", "agy")
_UPSTREAM = re.compile(r"127\.0\.0\.1:\d{1,5}")


class JailError(RuntimeError):
    """No jail is configured: the CLI legs refuse to run."""


_JAIL = JailConf()


def configure_jail(jail: JailConf) -> None:
    global _JAIL
    _JAIL = jail


def jail_available(jail: JailConf | None = None) -> bool:
    return bool((jail or _JAIL).wrapper.strip())


def wrapper_argv(conf: JailConf) -> list[str]:
    """The wrapper's command words; a relative program path is taken from the repository root."""
    words = shlex.split(conf.wrapper)
    program = Path(words[0]).expanduser()
    if not program.is_absolute():
        program = REPO_ROOT / program
    return [str(program), *words[1:]]


def jail_cmd(
    argv: list[str],
    workdir: str,
    jail: JailConf | None = None,
    *,
    cli: str,
    mcp_upstream: str | None = None,
    home_files: Iterable[tuple[str, str]] = (),
    keep_home: str | None = None,
) -> list[str]:
    """`argv` wrapped to run in the jail with `workdir` (empty) as /work. `mcp_upstream`
    (`127.0.0.1:<P>`) is the MCP server the jail relays to; `home_files` are (path in the fresh
    home, host file) pairs the jail copies in."""
    conf = jail or _JAIL
    if not conf.wrapper.strip():
        raise JailError(
            "no jail: set [cli.jail] wrapper in k2val.toml. The CLI legs send everything they "
            "see to a vendor and never run unjailed."
        )
    if cli not in CLIS:
        raise ValueError(f"cli must be one of {CLIS}, got {cli!r}")
    if mcp_upstream is not None and not _UPSTREAM.fullmatch(mcp_upstream):
        raise ValueError(f"mcp_upstream must be 127.0.0.1:<port>, got {mcp_upstream!r}")
    if not argv:
        raise ValueError("no CLI command")
    cmd = [*wrapper_argv(conf), "--cli", cli, "--workdir", workdir]
    if mcp_upstream:
        cmd += ["--mcp-upstream", mcp_upstream]
    for rel, src in home_files:
        cmd += ["--home-file", f"{rel}={src}"]
    if keep_home:
        cmd += ["--keep-home", keep_home]
    return [*cmd, "--", *argv]
