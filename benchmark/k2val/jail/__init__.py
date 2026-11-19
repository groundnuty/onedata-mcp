"""The jail of the CLI legs (wave 2): one function, and no fallback.

The codex and agy CLIs send everything they see to OpenAI or Google, so they run ONLY inside
`cli-jail.sh` (this directory): bwrap with its own network namespace (slirp4netns, an nft egress
filter, no host loopback), a fresh home holding only the CLI's login files, a minimal env, an
empty work dir, and no host path visible. The MCP server stays outside on 127.0.0.1:<P>; the jail
relays 127.0.0.1:<P> inside to it through a UNIX socket. See `cli-jail.sh` and design/08.

The wrapper comes from `[cli.jail] wrapper` in `k2val.toml` (a path relative to the repository
root, or absolute) and is invoked as

    <wrapper> --cli codex|agy --workdir <dir> [--mcp-upstream 127.0.0.1:<P>]
              [--home-file <rel>=<file>]... -- <argv...>

Nothing in the CLI legs starts a process except through `jail_cmd`; with no wrapper set it
raises, so a CLI is never run unjailed.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
from collections.abc import Iterable
from pathlib import Path

from benchmark.k2val.config import JailConf

REPO_ROOT = Path(__file__).resolve().parents[3]
CLIS = ("codex", "agy")
_UPSTREAM = re.compile(r"127\.0\.0\.1:\d{1,5}")
MAX_JAIL_FILE_BYTES = 1 << 20  # 1 MiB: a jail-written file the host reads is never larger


class JailError(RuntimeError):
    """No jail is configured: the CLI legs refuse to run."""


class JailFileError(RuntimeError):
    """A file the jail wrote is not a plain small regular file: it is not read."""


class JailCleanupError(RuntimeError):
    """A directory the jail wrote into could not be removed."""


def _unlock_dirs(path: str) -> None:
    """u+rwx back on `path` and every directory below it, top-down, never through a symlink:
    the jail may have stripped them (chmod 0) so that removal fails. Files are left alone (an
    unlink needs no permission on the file)."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode):
        return
    os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
    with os.scandir(path) as entries:
        subdirs = [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    for d in subdirs:
        _unlock_dirs(d)


def remove_tree(path: Path | str) -> None:
    """Remove a directory the jail could write into (the trial's scratch, its work dir, a jail's
    staging dir left by a SIGKILL), whatever the jail did to its permissions. Raises
    JailCleanupError if anything is left: it may hold a copy of a CLI login."""
    import shutil

    p = str(path)
    if not os.path.lexists(p):
        return
    _unlock_dirs(p)
    shutil.rmtree(p, ignore_errors=True)
    if os.path.lexists(p):
        raise JailCleanupError(f"could not remove {p} (it may hold a copy of a CLI login)")


def read_jail_file(
    directory: Path | str, name: str, limit: int = MAX_JAIL_FILE_BYTES
) -> str | None:
    """Read `name` in `directory`, a file the jailed CLI could have written (or replaced), without
    trusting it. None if it is absent. Refused (JailFileError) unless it is a regular file with one
    link and at most `limit` bytes: a symlink (to a host file, /proc/self/environ, /dev/zero), a
    FIFO (which would hang the read), a device, a directory or a hard link is never followed or
    read. The file is opened relative to an O_NOFOLLOW directory fd, O_NOFOLLOW and O_NONBLOCK,
    and checked with fstat on that same fd."""
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"a plain file name is required, got {name!r}")
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:  # the jail made its work dir unreadable (chmod 0), or replaced it
        raise JailFileError(f"{directory}: not readable ({os.strerror(e.errno or 0)})") from None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as e:  # ELOOP: a symlink
            raise JailFileError(
                f"{name}: not a regular file ({os.strerror(e.errno or 0)})"
            ) from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise JailFileError(f"{name}: not a regular file")
            if st.st_nlink != 1:
                raise JailFileError(f"{name}: a hard link ({st.st_nlink} links)")
            if st.st_size > limit:
                raise JailFileError(f"{name}: larger than {limit} bytes")
            data = b""
            while len(data) <= limit and (chunk := os.read(fd, 65536)):
                data += chunk
            if len(data) > limit:
                raise JailFileError(f"{name}: larger than {limit} bytes")
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    return data.decode("utf-8", errors="replace")


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
    return [*cmd, "--", *argv]
