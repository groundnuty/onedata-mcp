"""What counts as a whole file id (design/12-14, the guard on a mis-copied id).

A model copies a long hex id out of an earlier answer and sometimes drops, changes, doubles or
splices characters. `_file_id_parts` refuses, before any request, a value that is not an id as
Onedata's encoder writes it (ctool `file_id:build_objectid/2`, see `test/unit/object_ids.py`):
the reserved bytes zero, the payload at most 320 bytes, the length byte the payload's length
modulo 256, the CRC-16 the encoder computes, and a payload Onedata decodes. 25.0 writes the CRC
but does not check it, so without this check a header spliced onto another file's payload
selects that other file.
"""

from __future__ import annotations

import glob
import json
import os
import re

import pytest

from onedata_mcp.api.files import _crc16, _file_id_parts, _incomplete_file_id_message
from test.unit.object_ids import (
    file_id,
    object_id,
    share_file_id,
    term_file_id,
    term_payload,
)

SPACE_ID = "9JO5__9nL1b01YEvwnft1r20gwgwlkIp19ND_n8hgxs"
UUID = "0cb444f249735948f20eaf93a2dfe54bch3e33"

# the example of definitions/transfer/transfer_file_create_request.yaml in the 25.0 swagger
SWAGGER_EXAMPLE = (
    "00000000005CF4706775696423745F772D67686431633765446F4D76546D6F2D67575F3361737A7670486B"
    "477A7936587734507265584A7723394A4F355F5F396E4C31623031594576776E667431723230677767776C"
    "6B497031394E445F6E3868677873"
)

# the older id of the swagger README (an Erlang term {guid, Uuid, SpaceId}): its sixth header byte
# is its 145 payload bytes
README_TERM_ID = (
    "0000000000917733836803640004677569646D0000005254576F796132686E575530775331424F516B5534"
    "646B5A4963565678644545795746684F616E646B5157466A615531484D565A33516E42785A794D6A6E7A69"
    "434565634B2D494A4679387770456E6E3850776D0000002B6136552D334675766A6A6C465A7251576D7639"
    "584256304E454448594A615A4C4D6132725A566F704F746B"
)

# Ids a 25.0 Oneprovider returned in the validation runs (a file, the space's root, its trash
# and archives roots), and one id a model passed in their place: the first file's payload under
# the header of another file's id of the same length. 25.0 answered it with the first file.
RECORDED_FILE = (
    "00000000005232AE67756964233262643331326562363630643336393836303339393238613232316430306338"
    "636833616537233038643930623436616538313636346330653363326461376331643363376261636839393938"
)
RECORDED_SPLICE = "000000000052152D" + RECORDED_FILE[16:]
RECORDED_ROOTS = (
    "0000000000581734677569642373706163655F6665633765346663383237393932643936313964626237393139"
    "633563616333636861356663236665633765346663383237393932643936313964626237393139633563616333"
    "636861356663",
    "000000000058030B677569642374726173685F6661373461363165623762653530653463616465636238633564"
    "323733363764636865656566236661373461363165623762653530653463616465636238633564323733363764"
    "636865656566",
    "00000000006007FF677569642361726368697665735F726F6F745F37326133633461356538326531643065653432"
    "3137636638666565386230666563686232353823373261336334613565383265316430656534323137636638666565"
    "3862306665636862323538",
)

# A space id under a non-default `datastore_doc_key_length` of 60 (cluster-worker
# `datastore_key:new/0`: 60 random bytes in hex, `ch`, a 4-digit label): 126 characters.
LONG_SPACE_ID = "ab" * 60 + "ch" + "1f2e"


def whole_id(uuid: str = UUID, space_id: str = SPACE_ID, *, lower: bool = False) -> str:
    return file_id(uuid, space_id, lower=lower)


WHOLE = whole_id()


def test_the_crc_is_the_encoders() -> None:
    """CRC-16 with polynomial 0x8005, reflected, initial value 0: the source's own check value."""
    assert _crc16(b"123456789") == 0xBB3D


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
        (term_file_id(UUID, SPACE_ID), UUID, SPACE_ID),  # an older id, as an Erlang term
        (share_file_id(UUID, SPACE_ID, "shareid0123"), UUID, SPACE_ID),  # through a share
    ],
)
def test_a_whole_id_is_read_as_its_uuid_and_space(value: str, uuid: str, space_id: str) -> None:
    assert _file_id_parts(value) == (uuid, space_id)


@pytest.mark.parametrize("value", [SWAGGER_EXAMPLE, README_TERM_ID, RECORDED_FILE, *RECORDED_ROOTS])
def test_the_reference_and_recorded_ids_are_whole(value: str) -> None:
    """Ids Onedata wrote: the swagger's and its README's examples, and ids 25.0 returned."""
    assert _file_id_parts(value) is not None
    assert _file_id_parts(value.lower()) is not None


@pytest.mark.parametrize(
    "uuid",
    [
        f"trash_{LONG_SPACE_ID}",  # 132 characters, a 264-byte payload
        f"archives_root_{LONG_SPACE_ID}",  # 140 characters, a 272-byte payload
        f"space_{LONG_SPACE_ID}",
        # a hard link's uuid (op-worker `fslogic_file_id:gen_link_uuid/1`)
        f"link_{'0f' * 8}_file_{'cd' * 60}ch1f2e",
    ],
)
def test_long_ids_under_a_longer_key_length_are_whole(uuid: str) -> None:
    """No component has a length cap: the encoder bounds only the payload (320 bytes), and over
    255 bytes the length byte holds the length modulo 256."""
    value = whole_id(uuid, LONG_SPACE_ID)
    assert _file_id_parts(value) == (uuid, LONG_SPACE_ID)


def _flip(value: str, index: int, to: str) -> str:
    return value[:index] + to + value[index + 1 :]


def _other(digit: str) -> str:
    return "1" if digit != "1" else "2"


# where things are in WHOLE: 16 header digits, then "guid#" (10 digits), then the uuid
MARKER = 16
IN_UUID = 16 + 10 + 2 * 5  # inside the uuid, a letter or digit
IN_SPACE = len(WHOLE) - 6  # inside the space id
SAME_LENGTH_OTHER = whole_id("f" * len(UUID))  # another file's id: the same length byte
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
    # the same length, so only the CRC shows them
    "a changed digit in the uuid": _flip(WHOLE, IN_UUID + 1, _other(WHOLE[IN_UUID + 1])),
    "a changed digit in the space id": _flip(WHOLE, IN_SPACE, _other(WHOLE[IN_SPACE])),
    "a changed digit in the enterprise number": _flip(WHOLE, 3, "1"),
    "a changed digit in the CRC": _flip(WHOLE, 13, _other(WHOLE[13])),
    "a header spliced onto another file's payload": SAME_LENGTH_OTHER[:16] + WHOLE[16:],
    "the recorded splice": RECORDED_SPLICE,
    "the older id with a byte dropped from its end": term_file_id(UUID, SPACE_ID)[:-2],
    "the older id with a byte dropped from its first binary": term_file_id(UUID, SPACE_ID)[:40]
    + term_file_id(UUID, SPACE_ID)[42:],
    "the older id doubled": term_file_id(UUID, SPACE_ID) * 2,
    "the share id with a fourth part missing": share_file_id(UUID, SPACE_ID, "s1")[
        : 16 + 2 * len(f"shareGuid#{UUID}#{SPACE_ID}")
    ],
    "the share id cut inside its last part": share_file_id(UUID, SPACE_ID, "shareid0123")[:-12],
    "a share id with a changed marker": _flip(share_file_id(UUID, SPACE_ID, "s1"), 16, "5"),
    "a snippet of the swagger, cut short": (
        "000000000052326167756964233037613463613661663861343136633438633834336635633864356232"
    ),
    "empty": "",
    "a path": "/space/dir/file.dat",
    "a file name": "log_20200101.dat",
    "a space id": SPACE_ID,
    "hex that is not a file id": "ab" * 40,
}


@pytest.mark.parametrize("name", list(ALMOST))
def test_what_cannot_be_a_whole_id_is_refused(name: str) -> None:
    assert _file_id_parts(ALMOST[name]) is None, name


def test_the_splice_reaches_onedata_as_another_file_without_the_crc() -> None:
    """Why the CRC is checked: the splice's payload is a real file's, so 25.0 (which ignores the
    CRC on decode) would act on that file."""
    assert RECORDED_SPLICE != RECORDED_FILE and RECORDED_SPLICE[16:] == RECORDED_FILE[16:]
    assert _file_id_parts(RECORDED_FILE) is not None
    assert _file_id_parts(RECORDED_SPLICE) is None


# Each field broken on its own, the CRC computed over what is written: these show the check of
# that field, not the CRC's.
ONE_FIELD = {
    "the first reserved byte": object_id(b"guid#u#s", first=b"\x01\0\0\0\0"),
    "the second reserved byte": object_id(b"guid#u#s", first=b"\0\0\0\0\x01"),
    "a length byte one short": object_id(b"guid#u#s", length=7),
    "a share id's length byte": share_file_id(UUID, SPACE_ID, "s1", length=3),
    "a share id cut inside its last part": object_id(
        f"shareGuid#{UUID}#{SPACE_ID}#shareid0123".encode()[:-6],
        length=len(f"shareGuid#{UUID}#{SPACE_ID}#shareid0123"),
    ),
    "a payload over 320 bytes": whole_id("u" * 160, "s" * 156),  # 5 + 160 + 1 + 156 = 322
    "an older id whose second binary is longer than what is left": object_id(
        term_payload(UUID, SPACE_ID, declared=(len(UUID), len(SPACE_ID) + 3))
    ),
    "an older id whose first binary is longer than what is left": object_id(
        term_payload(UUID, SPACE_ID, declared=(500, len(SPACE_ID)))
    ),
    "an older id with bytes after its term": object_id(term_payload(UUID, SPACE_ID) + b"x"),
    "an empty space id": object_id(b"guid#u#"),
    "an empty payload": object_id(b""),
    "a third part": object_id(b"guid#u#s#x"),
}


@pytest.mark.parametrize("name", list(ONE_FIELD))
def test_each_field_is_checked_on_its_own(name: str) -> None:
    assert _file_id_parts(ONE_FIELD[name]) is None, name


def test_a_payload_of_320_bytes_is_whole_and_its_length_byte_is_modulo_256() -> None:
    value = whole_id("u" * 160, "s" * 154)  # 5 + 160 + 1 + 154 = 320
    assert value[10:12] == f"{320 % 256:02X}"
    assert _file_id_parts(value) == ("u" * 160, "s" * 154)
    assert _file_id_parts(object_id(bytes.fromhex(value[16:]), length=320 % 256 + 1)) is None


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


# --- the evidence store --------------------------------------------------------------------------
#
# With K2VAL_ARTEFACT_ROOT set to a validation run's artefact root, every id that a recorded tool
# call's result carries (each was written by a 25.0 Oneprovider) must be whole. Skipped without it:
# the store is not part of the repository.

_RECORDED_ID = re.compile(r"(?<![0-9A-Fa-f])0000000000[0-9A-Fa-f]{6,}(?![0-9A-Fa-f])")


def _recorded_result_ids(root: str) -> set[str]:
    found: set[str] = set()
    for path in glob.glob(os.path.join(root, "*", "*.jsonl")):
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                for call in json.loads(line).get("tool_calls") or []:
                    result = call.get("result")
                    text = result if isinstance(result, str) else json.dumps(result)
                    found.update(_RECORDED_ID.findall(text or ""))
    return found


def test_every_id_in_the_recorded_results_is_whole() -> None:
    root = os.environ.get("K2VAL_ARTEFACT_ROOT")
    if not root:
        pytest.skip("K2VAL_ARTEFACT_ROOT is not set")
    ids = _recorded_result_ids(root)
    assert ids, f"no ids found under {root}"
    refused = sorted(value for value in ids if _file_id_parts(value) is None)
    assert refused == [], f"{len(refused)} of {len(ids)} recorded ids refused"
