"""What counts as a complete file id (design/12-14, the guard on a mis-copied id).

A model copies a long hex id out of an earlier answer and sometimes drops, changes or doubles
characters. `_file_id_parts` refuses what cannot be a whole 25.0 id before any request. The ids
here are synthetic, built in the shape the swagger shows: an 8-byte header whose sixth byte is
the payload's length, then the ASCII payload `guid#<uuid>#<space id>`, all in hex.
"""

from __future__ import annotations

import pytest

from onedata_mcp.api.files import _file_id_parts, _incomplete_file_id_message

SPACE_ID = "9JO5__9nL1b01YEvwnft1r20gwgwlkIp19ND_n8hgxs"
UUID = "0cb444f249735948f20eaf93a2dfe54bch3e33"

# the example of definitions/transfer/transfer_file_create_request.yaml in the 25.0 swagger
SWAGGER_EXAMPLE = (
    "00000000005CF4706775696423745F772D67686431633765446F4D76546D6F2D67575F3361737A7670486B"
    "477A7936587734507265584A7723394A4F355F5F396E4C31623031594576776E667431723230677767776C"
    "6B497031394E445F6E3868677873"
)


def whole_id(uuid: str = UUID, space_id: str = SPACE_ID, *, lower: bool = False) -> str:
    payload = f"guid#{uuid}#{space_id}".encode()
    text = (bytes([0, 0, 0, 0, 0, len(payload), 0x52, 0x73]) + payload).hex()
    return text if lower else text.upper()


WHOLE = whole_id()


@pytest.mark.parametrize(
    ("value", "uuid", "space_id"),
    [
        (WHOLE, UUID, SPACE_ID),
        (whole_id(lower=True), UUID, SPACE_ID),  # either case of hex
        (SWAGGER_EXAMPLE, "t_w-ghd1c7eDoMvTmo-gW_3aszvpHkGzy6Xw4PreXJw", SPACE_ID),
        # the roots Onedata itself names, which a model is given too
        (whole_id(f"trash_{SPACE_ID}"), f"trash_{SPACE_ID}", SPACE_ID),
        (whole_id(f"archives_root_{SPACE_ID}"), f"archives_root_{SPACE_ID}", SPACE_ID),
        (whole_id("a", "b"), "a", "b"),
    ],
)
def test_a_whole_id_is_read_as_its_uuid_and_space(value: str, uuid: str, space_id: str) -> None:
    assert _file_id_parts(value) == (uuid, space_id)


def _flip(value: str, index: int, to: str) -> str:
    return value[:index] + to + value[index + 1 :]


# where things are in WHOLE: 16 header digits, then "guid#" (10 digits), then the uuid
MARKER = 16
IN_UUID = 16 + 10 + 2 * 5  # inside the uuid, a letter or digit
ALMOST = {
    "one digit dropped from the end": WHOLE[:-1],
    "one digit dropped from the front": WHOLE[1:],
    "one digit dropped in the middle": WHOLE[:90] + WHOLE[91:],
    "three digits dropped in the middle": WHOLE[:90] + WHOLE[93:],
    "a whole byte dropped from the end (the space id is one character short)": WHOLE[:-2],
    "a whole byte dropped in the middle (the uuid is one character short)": WHOLE[:IN_UUID]
    + WHOLE[IN_UUID + 2 :],
    "a whole byte dropped from the header": WHOLE[2:],
    "a digit added": WHOLE[:90] + "a" + WHOLE[90:],
    "a byte added in the uuid": WHOLE[:IN_UUID] + "41" + WHOLE[IN_UUID:],
    "the id twice": WHOLE + WHOLE,
    "the id twice, the second without its header": WHOLE + WHOLE[16:],
    "the payload only": WHOLE[16:],
    "the header only": WHOLE[:16],
    "a character that is not hex": _flip(WHOLE, 60, "g"),
    "a trailing space": WHOLE + " ",
    "a changed digit in the marker": _flip(WHOLE, MARKER, "7"),  # `wuid#` for `guid#`
    "a changed digit in a separator": _flip(WHOLE, MARKER + 10 + 2 * len(UUID), "4"),  # no `#`
    "a header that says another length": _flip(WHOLE, 10, "F"),
    "empty": "",
    "a path": "/space/dir/file.dat",
    "a file name": "log_20200101.dat",
    "a space id": SPACE_ID,
    "hex that is not a file id": "ab" * 40,
}


@pytest.mark.parametrize("name", list(ALMOST))
def test_what_cannot_be_a_whole_id_is_refused(name: str) -> None:
    assert _file_id_parts(ALMOST[name]) is None, name


def test_what_the_check_cannot_see_passes_and_is_onedatas_to_refuse() -> None:
    """Said out loud, so nobody reads the check as proof of an exact id: a letter changed to
    another letter in the uuid, and a header spliced onto another file's payload, are whole."""
    changed = _flip(WHOLE, IN_UUID + 1, "5")  # one digit for another: still alphanumeric
    assert _file_id_parts(changed) is not None and changed != WHOLE
    other = whole_id("f" * len(UUID))  # same length, so the same header length byte
    spliced = WHOLE[:16] + other[16:]
    assert _file_id_parts(spliced) is not None


def test_a_payload_too_long_for_the_length_byte_is_not_refused_for_its_length() -> None:
    """The header cannot count past 255 payload bytes, so the byte is not compared then."""
    long_space = "s" * 128
    long_uuid = "u" * 128  # 5 + 128 + 1 + 128 = 262 bytes
    payload = f"guid#{long_uuid}#{long_space}".encode()
    value = (bytes([0, 0, 0, 0, 0, 7, 0, 0]) + payload).hex()
    assert len(payload) > 0xFF
    assert _file_id_parts(value) == (long_uuid, long_space)


def test_the_message_for_an_incomplete_id_says_what_to_do() -> None:
    text = _incomplete_file_id_message(WHOLE[:-3])
    assert text.startswith("'" + WHOLE[:80] + "'...")  # echoed cut short, as an id is long
    assert "is not a complete file id; copy it exactly from the earlier answer" in text
    assert "or give the path, /<space_name>/<path>." in text


def test_a_long_value_is_echoed_cut_short() -> None:
    text = _incomplete_file_id_message("a" * 300)
    assert "a" * 80 in text and "a" * 81 not in text and "..." in text


def test_a_relative_path_is_told_apart_from_a_bad_copy() -> None:
    text = _incomplete_file_id_message("space/dir/file.dat")
    assert text.startswith("Paths must start with '/'")
    assert "not a complete file id" not in text
