"""The jail of the CLI legs (wave 2): one function, and no fallback.

The codex and agy CLIs send everything they see to OpenAI or Google, so they run ONLY inside
the coordinator's jail (bwrap, a fresh empty home, a minimal env, an empty work dir, and no `/mnt`,
`/Users`, repo, token or key paths visible). The jail is built elsewhere; this module only
puts the wrapper in front of the command, from `[cli.jail] wrapper` in `k2val.toml`.

The wrapper is invoked as `<wrapper> <workdir_flag> <workdir> -- <argv...>`. That calling
convention is an assumption until the wrapper exists: change it here, in one place.

Nothing in the CLI legs starts a process except through `jail_cmd`; with no wrapper set it
raises, so a CLI is never run unjailed.
"""

from __future__ import annotations

import shlex

from benchmark.k2val.config import JailConf


class JailError(RuntimeError):
    """No jail is configured: the CLI legs refuse to run."""


_JAIL = JailConf()


def configure_jail(jail: JailConf) -> None:
    global _JAIL
    _JAIL = jail


def jail_available(jail: JailConf | None = None) -> bool:
    return bool((jail or _JAIL).wrapper.strip())


def jail_cmd(argv: list[str], workdir: str, jail: JailConf | None = None) -> list[str]:
    """`argv` wrapped to run in the jail with `workdir` as its (empty) working directory."""
    conf = jail or _JAIL
    if not conf.wrapper.strip():
        raise JailError(
            "no jail: set [cli.jail] wrapper in k2val.toml. The CLI legs send everything they "
            "see to a vendor and never run unjailed."
        )
    return [*shlex.split(conf.wrapper), conf.workdir_flag, workdir, "--", *argv]
