"""Secrets for the validation panel: read from files at run time, held masked, redacted at
every write.

A token or key is read from its file when it is needed (`read_secret_file`), held in a
`SecretStr` (whose `repr` and `str` show nothing), and registered with `MASKER`, which every
write boundary (artefact lines, the spend ledger, reports, scan records, printed lines, error
text) passes its text through. A secret is never logged, printed, persisted or put on a
command line.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

MASK = "***"
# A shorter value would corrupt ordinary text when redacted; real tokens are far longer.
MIN_SECRET_LENGTH = 6


class SecretFileError(RuntimeError):
    """A secret file is missing, unreadable or empty. The message never holds its content."""


class SecretStr:
    """A secret value that shows nothing when printed or logged. `reveal()` returns it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return f"SecretStr({MASK})"

    __str__ = __repr__

    def __bool__(self) -> bool:
        return bool(self._value)


class Masker:
    """Replaces every registered secret (and its JSON-escaped and URL-quoted forms) in text."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()

    def register(self, value: str) -> None:
        for piece in [value, *value.splitlines()]:
            piece = piece.strip()
            if len(piece) < MIN_SECRET_LENGTH:
                continue
            self._secrets.add(piece)
            self._secrets.add(json.dumps(piece)[1:-1])
            self._secrets.add(quote(piece, safe=""))

    def redact(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, MASK)
        return text

    def contains(self, text: str) -> bool:
        return any(secret in text for secret in self._secrets)

    def clear(self) -> None:
        self._secrets.clear()


MASKER = Masker()


def read_secret_file(path: str | Path) -> SecretStr:
    """The content of a token or key file, stripped. Registers it with `MASKER`."""
    file = Path(path).expanduser()
    try:
        value = file.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise SecretFileError(f"cannot read secret file {file} ({type(e).__name__})") from None
    if not value:
        raise SecretFileError(f"secret file {file} is empty")
    MASKER.register(value)
    return SecretStr(value)


def redact(text: str) -> str:
    return MASKER.redact(text)


def say(*parts: object, file=None) -> None:
    """`print` through the masker: the one way the panel prints a line."""
    import sys

    print(redact(" ".join(str(p) for p in parts)), file=file or sys.stdout, flush=True)
