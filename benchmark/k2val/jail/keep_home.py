"""`cli-jail.sh --keep-home <dir>`: copy a jailed CLI's session files out, never its login.

    python3 keep_home.py --cli codex|agy --home <jail home> --out <dir> --staged <staged home>

Only the session paths listed in SESSION_PATHS are copied, and no file whose name looks like a
login. Then every copied file is scanned for the values of the staged login files (and of the
login files as the CLI left them, in case it refreshed a token): one hit and the copy is deleted.
Only counts are printed. Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
from pathlib import Path

# Session files observed in jailed runs, relative to the jail's home. Anything else stays.
SESSION_PATHS = {
    "codex": [".codex/sessions", ".codex/log"],
    "agy": [
        ".gemini/antigravity-cli/conversations",
        ".gemini/antigravity-cli/brain",
        ".gemini/antigravity-cli/implicit",
        ".gemini/antigravity-cli/annotations",
        ".gemini/antigravity-cli/log",
        ".gemini/tmp",
    ],
}
LOGIN_NAMES = [
    "auth.json",
    "*oauth*",
    "*token*",
    "*cred*",
    "*secret*",
    "installation_id",
    "google_accounts.json",
    "settings.json",
    "*.pem",
    "*.key",
]
MIN_SECRET_LEN = 12


def looks_like_login(name: str) -> bool:
    return any(fnmatch.fnmatch(name.lower(), p) for p in LOGIN_NAMES)


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def secret_values(files: list[Path]) -> set[str]:
    """Every string in the login files long enough to be a secret (JSON values, or the raw
    content and its whitespace-separated words)."""
    out: set[str] = set()
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:  # a JSON login: its string values (not its key names, which sessions may share)
            candidates = _strings(json.loads(text))
        except ValueError:
            candidates = [*text.split(), text.strip()]
        out.update(c for c in candidates if len(c) >= MIN_SECRET_LEN)
    return out


def login_files(home: Path, staged: Path) -> list[Path]:
    rels = [p.relative_to(staged) for p in staged.rglob("*") if p.is_file()]
    return [staged / r for r in rels] + [home / r for r in rels if (home / r).is_file()]


def copy_sessions(cli: str, home: Path, out: Path) -> int:
    n = 0
    for rel in SESSION_PATHS[cli]:
        src = home / rel
        if not src.exists():
            continue
        for path in [src] if src.is_file() else sorted(src.rglob("*")):
            if not path.is_file() or path.is_symlink() or looks_like_login(path.name):
                continue
            dest = out / path.relative_to(home)
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(path, dest)
            n += 1
    return n


def scan(out: Path, secrets: set[str]) -> int:
    hits = 0
    needles = [s.encode() for s in secrets]
    for path in out.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            hits += sum(data.count(n) for n in needles)
    return hits


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="keep_home.py")
    p.add_argument("--cli", required=True, choices=sorted(SESSION_PATHS))
    p.add_argument("--home", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--staged", required=True, type=Path)
    a = p.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    secrets = secret_values(login_files(a.home, a.staged))
    if not secrets:
        print("keep_home: no login values to check against; nothing kept", file=sys.stderr)
        return 1
    n = copy_sessions(a.cli, a.home, a.out)
    hits = scan(a.out, secrets)
    if hits:
        shutil.rmtree(a.out, ignore_errors=True)
        print(
            f"keep_home: {hits} login values found in the session files; nothing kept",
            file=sys.stderr,
        )
        return 1
    os.chmod(a.out, 0o700)
    print(f"keep_home: {n} session files kept, 0 login values in them", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
