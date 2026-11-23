# Readiness tools: `check_readiness` and `ensure_readiness`

Status: spec for implementation (2026-11-19). Two new tools at maturity
`experimental`, origin `ours`. Superseded in part by their promotion records
(design/17, design/18, 2026-11-23): neither tool's description or answer names
the other; an unknown extent field is refused; one ensure call waits 150 s. They implement a *readiness guarantee*: the agent
declares the data its analysis needs, and the server says whether they are
ready, makes them ready, and holds them.

**Hard constraint: nothing that exists changes.** Every existing tool's name,
description, input schema and annotations stays byte-identical. The only edits
outside the new files are registration plus the classification and count
bookkeeping listed under "Touch points". A `tools/list` diff with
`ONEDATA_MCP_TOOLS` set to the list of the existing tools, base `8388f4c` against
this branch, must be empty.

## Definitions

- **Local provider.** The Oneprovider this server talks to
  (`ONEDATA_ONEPROVIDER_HOST`). It is the agent's site. Its id comes from
  `GET /api/v3/oneprovider/configuration`, field `providerId`; cache it for the
  process lifetime.
- **Size.** The size a file is judged at is the largest of the listing's `size`,
  every provider's `logicalSize` or `virtualSize` (25.0 may say either), and
  the largest block end (offset + length) at any readable provider: a stale,
  smaller size must not let a
  partial replica look complete. A size missing from every source makes the
  file `unchecked`: nothing can be judged covered. A listing `size` of exactly
  0, with no other size evidence and **every** provider entry readable, is an
  empty file: there is nothing to read, so a size-0 file is covered by any
  provider that lists it, and it is ready when the local provider lists it and
  writes are complete. One unreadable or malformed provider and the file is not
  confirmed empty (`unchecked`). A block end raises a size some source gave; it
  does not make one up. The largest size seen for a file is kept across polls
  and across overlapping dataset listings: never replaced by a smaller one.
- **Coverage.** A provider *covers* a file when the union of its blocks over
  all its storages covers `[0, size)`. Blocks are the `[offset, length]`
  pairs in `distributionPerProvider[pid].distributionPerStorageBackend[sid].blocks`
  of `GET /data/{id}/distribution`. A block counts only if it is a list or
  tuple of exactly two non-negative ints with a length above 0. A provider
  whose entry says `success: false`, has no storage map, or carries any other
  kind of block is unreadable for that file: its blocks are unknown.
- **Writes complete.** Some provider **other than the local one** covers the
  file. Exception: the file's `originProviderId` is the local provider and the
  local provider covers it. Local blocks are excluded on purpose: a read of an
  unwritten range through the local provider might materialise zero-filled
  local blocks. This is UNVERIFIED and gets a live test.
- **Ready (per file).** The local provider covers the file AND writes are
  complete. Never infer ready from a transfer's `replicationStatus: completed`
  or from a QoS `fulfilled` status. Both can precede the target-side bytes
  (onedata-mcp#17).
- **Extent ready.** Every resolved file is ready, and nothing in the extent
  failed to resolve.

## The `extent` argument (Declare)

The same argument for both tools. It is a list of 1–20 items:

```json
{"dataset": "my-dataset",
 "files": ["data_20180801.nc"],
 "date_ranges": [{"start": "2018-06-01", "end": "2018-08-31"}]}
```

- `dataset` (required): a space name (e.g. `my-dataset`), or an absolute
  directory path inside a space (`/my-dataset/sub/dir`). Resolve a space name
  with the existing `resolve_space_id_or_name` / path helpers. Do not
  reimplement them. A name with a `/` but no leading one (`my-dataset/sub`) is
  forgiven: when its first segment is an existing space name it means
  `/my-dataset/sub`. If the first segment is not a space, or the directory does
  not exist, the item is unresolved and says how to fix it.
- `files` (optional): file names (basenames) or absolute paths inside the
  dataset. At most 20,000 per item.
- Declared names and paths are matched **exactly**: no trimming, no case
  folding, no normalising of slashes. A name that does not match is
  unresolved.
- `date_ranges` (optional): inclusive `YYYY-MM-DD` bounds. They select the files
  whose name carries a date in any range. **The date in a name** is the first
  maximal run of exactly 8 digits in the basename (regex `(?<!\d)\d{8}(?!\d)`)
  that parses as a valid `YYYYMMDD`. That handles `data_20180801.nc` and
  `20180801_example_product_….nc`. A file with no such date never matches a
  date range.
- With neither `files` nor `date_ranges`, the item is the whole dataset.
  With both, it is the union.
- Resolution lists the dataset recursively with the existing
  `list_files_recursively` helper (`GET /data/{id}/files`). Page with
  `limit=1000` and the returned token. Request the attributes `fileId`, `path`,
  `name`, `type`, `size`, `isFullyReplicatedLocally`, `localReplicationRate`
  and `originProviderId` (the origin exception below needs it). Keep
  `type == REG` only.
- A dataset holds up to ~15,400 files (16 pages), and an extent has ~1,500–2,000
  files in the expected use. Stop paging and refuse, with a clear error, as soon as
  more than 20,000 regular files have been seen in a dataset listing, or an
  extent resolves to more than 20,000 files. `date_ranges` takes at most 400
  ranges per item (both caps are in the input schema).
- **Unresolved items** are reported, never silently dropped: a dataset not
  found (also one whose listing answers enoent or 404), a named file not
  present, a date range that matched nothing, an empty dataset, every
  directory in a page's `inaccessiblePaths`, and a listing that is incomplete
  (a page that is not the last but gives no continuation token, a token that
  does not advance, an entry without `type`, `path` or (for a regular file)
  `fileId`, a `type` outside REG, DIR and SYMLNK, a listing body that is not
  an object, and a plain 404 or notFound from the root lookup, which reads as
  "no such directory"). Any unresolved item
  makes the extent not ready. Each unresolved text is one plain line that says
  what to give instead, never what to do with the data, e.g. "dataset
  'my-dataset/sub' not found: give a space name (e.g. 'my-dataset') or an
  absolute directory path ('/my-dataset/sub')."

## `check_readiness(extent)` (read-only)

1. Resolve the extent.
2. Fast path: a file whose `isFullyReplicatedLocally` is false cannot be ready.
   Classify it. For a file whose flag is true, confirm with its distribution
   (local coverage plus writes complete).
3. Classify every not-ready file by **the first matching reason**:
   1. `unreachable`: no online provider other than the local one covers it,
      and some offline or erroring provider has blocks of it. A provider is
      offline if Onezone `GET /api/v3/onezone/providers/{pid}` says
      `"online": false`, or the distribution entry for it has
      `"success": false`. BEST EFFORT, UNVERIFIED: if the Onezone lookup fails,
      treat the provider as online. Never fail the tool because of this lookup.
   2. `being_written`: no provider other than the local one covers it (writes
      are not complete).
   3. `in_transfer`: writes are complete and something is moving it to the
      local provider. That is either an ongoing transfer of this file (or of
      an ancestor directory) whose replicating provider is the local one, or
      a placement rule this server created for the file that is not yet
      satisfied. Ongoing transfers come from
      `GET /spaces/{sid}/transfers?state=ongoing` plus `GET /transfers/{tid}`,
      using the existing helpers, once per space per call, capped at 50
      transfers.
   4. `at_other_site`: writes are complete and nothing is moving it.
4. Answer, compact and never a per-file dump:

```json
{"ready": false,
 "files": 1544, "bytes": 1804000000, "bytes_local": 1790000000,
 "not_ready": {"being_written": {"files": 14, "examples": ["/my-dataset/data_20180818.nc", "…"]}},
 "unresolved": [],
 "advice": "Not ready: 14 files are still being written at their owner, so their bytes are incomplete. ensure_readiness can wait for them and place them at your provider."}
```

- At most 3 `examples` per reason.
- `advice` is one or two plain sentences naming the dominant reason and how
  the other tool is used. It states what the answer means and which function
  does what. It never advises the agent on its analysis: no "do not compute on
  them yet" or similar. The existing tools carry no such advice.
- When ready: `{"ready": true, "files": …, "bytes": …, "advice": "Ready: all
  declared files are complete and readable at your provider now."}`.

Concurrency: at most 16 REST calls in flight, for the whole call (distribution,
provider, transfer and rule reads share the limit). Budget: the call must end
within 60 s. Every read is cut off by what is left of the budget; Onezone
provider lookups are made once per provider per call, concurrently, and are
skipped (provider taken as online) once the budget is gone. If the budget runs
out, answer not ready and list the unchecked files as an `unchecked` count. A
file whose reads all failed while the rest were skipped at the deadline is
`unchecked`, not an error; only when every file was attempted and every read
failed is the first error raised. The read of the local provider's id
(`/configuration`) is cut off by the budget too: on a timeout the answer is not
ready, "provider configuration unavailable". A name or a date range declared
twice in an item is looked for once.

A transfer counts as moving a file here only while its state (`transferStatus`)
is `scheduled`, `enqueued`, `replicating` or `active`. Failed, cancelled,
completed, skipped and aborting transfers do not count.

## `ensure_readiness(extent, deadline_s)` (write: places and holds data)

> **Since 2026-11-23** (design/18, the operator's decisions): one call waits at
> most 150 s by default and ends within 170 s (`ENSURE_GRACE_S` 20 s), under a
> common MCP client's ~180 s; the env may still raise the wait to 240 s. The
> answer's `held` also lists the ids of the holds (`qos_requirement_ids`), and
> neither tool's description or answer names the other tool. The text below is
> the original spec.

`deadline_s` is an int, 1–86,400: how long the agent is willing to wait in
total. **One call waits at most `min(deadline_s, MAX_WAIT_S)`, counted from the
call's start**: resolving and placing use up part of it.
`MAX_WAIT_S = 240` by default (env `ONEDATA_MCP_ENSURE_MAX_WAIT_S`), and never
more than 240 whatever the env says. The harness that calls the tools bounds one call
at 300 s and passes no extra env, so the default must be safe. The whole call,
including placement and the answer, must end within `MAX_WAIT_S + 30` s.

1. Resolve and classify, as Check does.
2. **Hold and place.** For every resolved file, ready or not, without a
   placement rule confirmed to exist: add a QoS requirement
   `POST /qos_requirements` with `{"fileId": id, "expression": "providerId=<local_id> & anyStorage", "replicasNum": 1}`.
   - Use the existing qos api helper.
   - The fixed expression form is the tag that marks this tool's rules. Expression
     validity is UNVERIFIED; if Onedata rejects it, fall back to
     `providerId=<local_id>` and record which form is in use.
   - Remember `file_id -> qos_id` in a process-level dict. This is a cache: if
     it is lost, a second rule is harmless. A remembered rule is not taken on
     trust: before it is counted as held, read it back with
     `GET /qos_requirements/{id}` (concurrently, within the 16-call limit). If it
     is gone (an agent with `remove_qos_requirement` may have deleted it), or
     cannot be read, create it again. A rule is confirmed only by a 200 answer
     with a JSON object whose `qosRequirementId` is the id or whose `fileId` is
     the file; a redirect, an HTML page or any other shape confirms nothing. `held` counts only rules confirmed or
     created in this call; a file that is neither (a failed creation, or out of
     time) is in `hold_failed`.
   - Concurrency: at most 8 in flight.
   - The rule makes Onedata replicate the file to the local provider from
     wherever it can (the system chooses the source), and protects the replica
     from eviction. That protection is the Hold. UNVERIFIED: whether the rule
     keeps up with a file still being written, and whether eviction is refused.
   - Keep one hook, `_nudge(file)`, called on each poll for a file that has
     writes complete, is not local, and whose rule reports fulfilled. For now
     it is a no-op returning False. If the live test shows QoS does not
     re-sync blocks written after the rule was added, it will schedule a
     replication with the existing `create_transfer` helper.
3. **Wait.** Poll every 5 s until every file is ready or the wait runs out.
   - Re-check only the files not yet confirmed ready. Use per-file attributes
     (`isFullyReplicatedLocally`, `size`) with the existing attributes helper.
   - Confirm candidates with their distribution (writes complete).
   - **Final pass.** A confirmation during the wait is not final. Before the
     answer, every file confirmed ready is read again (attributes only:
     `isFullyReplicatedLocally` must be true and `size` must not exceed the
     size it was confirmed at; a bigger file is judged at the bigger size). A
     file that fails the pass, or whose read fails, goes back to not ready and
     is classified from a fresh distribution. A final `isFullyReplicatedLocally`
     of false is final: no assessment may make the file ready again. Any read
     that fails in the final pass leaves its file `unchecked`, and the final
     pass never raises. Every rule counted as held is read again too, and `held`
     counts only the rules confirmed there. Check's single pass already judges
     every file at the time of the answer.
4. **Answer.** Check's shape, plus three fields:
   - `"waited_s"`;
   - `"held": {"files": N, "rule": "<expression>"}`;
   - `"hold_failed": {"files": K, "examples": [...]}` if any rule creation
     failed.

   `advice` follows the same rule as in Check (meaning and function use, no
   advice on the analysis). When ready it is "Ready: all declared files are
   complete and readable at your provider now, and held there." If not ready,
   it names the reason and what the function does next, for example "Not ready
   after 240 s: 14 files are still being written at their owner. Call
   ensure_readiness again to keep waiting." (the number is `waited_s`):
   - `being_written` / `in_transfer`: "Call ensure_readiness again to keep
     waiting";
   - `unreachable`: "the only copies are at a provider that is down";
   - unresolved items: fix the extent.

## Tool descriptions (model-facing; keep short and concrete)

`check_readiness`: "Check whether the data your analysis needs are ready to
read at your provider: complete there, with all writes finished. Describe the
data as `extent`: for each dataset (a space name such as 'my-dataset'),
optionally the files or date ranges you need; dates are read from the YYYYMMDD
in each file name. The answer is ready, or not ready and why: being_written (the
owner is still writing them, so their bytes are incomplete), at_other_site
(complete at another provider, not yet at yours), in_transfer (on their way to
your provider), or unreachable (only at a provider that is down). Read-only."

`ensure_readiness`: "Make the data your analysis needs ready at your provider,
and keep them there for your run. Takes the same `extent` as check_readiness,
plus `deadline_s`, how long you can wait. The system chooses where to copy
from and waits for unfinished writes and transfers. The answer is ready (the
data are complete and readable at your provider now, and held there), or not
ready with the reason. One call waits at most about 4 minutes; if the answer is
not ready because data are being written or in transfer, call it again."

Field descriptions follow the same style. Keep the `extent` item model small:
`dataset`, `files`, `date_ranges` with `start`/`end`.

## Touch points

- New: `onedata_mcp/api/readiness.py`, the logic. Pure helpers are unit-tested
  on their own: `name_date`, `covers`, `writes_complete`, `classify`.
- New: `onedata_mcp/modules/readiness.py`, the tool registration (pydantic
  models, `ToolAnnotations`: check `readOnlyHint=True`, ensure
  `destructiveHint=False`).
- `onedata_mcp/main.py`: `readiness.register_module(mcp)`. No other change.
- `onedata_mcp/tool_maturity.py`: add both names to `EXPERIMENTAL`. They are
  not in `UPSTREAM`.
- `onedata_mcp/token_policy.py`: add `ensure_readiness` to `WRITE_TOOL_NAMES`
  (it creates QoS rules, like `add_file_qos_requirement`).
- Update the tests that pin the registered tool count or set, and the
  README/USAGE tool counts plus one short section.
- Do not touch any other tool's code, docstring or schema.

## Tests

- **Unit, against fakes of production's shape.** Use pytest-httpx. Response
  bodies follow Onedata 25.0 exactly: reuse the shapes already in
  `test/unit/api/` and the swagger at `~/repos/onedata/oneprovider-swagger`
  (`definitions/data/recursive_file_list.yaml`, `data_distribution*`, QoS,
  transfers). Cover:
  - S0: complete at the owner, nothing local → `at_other_site`;
  - S2: owner partial, preallocated at the final size → `being_written`;
  - local partial with a pending rule → `in_transfer`;
  - owner down → `unreachable`;
  - ready;
  - local blocks full but no other provider full → NOT ready (`being_written`);
  - unresolved dataset, file and date range;
  - date parsing for both name forms;
  - the 20,000-file refusal;
  - ensure: places one rule per file, waits, turns ready when the fake flips,
    returns not ready at the wait cap with the right advice, falls back to the
    plain expression on a 400, reports `hold_failed`.
- **Gated live test**, `test/live/test_readiness_live.py`. It is skipped
  unless `ONEDATA_MCP_LIVE_READINESS=1`. It **refuses to run** unless the
  provider host is `127.0.0.1`, `localhost` or ends in `.datahub.test` (the
  local Compose federation). Never run it against production.
- **Manifest diff**, `scripts/tools_manifest.py`. It builds the server
  in-process with a given `ONEDATA_MCP_TOOLS`, dumps `tools/list` (name,
  description, inputSchema, annotations) as sorted JSON, and runs on two
  checkouts. Diff them: with the list of the existing tools it must be empty; with
  that list plus the two new tools it must show only the two additions.
