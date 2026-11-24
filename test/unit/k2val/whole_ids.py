"""A fake federation's ids in the shape 25.0 gives (the tools refuse an id that cannot be whole).

`list_children`, `get_file_attributes` and `schedule_file_replication` refuse, before any request,
a file id that Onedata's encoder could not have written: the hex of an 8-byte header, with its
length byte and CRC, and the payload `guid#<uuid>#<space id>`
(`onedata_mcp.api.files._file_id_parts`, `test/unit/object_ids.py`). The scenario tests that hand
the model an id (FAi, LCi, SR1i) need ids written so, as the real federation's are.
"""

from __future__ import annotations

from test.unit.object_ids import object_id

SPACE = "spaceid0000000001"


def whole_id_of(fake_id: str) -> str:
    """`fake_id` (the fake federation's own id) as a whole 25.0 file id: the id is the uuid."""
    payload = f"guid#{fake_id}#{SPACE}".encode()
    return object_id(payload)
