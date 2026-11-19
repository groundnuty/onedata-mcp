# Freeze record: the readiness tools

Frozen on 2026-11-19 at **`4791e83cedcbe5155093aeec3fce65190c5ec122`** (branch `feat/readiness-tools`). The two tools are `check_readiness` and `ensure_readiness`, both maturity `experimental`. Their spec is [`07-readiness-tools.md`](07-readiness-tools.md); the validation panel is described in `08-readiness-validation-panel.md` on branch `feat/readiness-panel`.

## What was frozen

- **Base** is `1e6b9cd`, branch `fix/k1-descriptions`. It is `8388f4c` plus two commits:
  - wording-only fixes to six existing tool descriptions;
  - a clear path hint in the no-roots error.
- **On top of the base:**
  - `fefbdb9` adds the two tools, `scripts/tools_manifest.py` and their tests.
  - `7ee5d8f` is lint-only, in two benchmark/test files.
  - `de2db24` resolves dataset spaces from the provider's own list.
  - `4791e83` accepts Onedata 25.0's last listing page.
- **No existing tool's name, description, input schema or annotations differ from the base.**
- 627 unit tests pass at the frozen commit.

## tools/list at the frozen commit

Both listings were produced by `scripts/tools_manifest.py`, with `ONEDATA_MCP_TOOLS` set to the selection; the output is sorted JSON of name, description, inputSchema and annotations.

| Selection | Tools | sha256 of the listing | File |
|---|---|---|---|
| existing 11: `add_file_qos_requirement, get_file_attributes, get_file_distribution, get_file_metadata, get_file_qos_summary, get_qos_requirement, get_transfer, list_space_providers, list_space_transfers, remove_qos_requirement, schedule_file_replication` | 11 | `4c8ef3f22fee7805e7c7ef72b4258a6a3bc4ee2f95ef1dafb7d0edd8ebf61b66` (byte-identical to the base `1e6b9cd`) | [`tools-list-existing-selection.json`](09-readiness-freeze/tools-list-existing-selection.json) |
| the same 11 + `check_readiness, ensure_readiness` | 13 | `680d5e2f56f75b90c30f2fcba4b08eb0e969573fd8980e9b35a26de3f033b064` (0 lines removed against the 11; only the two tools added) | [`tools-list-with-readiness.json`](09-readiness-freeze/tools-list-with-readiness.json) |

## The two descriptions, verbatim

### `check_readiness`

> Check whether the data your analysis needs are ready to read at your provider: complete there, with all writes finished. Describe the data as `extent`: for each dataset (a space name such as 'my-dataset'), optionally the files or date ranges you need; dates are read from the YYYYMMDD in each file name. The answer is ready, or not ready and why: being_written (the owner is still writing them, so their bytes are incomplete), at_other_site (complete at another provider, not yet at yours), in_transfer (on their way to your provider), or unreachable (only at a provider that is down). Read-only.

### `ensure_readiness`

> Make the data your analysis needs ready at your provider, and keep them there for your run. Takes the same `extent` as check_readiness, plus `deadline_s`, how long you can wait. The system chooses where to copy from and waits for unfinished writes and transfers. The answer is ready (the data are complete and readable at your provider now, and held there), or not ready with the reason. One call waits at most about 4 minutes; if the answer is not ready because data are being written or in transfer, call it again.

## Live checks (Onedata 25.0, a local Compose federation, scratch space)

One 256 KiB file was written whole at the owner provider. One 1 MiB file was preallocated by an empty write at its final size, then filled in three parts 25 s apart.

| Check | Result |
|---|---|
| Size field in `/distribution` | `virtualSize` (no `logicalSize`); both are read. |
| `check_readiness` | Not ready: `at_other_site` for the remote file, `being_written` for the arriving one. |
| A read of an unwritten range through the local provider | Zeros (HTTP 206), and **no local blocks created**, so coverage cannot be faked. |
| Placement rule `providerId=<local> & anyStorage` | Accepted. Local blocks grew with each written part, so the rule keeps up with the writer. |
| `ensure_readiness` during writes | Not ready (`being_written`) after its 40 s wait. |
| `ensure_readiness` after the last part | Ready and held, 5 s after the writes completed; local content equals the written bytes. |
| Hold (an eviction of the local replica by an administrator) | Refused under the rule: 0 of 2 files evicted. Without the rule, 1 of 1 was evicted. |

Two bugs only the live run could show were fixed before the freeze. Onedata 25.0's last listing page has no `isLast`, only `nextPageToken: null`. Space names had been resolved through Onezone, which fails with tokens valid only at the provider.

## Code reviews (independent, jailed; two rounds)

- **Round 1: 14 findings.** 12 were fixed, mostly ways to answer ready wrongly in corner cases:
  - stale or missing sizes;
  - inaccessible subtrees;
  - a confirmation going stale;
  - name normalisation;
  - incomplete pages;
  - malformed blocks;
  - a hold claimed for a deleted rule;
  - time budgets;
  - caps;
  - failed transfers.
- **Round 2 (delta): 16 findings.** 14 were fixed, all false-ready or false-held edge cases:
  - size evidence (block ends, `virtualSize`, the maximum across polls);
  - final-pass rereads;
  - hold reconfirmation;
  - listing robustness;
  - bounded configuration reads.
- **Recorded, not fixed (4):**
  - the existing space resolver's fan-out (existing code, unchanged);
  - `unreachable` masked by a partial online copy (best effort by design);
  - a stale zero size with no other evidence;
  - an owner rewriting a file after confirmation.

## Validation panel (the PPAM protocol in small)

- **Setup:**
  - synthetic files;
  - neutral prompts naming no tool;
  - a two-axis oracle;
  - one scratch space per config;
  - manual three-layer classification of every failure.
- **Control (Sonnet 5.5 at medium effort):** 7/7 at K=1.
- **Wave 1:** 168 trials, 8 configs × 7 scenarios × K=3:
  - GLM-5.3-Flash;
  - Qwen3.6-35B-A3B;
  - Qwen3-Coder-30B-A3B;
  - DeepSeek-V4-Flash-0731;
  - Haiku 5.5 at low, medium and high effort;
  - the Sonnet control.
- **Results:**
  - The oracle recorded 160 PASS and 8 FAIL. All 8 FAILs are **harness false negatives** of the answer parser; every one of those answers was correct.
  - Readiness violations: **0**. Premature ready or done claims: **0**. Readiness tool errors: **0**.
  - Every config used the tools from their descriptions alone:
    - it told `being_written` from `at_other_site`;
    - it called `ensure_readiness` again on "not ready";
    - it reported ready only after the tool did.
  - Recorded only: one model twice passed an existing tool's list argument as a JSON string. It was rejected by the schema, and the model recovered.
- **Changes made to the readiness tools because of panel trials: none.**
