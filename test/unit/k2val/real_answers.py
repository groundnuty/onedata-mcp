"""Real final answers the validation panel's oracle scored FAIL although they were correct.

Each is the `final_answer` of a trial record the panel's first two runs scored FAIL, unchanged
but for stripped surrounding whitespace. They are regression cases for
`benchmark/k2val/oracles.py`: every one of them is a correct answer and must PASS.
"""

# ruff: noqa: E501

from __future__ import annotations

# V1: which of the six files are fully stored at the local site (the right answer names exactly two).
V1_REAL: list[tuple[str, str]] = [
    (
        "qwencoder-0",
        """Based on my analysis of the file distributions, I can now determine which files are fully stored at our vistula-dc site.

Looking at the distribution data for each file:

1. **log_20200101.dat**: Has 0 physical size at vistula-dc (physicalSize: 0, blocks: [])
2. **log_20200102.dat**: Has full physical size at vistula-dc (physicalSize: 262144, blocks: [[0, 262144]])
3. **log_20200103.dat**: Has 0 physical size at vistula-dc (physicalSize: 0, blocks: [])
4. **log_20200104.dat**: Has 0 physical size at vistula-dc (physicalSize: 0, blocks: [])
5. **log_20200105.dat**: Has full physical size at vistula-dc (physicalSize: 262144, blocks: [[0, 262144]])
6. **log_20200106.dat**: Has 0 physical size at vistula-dc (physicalSize: 0, blocks: [])

The files that are fully stored at vistula-dc (with non-zero physical size and actual blocks present) are:

**log_20200102.dat**
**log_20200105.dat**

These two files have their complete data stored at our vistula-dc site, while the other four files either don't have any data stored at our site or only have partial replication.""",
    ),
    (
        "sonnet-medium-0",
        """Two of the six files are fully stored at vistula-dc:

- log_20200102.dat
- log_20200105.dat

The other four (log_20200101.dat, log_20200103.dat, log_20200104.dat and log_20200106.dat) have a local replication rate of 0.0 at vistula-dc.""",
    ),
]

# V4: the files are complete at another provider and not at this site (the right answer is "not ready").
V4_REAL: list[tuple[str, str]] = [
    (
        "deepseek-2",
        """The check shows that the log files are **not ready** at my site (`vistula-dc`). Here's the situation:

- **Files**: There are 5 log files (for 1–5 January 2020, i.e., `log_20200101.dat` through `log_20200105.dat`), totaling about 1.25 MB.
- **Readiness**: **Not ready**.
- **Reason**: All 5 files are **complete at another provider** but are **not yet present at my site** (`vistula-dc`). None of the bytes are available locally (0 bytes local).

In short, the data is fully written and intact, but it resides at a different provider and has not been replicated/transferred to `vistula-dc`. If you need these files locally for analysis, I can use `ensure_readiness` to bring them over (waiting for the transfer to complete).""",
    ),
    (
        "haiku-medium-1",
        """No. The five log files for 1–5 January 2020 are complete, but they are not at your site yet. Your site holds 0 of the 1.3 MB involved.

- `log_20200101.dat` through `log_20200105.dat` are each complete at another provider.
- None has been transferred to `vistula-dc` yet, so they can't be read locally.

I haven't started a transfer. I can run `ensure_readiness` on this range to copy them to your site and hold them there. It will wait up to a deadline you set.""",
    ),
    (
        "gpt-luna-low-1",
        """No. All 5 files for 1–5 January 2020 are complete elsewhere, but none are currently stored/readable at `vistula-dc` (`0` of `1,310,720` bytes local). They have not yet been transferred to your site.""",
    ),
    (
        "gpt-luna-low-2",
        """No. The five log files for 1–5 January 2020 are complete elsewhere, but none are currently local/readable at `vistula-dc` (`0` of `1,310,720` bytes local). They have not yet been transferred to your site.""",
    ),
]

# V5: two files are still being written, three are at another provider (the right answer is "not ready", naming the writes).
V5_REAL: list[tuple[str, str]] = [
    (
        "deepseek-1",
        """The log files are **not** complete and readable at my site. Here's the breakdown:

- **2 files are currently being written** (incomplete):
  - `log_20200102.dat`
  - `log_20200104.dat`

- **3 files are complete but at another provider** (not yet at my site, `vistula-dc`):
  - `log_20200101.dat`
  - `log_20200103.dat`
  - `log_20200105.dat`

**Total:** 5 files (covering 1–5 January 2020), totaling ~1.25 MB. None of the bytes are currently available locally at my site (`bytes_local: 0`).

**Why not ready:** The two files being written are still incomplete because the owner is still writing them, so their data is not yet finalized. The other three are fully written but reside at another provider and haven't been transferred to `vistula-dc` yet.""",
    ),
    (
        "haiku-low-0",
        """No, they are not ready at your site (vistula-dc). Of the 5 files for 1–5 January 2020, none are readable locally yet, for two different reasons:

- **Still being written (2 files):** `log_20200102.dat` and `log_20200104.dat`. The owner is still writing them, so their contents are incomplete.
- **Complete elsewhere, not yet here (3 files):** `log_20200101.dat`, `log_20200103.dat` and `log_20200105.dat`. They are finished at another provider but haven't been replicated to yours.

The two being written will need to finish before they can be fully read. The three at the other site can be pulled in with `ensure_readiness`, which will copy them to your provider. I can start that now if you want.""",
    ),
    (
        "haiku-low-2",
        """No, they are not ready at your site. Of the 5 files for 1–5 January 2020, none are readable locally yet (0 of 1.31 MB present):

- **Being written (2 files):** `log_20200102.dat` and `log_20200104.dat`. The owner is still writing them, so their contents are incomplete.
- **At another site (3 files):** `log_20200101.dat`, `log_20200103.dat` and `log_20200105.dat`. These are complete elsewhere but haven't been transferred to `vistula-dc` yet.

The three complete files can be brought over with `ensure_readiness`. The two still being written will need to finish before they can be considered complete.""",
    ),
    (
        "gpt-luna-low-0",
        """No. At `vistula-dc`, none of the 5 files is currently local/readable (`bytes_local: 0`):

- 2 files are still being written: Jan 2 and Jan 4.
- 3 files are complete but only available at another provider: Jan 1, Jan 3, and Jan 5.

Therefore, the set is neither complete nor readable at `vistula-dc`.""",
    ),
    (
        "gpt-luna-low-1",
        """No. At `vistula-dc`, none of the 5 files are locally readable:

- 2 files are still being written, so their contents are incomplete: January 2 and 4.
- 3 files are complete but only available at another provider and have not yet transferred: January 1, 3, and 5.""",
    ),
    (
        "gpt-luna-low-2",
        """No. At `vistula-dc`, all 5 expected files are present in the date range, but none are locally readable yet (`bytes_local: 0`):

- 2 files are still being written: Jan 2 and Jan 4.
- 3 files are complete but only available at another provider: Jan 1, Jan 3, and Jan 5.""",
    ),
]
