"""File ids as Onedata's encoder writes them, for tests.

A file id is the hex of a CDMI object id, as ctool's `file_id:build_objectid/2` builds it (ctool
`src/utils/file_id.erl`): an 8-byte header, then the payload. The header is a zero byte, the
3-byte enterprise number (0), a zero byte, the payload's length in one byte (`Length:8`, so modulo
256; the encoder refuses a payload over 320 bytes), and a CRC-16 of the whole id written with
that field zero, big-endian. The payload is `guid#<uuid>#<space id>` (`file_id:pack_guid/2`), or
`shareGuid#<uuid>#<space id>#<share id>` for a file seen through a share.

The CRC here is written out on its own (bit by bit, not the encoder's table), so that a test of
the tools' check does not reuse the code it tests; the reference ids in `test_file_ids.py` tie
both to what Onedata wrote.
"""

from __future__ import annotations


def crc16(data: bytes) -> int:
    """CRC-16 as the encoder computes it: polynomial 0x8005, reflected, initial value 0."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def object_id(
    payload: bytes,
    *,
    first: bytes = bytes(5),
    length: int | None = None,
    crc: int | None = None,
    lower: bool = False,
) -> str:
    """`payload` as a file id. By default every field is as the encoder writes it; `first` (the
    header's first five bytes), `length` and `crc` replace a field, and the CRC is computed over
    what is written unless `crc` is given. So a test can break one field and keep the rest
    consistent."""
    size = len(payload) % 256 if length is None else length
    head = first + bytes([size])
    check = crc16(head + b"\0\0" + payload) if crc is None else crc
    text = (head + check.to_bytes(2, "big") + payload).hex()
    return text if lower else text.upper()


def file_id(uuid: str, space_id: str, **fields: object) -> str:
    """The id of the file `uuid` in the space `space_id`."""
    return object_id(f"guid#{uuid}#{space_id}".encode(), **fields)  # type: ignore[arg-type]


def share_file_id(uuid: str, space_id: str, share_id: str, **fields: object) -> str:
    """The id of the same file as seen through the share `share_id`."""
    payload = f"shareGuid#{uuid}#{space_id}#{share_id}".encode()
    return object_id(payload, **fields)  # type: ignore[arg-type]


def term_payload(uuid: str, space_id: str, *, declared: tuple[int, int] | None = None) -> bytes:
    """The payload of an older id: the Erlang term `{guid, Uuid, SpaceId}` (external term format:
    a 3-tuple, the atom `guid`, two binaries, each a 4-byte length then its bytes). `declared`
    replaces the two lengths written."""
    sizes = declared or (len(uuid), len(space_id))

    def binary(text: str, size: int) -> bytes:
        return b"m" + size.to_bytes(4, "big") + text.encode()

    return b"\x83h\x03d\x00\x04guid" + binary(uuid, sizes[0]) + binary(space_id, sizes[1])


def term_file_id(uuid: str, space_id: str, **fields: object) -> str:
    """The older id of the same file (the swagger README's example has this shape)."""
    return object_id(term_payload(uuid, space_id), **fields)  # type: ignore[arg-type]
