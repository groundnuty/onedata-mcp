# Promotion record: `list_children`

Status: **gate 4 passed** (updated 2026-11-24). Gates 1 to 3 are done: the live test passed against Onedata 25.0 (2026-11-21, 10 passed). Gate 4 ran 2026-11-22 (results below); by the operator's decision F3 was fixed (PR #46), and LCp and LCi were re-run at K=8 on the six-config panel on 2026-11-23 (run `rerun-k8`, "Re-run after the 2026-11-23 decisions", below). The re-run leaves no tool-attributable finding open.

The short commit ids below are **branch commits** on `feat/promote-file-tools`. A squash merge leaves them unreachable from `master`. **The anchor is the surface hash.**

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md). The scenarios are in [`benchmark/k2val/file_scenarios.py`](../benchmark/k2val/file_scenarios.py), each with its brief, right answer and rules. The model for this record is [`11-promotion-remove-qos-requirement.md`](11-promotion-remove-qos-requirement.md); [`13`](13-promotion-get-file-attributes.md) describes the harness shared by the three file tools.

## The tool

- **REST call:** `GET /data/{id}/children` with a JSON body (`limit`, `offset` or `token`, `attributes`; swagger `list_children`, oneprovider-swagger tag `25.0`, commit `39da981`, `paths/data/id/children.yaml` and `definitions/data/get_directory_children_request.yaml`). A path is first resolved with `POST /lookup-file-id/{path}`. The swagger lists 200, 400, 403, 404 and 500, with the generic `Error` body.
- **Input:** `parent_id_or_path`; `attributes`, an optional list, **now capped at 64**; `limit`, 1 to 100 (default 10; the API allows 1000); `offset`, **now 0 to 500** (the API allows -500 to 500, negative only with an `index` the tool does not expose); `token`.
- **Read-only:** annotated `readOnlyHint: true`; not in `token_policy.WRITE_TOOL_NAMES`.
- **Code:** `onedata_mcp/modules/files.py` (surface) and `onedata_mcp/api/files.py` (call and errors).
- **The only tool that lists directories.** `list_files_recursively` (stable) returns regular files only, so it cannot show an empty directory.

## The surface being promoted

| Selection | Before (master `4047a56`) | Promoted surface (since branch commit `a2f62e8`) |
|---|---|---|
| `list_children` alone | `74a4f7eae2d4f2b241c9dc67d1fc9429b148b3aefb3ab9ad93d2e45d15a337af` | `726d5e587763b1e469ccf1932ae62c9be6a871a9c2f40a21113c2f215664aafa` |
| FILE_TOOLS (6 tools; the LC and CD scenarios run on it) | `43dd847ef84ddcd542278d3705a3b6e2affa3e769601545bc067da09fec6d114` | `c030429d0e04de218f50a1bda935d17e84026c8c24ea1a61fd6d91b0bac31922` (with design/13's and design/15's surfaces; it was `3ace77c4…` before design/15's review change) |

FILE_TOOLS is `create_directory, create_file, download_file, get_file_attributes, list_children, list_files_recursively`.

- `a2f62e8` is the last commit that changes this tool's surface. The external review's fixes (`3558556`, `49370ff`, `40bd036`) change errors and input handling only; the hash above is checked unchanged after them.
- The `tools/list` diff. The name and annotations are unchanged; the input schema gains `maxItems: 64` on `attributes` and `minimum: 0`, `maximum: 500` on `offset`:
  - description: `List children (files and directories) of a given file id or path.` → `List the entries of a directory (its files and subdirectories), given by path or file id.` + `Returns one page: children, the requested attributes of each entry; isLast, true when no entries follow; and nextPageToken. Without attributes, each entry has its path, name, type (REG for a regular file, DIR for a directory, SYMLNK for a symbolic link), size, posixPermissions, atime and mtime. When isLast is false, call again with token set to nextPageToken to get the next page.`
  - `attributes`: `Use the same allowed values as in get_file_attributes.` → the allowed names, listed in full (the text design/13 gives `get_file_attributes`).
  - `limit`: `Maximum number of children` → `Maximum number of entries in this page (1 to 100)`.
  - `offset`: `Starting offset of the children` → `Number of entries to skip from the start of the directory (0 to 500). Not used with token.`
  - `token`: `Token to continue listing from the next page of results` → `The nextPageToken of the previous page, to get the next page`.
  - `parent_id_or_path`: `File id or path to the parent file …` → `Path of the directory, /<space_name>/<path_to_directory>, or its file id`.
- **Earlier evidence:** none. No scenario in either harness offered or required it (triage, 2026-11-20).

**Before the gate-4 run**, check that the surface has not moved:

    ONEDATA_MCP_TOOLS=list_children make -s tools-manifest | sha256sum   # expect 726d5e58...

## Gate 1: implementation (done, branch commit `a2f62e8`, then `3558556`, `49370ff` and `40bd036` after the external review)

The tests are in `test/unit/api/test_list_children.py`, through the API and through an MCP client.

| Path | Before | Now |
|---|---|---|
| A next page (`token`) | **Probable defect:** `offset: 0` was always sent with the token; the swagger says `offset` "cannot be provided alongside the `token` parameter". If 25.0 enforces it, no second page could ever be read. Unverified: the live test settles it. | With a token, no offset is sent |
| A token and a non-zero offset | Both sent | Refused before any request: `Pass token or offset, not both: …` |
| `offset` below 0 or above 500 | Sent | Refused by the schema |
| `?`, `#` or `/` in an id | **Defect:** the path was cut short or became another endpoint | Percent-encoded |
| An empty id, `.` or `..` | Sent: `/data/../children` is `/children` at the API root | Refused before any request |
| The parent is a regular file | **Defect (seen live, 25.0, 2026-11-21):** Onedata does not refuse; it returns the file itself as the only child, so a model would read "the directory contains <file>" | `'…' is a regular file, not a directory; it has no children.` (detected on the first page by the single entry being the target, or one type read when the target was given by id) |
| `enotdir` on the listing itself | Onedata's message only | `The path '…' is not a directory: only a directory has entries to list.` |
| A component of the path is not a directory (`enotdir` while resolving `/s/file/child`) | Onedata's message only | `A component of path '…' before its last part is not a directory, so this provider cannot resolve the path.` (review, 6) |
| An id that names nothing (a structured not-found), a malformed id, no access | Onedata's message only | The hints of design/13 (`This provider reports nothing at …`, `This provider rejected … as a file id`, `Not allowed to list … Calling again will not help.`), then Onedata's message. (For an **id**, the no-access hint is amended after gate 4: see the last rows.) |
| A bare 404 (no structured body) | Onedata's message only | Its possible causes, not one of them (review, 5): `This provider answered 404 for … without saying why: it may not exist, its space may not be supported by this provider, or the token may not see it. Check …` |
| A path that names nothing | `Path "…" not found` | Unchanged |
| `attributes` as JSON text | pydantic `list_type` error | Read as that list, as for `get_file_attributes` (the same validator); any other string refused with what to send |
| More than 64 names | Sent | Refused by the schema |
| A deprecated name | Refused, after the path lookup | Refused before any request |
| `spaceNotSupportedBy` | **Misleading:** `Space "…" does not exist.` (the shared resolver) | `This provider does not support the space of …, so it cannot list it here; the space may still exist.` and the spaces the token sees (review, 5). An `OnedataApiError` now. The stable tools keep the shared resolver. |
| A relative path with no MCP session (the panel's API legs) | `RuntimeError: session is not available …` | `Paths must start with '/' …` (review, 7) |
| `attributes` as JSON text longer than 64 names of 128 characters, or nested too deeply to decode | Decoded unbounded; a `RecursionError` passed the hint | Refused with the same hint (review, 12) |
| A bad token (a made-up one) | **Seen live (25.0, 2026-11-21):** Onedata answers `500 internalServerError`, not a 400, so a model read a server error with nothing to fix | With a token and a 5xx: `The provider failed on this page token. A token is valid only as returned in the previous page's nextPageToken; to start over, list again without a token.`, then Onedata's message. No id hint. A 5xx with no token is handled as before (Round 2). Commit `5e05990` |
| Empty result (an empty directory) | `{"children": [], "isLast": true}` | Unchanged; the description now says what `isLast` means |
| *Rows added after gate 4 (2026-11-23), for the operator's decisions below. The description is unchanged, so the surface hashes are; these are behaviour, not surface.* | | |
| A page that is not the last (`isLast` is false) (F3) | The page as Onedata gave it | The page, plus `"more": "More entries follow: call again with token set to nextPageToken."` (without a `nextPageToken`: `"More entries follow."`). Nothing is added to a last page (`isLast` true) or to an answer that does not say. The note says what the description says (`call again with` `token` `set to` `nextPageToken`) and names no tool. |
| A directory given as an id that cannot be whole (truncated, extended, doubled, not hex) | Sent; Onedata answered 403, `spaceNotSupportedBy`, 404 or 400 | Refused before any request: `'…' is not a complete file id; copy it exactly from the earlier answer, or give the path, /<space_name>/<path>.` The check and its limits are design/13's (gate 1, the rows added after gate 4). A path is not checked. |
| No access (403, `forbidden`, `eacces`) for an **id** | `Not allowed to list file id '…': the token has no access to it. Calling again will not help.` | `Could not list file id '…': the id may be mis-copied (…), or the token may have no access to the file. Copy the id exactly from the earlier answer, or give the path, /<space_name>/<path>.` and, when it applies, ` The space in the id is not one this token sees at this provider.` For a **path** the old hint stands. |
| `spaceNotSupportedBy` for an **id** | `This provider does not support the space of file id '…', so it cannot list it here; the space may still exist.` | `Could not list file id '…': this provider reports that it does not support the space in the id. The id may be mis-copied (…): copy it exactly …` then the unseen-space sentence when it applies, and the spaces the token sees. For a **path** the row above stands. |

The 25.0 error bodies are unverified, as in design/13.

## Gate 2: surface review (done, branch commit `a2f62e8`)

`test_the_description_follows_the_surface_rules` covers the first three:

- **Says what it returns:** it did not, and above all not that a page holds 10 entries by default with more to fetch. **Fixed**: one page, its three fields, the default attributes (with `type`, which is how a model tells a subdirectory), and how to get the next page. LCp tests this text.
- **Names no tool that may not be offered alongside it:** it named `get_file_attributes`, which is not always offered with it: the panel's K1 set offers `get_file_attributes` alone, and a hand-written `ONEDATA_MCP_TOOLS` may offer either. **Fixed:** the attribute names are listed in the field.
- **Cites no document:** yes.
- **Errors tell the caller how to fix the call:** done in gate 1.
- **`make conformance`: passed** on the integrated server (the integration branch `feat/promotion-harness` at `501c867`, which carries all five packages), run 2026-11-21 (`conformance-results/20261121T010707Z`) with `@modelcontextprotocol/conformance@0.1.16`, the version `make conformance` now pins: 12 passed, 20 failed, every failure expected (`conformance-baseline.yaml`: scenarios that need prompts, resources, completion, sampling, elicitation or the suite's own test tools, none of which this server has), and the baseline check passed. Conformance tests the protocol, not one tool, so this one run serves design/11–15.
- **Reported, not changed:** the curation note `design/03-tool-allowlist-curation.md` says `list_files_recursively` with `max_depth=1` reproduces this tool; that tool has no such parameter (triage). Stable `list_files_recursively`'s `attributes` field still points to `get_file_attributes` (design/13).

## Gate 3: live check (built in branch commits `d94e5c9` and `ab05369`; done, first live run 2026-11-21: 10 passed)

[`test/live/test_files_live.py`](../test/live/test_files_live.py), section `list_children`:

1. **LCp's shape:** 11 files and 3 subdirectories (two empty) in one directory. The first page with the default limit holds 10 entries and `isLast` false; every entry is then read page by page with the token, which settles that a token page is accepted. It prints the order of the entries (LCp assumes name order puts the two empty directories on the second page), the recursive file listing of the same directory (it must show no empty directory: that is why LC requires this tool), and a page read with `offset: 10`.
2. The errors and the empty case as the model reads them, printed: listing a regular file, a missing path, a made-up token; an empty directory (asserted: `children` empty, `isLast` true).

Gated and guarded as design/13 describes; run with `make valfed-files-live` or `make test-files-live`. Since the external review (findings 4 and 11): the probe directory is made exclusively and deleted by its id; the page read with `offset: 10` must equal the token walk's second page; the three subdirectories must carry `type` `DIR`; listing a regular file, a made-up token and a missing path are asserted against the expected contracts and marked `TODO(first live run)` (the first two are settled by the first live run, below). `make valfed-check` now also builds LCp's fixture (15 entries, waited for at the local provider).

**Results.** The first live run (2026-11-21, Onedata 25.0, `make valfed-files-live`, 10 passed) settled these:

- **A regular file as the parent:** 25.0 does not fail. It lists the file as its own only entry (gate 1's row); the tool refuses it with `'<path>' is a regular file, not a directory; it has no children.`, and the live test asserts that text.
- **A made-up token** (`not-a-token`) on a directory that lists without one: **`500 internalServerError`**, not the 400 this record first assumed. The live test pins `BAD_TOKEN_STATUS = 500` and `BAD_TOKEN_ERROR_ID = "internalServerError"`, and lists the directory without a token right before and right after, since a 500 is also the answer to any server failure. The model reads the page-token hint of gate 1 (`5e05990`). Findings log M-24; Onedata-side entry 22 of `research/empirical-onedata-25.0-findings.md`.
- **Token paging:** the live file's 10 tests passed on the same day (`make valfed-files-live`, design/15's gate 3), and one of them is LCp's token walk. It asserts that the first page (default limit) holds 10 entries with `isLast` false, that each next page, requested with the token and no offset, returns a new token until `isLast`, that the walk covers all 14 entries and starts with the first page, and that a page read with `offset: 10` equals the walk's second page. So a token page is accepted without an offset. This is read from the test's assertions and the pass count, not from a recorded printout.

Recorded since, from the same run's printout (2026-11-23):

- **The order of the entries:** name order. The token walk listed `calib_runs`, `log_20200101.dat` … `log_20200111.dat`, `raw_frames`, `spare_slots`, the first page holding the first ten, and the page read with `offset: 10` held the last four. LCp does not depend on it: its fixture check refuses to start unless the first page of 100 holds none of the three subdirectories.
- **A missing path:** `Path "<path>/no-such-dir" not found`.
- **An empty directory:** `{"children": [], "isLast": true}`, with a `nextPageToken` beside them.

## Gate 4: validation run (scenarios built in branch commit `9694c3d`; run 2026-11-22, results below)

**Gate 4 runs on one integrated commit.** The packages of `remove_qos_requirement`, `schedule_file_replication`, `get_file_attributes`, `list_children` and `create_directory` are validated together, on one commit of the integration branch `feat/promotion-harness` that carries all five and the harness they share (design/08's "Scoring" and "Reading the answer"). The tool surfaces there are those of merge `d946358`, the last commit that changes any of them. Each record therefore cites the same offered-set hashes, not its own package's K1 hash. Computed as above on that tree:

| Offered set | Hash on the integrated commit |
|---|---|
| the panel's K1 set (11 tools; V1–V3, V3n, V3o, SR*, FA*) | `9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3` |
| the panel's K2 set (13 tools; V4–V7) | `6f502cb473137d4e972434c039830d7388b3e87f181dcc0386e0d0747fbc141b` |
| FILE_TOOLS (6 tools; LC*, CD*) | `c030429d0e04de218f50a1bda935d17e84026c8c24ea1a61fd6d91b0bac31922` |

The tool's own hash is unchanged by the integration. The run's record names the commit it ran on, and that commit's hashes must equal these.

### Scenario coverage (design/10)

| The protocol asks for | Scenario | Brief | Fixture | Right answer |
|---|---|---|---|---|
| a task that needs it | **LC** | "Which subdirectories does `<dir>` have? List their names." | `log_20200101.dat`, `log_20200102.dat`; `calib_runs/` holding `log_20200103.dat`; `raw_frames/` and `spare_slots/`, empty | calib_runs, raw_frames, spare_slots |
| "no, because" | **LCn** | LC's, word for word | two files and no subdirectory | none |
| offered but not needed | **LCo** | "Which log files are in `<dir>`, including those in its subdirectories? List their names." | LC's | log_20200101.dat, log_20200102.dat, log_20200103.dat (the last in calib_runs) |
| argument variants | **LCp** | LC's, word for word | eleven files, then LC's three subdirectories (14 top-level entries) | calib_runs, raw_frames, spare_slots; two of them are on the second page of the default `limit` |
| the end state after a mutating tool's answer | not applicable: the tool reads only. But the set offers `create_directory` and `create_file`, so every LC scenario checks that the trial directory is unchanged, at the answer and after it. | | | |

- **Requires.** LC and LCp offer FILE_TOOLS. `list_files_recursively` lists regular files only, so it reveals `calib_runs` (through `calib_runs/log_…`) but never the two empty directories; `get_file_attributes` cannot list; `download_file` reads content. Probing names with `create_directory` would change the tree, which fails the trial. A full trial on the fake federation shows the recursive listing alone gives the wrong answer (`test_lc_the_recursive_file_listing_alone_misses_them_too`).
- **LCn** keeps LC's brief, so a model that answers without listing fails one of them.
- **LCo** is answered by one call of `list_files_recursively`; a top-level listing alone misses `log_20200103.dat`.
- **LCp** is the variant that matters for this tool's arguments: `limit`, `isLast` and `nextPageToken`, and the token fix of gate 1. After the external review (finding 1) it holds 113 entries: 110 empty files `acq_0001.dat`…`acq_0110.dat`, then LC's three subdirectories, made last. Before the first turn the fixture lists the directory at the local provider with `list_children`'s largest limit (100) and refuses to start (RESET_FAIL) unless the page is not the last and holds none of the three: the answer needs a second page whatever order 25.0 lists in. A model that reads one page, even at limit 100, sees no subdirectory and fails.
- **LCi**, an id variant added after the external review (finding 9, reversing the earlier "no id variant"): "Which subdirectories does the directory with the id `<id>` have?", LC's fixture, the directory given by its id only. It requires `list_children` called with that id.
- **Required call (LC, LCn, LCp, LCi).** A pass needs successful `list_children` calls on the directory that, together, show every one of its top-level entries in their `children` lists (by `fileId`, or by name when no id was asked for): a listing read to its end. The answers are parsed structurally, so a listing asked for ids and types only counts, and a `name` nested elsewhere in an answer does not.

### How a trial is judged

- **Answer readers** (`file_oracles`, each one small function to be swapped for the rubric judge): `names_claimed` returns which of the three subdirectory names the answer mentions, the empty set if it mentions none and says there are none, else unreadable; `log_names_claimed` (LCo) returns every `log_YYYYMMDD.dat` it mentions. `test/unit/k2val/test_scenarios_lc.py` reads tables of real-sounding answers: lists, bullet paths, "no other subdirectories", "zero", "does not contain any", answers that also name the files.
- **mcp:** the reader returns the expected set, AND the tree at the answer equals the baseline (no entry added, removed, or changed in id, type, size or mode).
- **federation:** the tree after the answer equals the baseline.
- **An unreadable tree is never judged:** a failed or malformed listing (no `children` list; not last with no token) raises, and the trial is `ORACLE_ERROR`, a harness outcome.
- **Extra names are kept** (external review, finding 3): the readers also return a name the answer lists that is not in the fixture (an invented `archive`, a `notes.txt`), so the set is wrong and the trial fails. A subdirectory name mentioned in a negation ("there is no raw_frames") still counts as claimed; the judge reads it otherwise, and the trial is NEEDS_REVIEW. Since the integration, the shared checks of design/08's "Scoring" also apply: a pass needs the right answer and every check that applies; the answer is read by this reader and by the judge, and a disagreement is NEEDS_REVIEW.

### The run

- Panel: the six standing configs, `sonnet-medium` the control. K = 8: 5 scenarios × 6 configs × 8 = 240 trials.

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_SCENARIOS=LC,LCn,LCo,LCp,LCi K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

### Results

Run 2026-11-22 with design/13's and design/15's scenarios: run ids `files-k8-*`, commits and records as in [design/13's results](13-promotion-get-file-attributes.md#results). The offered surface every counted LC trial saw is FILE_TOOLS, `c030429d0e04de218f50a1bda935d17e84026c8c24ea1a61fd6d91b0bac31922`, as above.

The 240 trials, as stored: 217 PASS, 1 FAIL, 21 NEEDS_REVIEW, 1 NO_RESULT. Re-scored offline with the reader fixes below and the stored verdicts (`rejudge --cached-only`): 235 PASS, 3 FAIL, 1 NEEDS_REVIEW, 1 NO_RESULT.

PASS per config and scenario, after classification (K=8):

| Config | LC | LCn | LCo | LCp | LCi |
|---|---|---|---|---|---|
| sonnet-medium (control) | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| haiku-low | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| glm | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| qwen36 | 8/8 | 8/8 | 8/8 | 7/8 | 8/8 |
| qwencoder | 8/8 | 8/8 | 8/8 | 6/8 | 8/8 |
| deepseek | 8/8 | 8/8 | 8/8 | 7/8 | 8/8 |

236 of 240 pass (235 as re-scored, and Haiku LCo #3, PASS by hand). Pass rates are reported; they do not gate. The control passes all 40.

- **Classifications:**
  - **Harness (oracle) bug, fixed and re-scored: the LC and LCo readers took words, extensions and patterns for names (20 NEEDS_REVIEW; 18 now PASS, 2 FAIL as the judge read them; no stored PASS changed).**
    - `names_claimed` took a sentence's opening word before a comma ("Therefore, it has no subdirectories", DeepSeek LCn #4, Qwen3-Coder LCn #3, #6; "Additionally, …", Qwen3.6 LCi #7; "Thus, …", Qwen3-Coder LCp #3). It also took "e.g." (GLM LCi #6), a quoted JSON value ("the `isLast` was `true`", DeepSeek LCp #2), an extension ("regular files (`.dat` files)", Qwen3.6 LCp #1, #5; DeepSeek LCp #0, #3, #6, #7; Qwen3-Coder LCp #3, #5) and a glob ("110 `acq_*.dat` files", Haiku LCp #3; GLM LCp #5).
    - `log_names_claimed` (LCo) took a naming pattern for a file ("named in the `log_YYYYMMDD.dat` pattern", GLM LCo #5, #7, Qwen3.6 LCo #6; "matching the `log_*.dat` naming", GLM LCo #1).

    Connectives, abbreviations and JSON values join the readers' list of words that are not names, and an extension alone or a pattern is not a name. An invented name is still kept (paired cases, `test/unit/k2val/test_gate4_readers.py`). Qwen3-Coder LCp #3 and #5 now read "none" and FAIL (below).
  - **NEEDS_REVIEW resolved by hand (1):** Haiku LCo #3. Both readers agree on the three right names, but the judge's evidence joins three list items with commas, which the quote check rightly rejects as not one passage. The right reading is the three names: PASS (one `list_files_recursively` call, the directory unchanged). The check behaved as specified; nothing to fix.
  - **Deployment artefact (1): DeepSeek LCp #4,** NO_RESULT. After four good pages, 29 of its `list_children` calls failed with `connection_error` from the provider, and it ran out of its 40 rounds. Not a control trial; a re-run would only complete the grid.
  - **Paging stopped while `isLast` was false (3): Qwen3.6 LCp #4, Qwen3-Coder LCp #3, #5.** This is finding F3 below.
- **Tool-attributable findings:**
  - **F3 (was L3a, open; since 2026-11-23 not L3a under the one-family rule, fixed by decision; re-run 2026-11-23, below): paging stopped while `isLast` was false (LCp).**
    - Qwen3.6 #4 read four pages of 10 and one of 100, each `isLast: false`, and answered "`acq_0001.dat` through `acq_0100.dat` (and potentially more …). **The directory has no subdirectories.**"
    - Qwen3-Coder #3 read one page (10 entries, `isLast: false`) and answered "does not contain any subdirectories".
    - Qwen3-Coder #5 read four pages, each `isLast: false`, and answered "I've checked all pages".

    The evidence bar was read as met by two model families (the operator counted Qwen3.6 and Qwen3-Coder as two); **superseded 2026-11-23:** the operator counts models of one vendor lineage as one family (design/10, "The standing panel"), so these are one family (Qwen), and no control trial failed. The record above also presumes this case tool-attributable "until shown otherwise", since the description is what tells a model to page. #5 misreads this tool's own answer (`isLast: false` taken as the last page).

    *Against attribution:* the same models read the listing to its end in 6 of 8 (Qwen3-Coder) and 7 of 8 (Qwen3.6) LCp trials at the same surface; Qwen3.6 #4 shows it knew more entries followed; Sonnet, Haiku, GLM and DeepSeek always paged to the end. That does not show a cause outside the tool, so the finding stays open.

    *Fixes to consider:* a result that states the continuation when `isLast` is false (for example "more entries follow: call again with token"), or a larger default `limit` (10 now; 100 would fit LC's fixtures, though not LCp's 113 entries). Either changes the result's shape or the schema's default, so after it LCp must be re-run.

    **Decision (operator, 2026-11-23): fix, and re-run LCp at K=8.** Not L3a under the one-family rule, but a modest tool change prevents the failure, and the protocol's aim is tools that less capable models can use (design/10, "Aim"). The first option was taken, not a larger default `limit`: that changes the schema (the surface), and does not help a directory longer than the limit. A page that is not the last now carries `more` (gate 1, the rows added after gate 4). The description is unchanged, so the surface hashes are. Unit tests: `test_a_page_that_is_not_the_last_says_more_entries_follow` and the cases around it (nothing on a last page, nothing when the answer does not say), and `test_the_note_reaches_the_model_and_agrees_with_the_description`. The re-run is below.
- **The run passes when** (design/10, gate 4): every control trial passes, with control trials lost to a harness bug or deployment artefact re-run after the fix, so each scenario ends with K passing control trials; and no tool-attributable finding is open (fixed and re-run, or accepted only with its cause outside the tool and the operator's sign-off).

**Verdict of the first run: BLOCKED by F3 until the re-run.** The control passes every trial. On 2026-11-23 the operator chose the fix over a sign-off: the fix is made (above), and gate 4 passes once LCp is re-run at K=8 and the re-run is clean. `list_children` also shared the hints on a mis-copied id of design/13 ("Open: the hints on a mis-copied id"). No LCi trial passed a mis-copied id, but the same fix is made here (design/13, "Fixed 2026-11-23"), and LCi is re-run. **Superseded by the re-run, below.**

### Re-run after the 2026-11-23 decisions (run 2026-11-23)

LCp and LCi ran at K=8 on the six-config panel, in one invocation with design/13's FAi and design/12's SR1 and SR1i, in the three batches of the first run (each batch resumed after the federation was restarted):

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_ARTEFACT_ROOT=$VALFED_HOME/evidence/k2val \
    K2VAL_RID=rerun-k8-sonnet-medium-glm \
    K2VAL_SCENARIOS=SR1,SR1i,LCp,LCi,FAi K2VAL_K=8 K2VAL_MODELS=sonnet-medium,glm
# then the same options with
#   K2VAL_RID=rerun-k8-haiku-low-qwen36    K2VAL_MODELS=haiku-low,qwen36
#   K2VAL_RID=rerun-k8-qwencoder-deepseek  K2VAL_MODELS=qwencoder,deepseek
```

- **Run.** 2026-11-23, 15:56 to 23:04 UTC; run ids `rerun-k8-sonnet-medium-glm`, `rerun-k8-haiku-low-qwen36` and `rerun-k8-qwencoder-deepseek`, in the durable evidence store. The runner was master `e2697c2`: after the `more` note and the id guard (PR #46), before PR #52 (the replication tool's name refusal). `list_children` and `get_file_attributes` are the same at `e2697c2` and at master `2ce1182`.
- **Records.** The three directories hold 265 records for the 240 planned trials. The 25 superseded ones are infrastructure (19 ORACLE_ERROR, an unreadable snapshot; 5 RESET_FAIL; 1 HARNESS_ERROR), each followed by a valid record of the same trial; 13 of them are LC's (LCp: 7 ORACLE_ERROR, 3 RESET_FAIL; LCi: 1 and 2). The record per trial is the last valid one. The offered surface in every counted LC trial is FILE_TOOLS, `c030429d0e04de218f50a1bda935d17e84026c8c24ea1a61fd6d91b0bac31922`, as above.
- **Scoring.** Each answer was read by the deterministic reader and the judge. Re-scored offline at master `2ce1182` with the stored verdicts (`rejudge --cached-only`, no judge call), every stored outcome is reproduced. The reader fix of this round (design/12, the re-run) changes SR1 readings only; across every scored answer in the evidence store, no LC reading changes.

PASS per config (K=8; the first run's in brackets where it differs):

| Config | LCp | LCi |
|---|---|---|
| sonnet-medium (control) | 8/8 | 8/8 |
| haiku-low | 8/8 | 8/8 |
| glm | 8/8 | 8/8 |
| qwen36 | 8/8 (7/8) | 8/8 |
| qwencoder | 7/8 (6/8) | 8/8 |
| deepseek | 7/8 | 8/8 |

94 of 96 pass, with no NEEDS_REVIEW; the control passes all 16, so every control trial of the five LC scenarios has passed (40 in the first run, 16 here). DeepSeek LCp #4, the first run's deployment artefact, passed.

- **F3: the fix was exposed, and it worked.** Every LCp trial met a page with `isLast` false, so all 48 got the `more` note (207 notes in all), and 46 read the listing to its end. Qwen3.6 did in 8 of 8 (7 of 8 before), Qwen3-Coder in 7 of 8 (6 of 8). DeepSeek said it read the note: "There are more entries. Let me get the next page." (#1, before its second call).
- **Classifications (2 FAIL):**
  - **Qwen3-Coder #2: F3's shape, once, with the note in the answer it acted on. Model capability (L3b).** [0] one page of 10 entries, `isLast: false`, ending `"more": "More entries follow: call again with token set to nextPageToken."`; the answer: "does not contain any subdirectories. It only includes regular files … Thus, there are no subdirectories in this path." One family (Qwen) and no control failure, so the two-family clause does not apply. Nor does the third (one model with a traced mechanism): the answer it acted on said that more entries follow, the opposite of its conclusion. The same model, with the same note in the same place, paged to the end in the other seven trials.
  - **DeepSeek #1: a files-only listing taken for the whole directory. Model capability (L3b), not F3** (findings log M-48). It read four pages, each with the note, then: "So far all entries are regular files (acq_*.dat), not directories. Let me continue to check for any directories among the remaining entries. Let me use a recursive listing with a larger limit to be more efficient." It listed with `list_files_recursively` (stable), whose description says it lists "non-directory files", to the end ([4], [5], `name` and `type` only), and answered: "I've now listed all the files in the directory. All entries are regular files (`acq_*.dat` files and one `log_20200112.dat`). There are **no subdirectories**". That `log_20200112.dat` is the file inside `calib_runs`. It knew that more entries followed, so F3's mechanism (a page taken for the last) is absent; what failed is reading a listing of files as the directory's entries. **The verdict rests on this reading:** counted as F3's diagnosis, F3 would recur in two families (Qwen and DeepSeek) and be L3a again.
- **The id guard: no exposure here.** Every LCi call passed the directory's id exactly as given, so neither the refusal of an incomplete id nor the reworded `403` and `spaceNotSupportedBy` hints was drawn: **NOT VERIFIED** in this tool's scenarios. The check is shared with `get_file_attributes`, where it refused 28 calls in FAi (design/13, the re-run).
- **The Aim (design/10), per remaining failure:**
  - Qwen3-Coder #2: no change proposed. The note was there and plain. The options would be the note before `children` (it comes last), or a larger default `limit` (a schema change, rejected with F3's decision); nothing in the other 47 trials points to the note's place.
  - DeepSeek #1: `list_children`'s note could say that a page holds up to 100 entries (`limit`), since the model left the tool "to be more efficient". Not proposed: DeepSeek set `limit` to 100 itself in 6 of its other 7 LCp trials. `list_files_recursively`'s answer could say that it lists no directories: a stable tool's result, so a K=3 smoke re-run (M-48).

**Verdict: PASSES gate 4** (2026-11-24). The control passes every trial of both runs; F3's fix was exposed in every LCp trial, and its one recurrence is L3b; DeepSeek #1 is another diagnosis (above); no tool-attributable finding is open. The guard's hints stay NOT VERIFIED here (no model exposed to them). The offline rescore is `rescore-rerun-k8-2026-11-23`, in the evidence store.

## External review (2026-11-20)

An external frontier-model review of this package (the three file tools, their scenarios and the harness) reported twelve findings. The tool and live-test findings are fixed on this branch, as new commits after `0b015eb`. The harness findings go to the integration branch, which owns `benchmark/k2val/` and `test/unit/k2val/`; this branch leaves those files alone after the review.

| # | Finding | Status |
|---|---|---|
| 1 | LCp can pass without paging: `limit` up to 100 returns all 14 entries in one call, and the 25.0 listing order is not verified | harness: resolved in the integration branch |
| 2 | CDn's answer label omits the reason: "the directory already exists" passes, though the blocker is a regular file | harness: resolved in the integration branch |
| 3 | The LC and LCo readers cannot report a name outside the expected set (an invented fourth subdirectory, a file name outside the date pattern) | harness: resolved in the integration branch |
| 4 | The live test adopted a probe directory it did not create exclusively, deleted it by path, and suppressed cleanup errors | fixed here, `ab05369`: made exclusively (a taken name is retried, never adopted), deleted by its id in a `finally`, a failed deletion fails the teardown |
| 5 | A bare 404 became "Nothing exists" (and, with `create_parents` false, a missing parent); `spaceNotSupportedBy` became "does not exist" | fixed here, `49370ff`: specific diagnoses need distinguishing evidence (a structured not-found, `enoent`/`notFound`; `spaceNotSupportedBy`, now "this provider does not support the space ... the space may still exist"); a bare 404 gets its possible causes and what to check; Onedata's message is kept |
| 6 | Any 409 became "already exists"; the description promised the existing entry's type; `enotdir` while resolving `/space/file/child` called the whole path a file | fixed here, `49370ff`: "already exists" only with `eexist` or a 409 whose path then resolves, else a conflict (an `OnedataApiError`, not `FileExistsError`); the description says the type is given "when the provider can tell"; `enotdir` while resolving names "a component ... before its last part" |
| 7 | With an empty roots list, `create_directory("reports/2020")` reached the API (and could create `/reports/2020`); on the no-session path a relative path raised "session is not available" | fixed here, `40bd036`: a creation path must be absolute after root resolution, and its space component a name (`//x`, `/./x`, `/../x` refused); the three tools answer the no-session case with the same `Paths must start with '/'` hint |
| 8 | The CD end-state check cannot see changed file contents with the same id, size and mode, or a write outside the trial directory | harness: resolved in the integration branch |
| 9 | FA inherits the configured K1 set, so a set with a listing tool would not require this tool; LC has no id variant | harness: resolved in the integration branch |
| 10 | A non-final page that repeats its token keeps the harness's listing running forever | harness: resolved in the integration branch |
| 11 | The live listing test printed the offset page without checking it, accepted any answer for a listed file or a made-up token, and did not check the subdirectories' type | fixed here, `ab05369`: the offset page must equal the token walk's second page; the three subdirectories must carry `DIR`; the expected 25.0 failure contracts are asserted and marked `TODO(first live run)` |
| 12 | The JSON-text `attributes` was decoded before any bound, and a `RecursionError` bypassed the hint | fixed here, `3558556`: a text longer than 64 names of 128 characters is refused before decoding; a `RecursionError` gets the same hint |

The review also noted, without a finding: the cap of 64 names permits batching `xattr.<key>` names; `offset` 0 to 500 matches the exposed arguments; with a token no offset is sent. The pinned swagger, the live 25.0 behaviour and conformance were not verified by the reviewer either.

### Round 2

Two further external reviews (Codex and Gemini, round 2) of the whole promotion package found defects that the first round's fixes left or introduced. This list is the tool-level part, fixed in `fix/r2-tools` (code and unit tests only: no container, live test or model call was run). The harness items of both reviews (`benchmark/k2val/`, `test/unit/k2val/`) are not changed here and are not claimed as resolved by this list.

| Review item | Finding | Status |
|---|---|---|
| Codex 12 | A 500 with `enoent` on the listing or on the path lookup read as a missing target; a 5xx with `enotdir` read as "not a directory" | fixed, `9658ffa`: those diagnoses need a 4xx status; a 5xx that mentions a missing item gets the uncertain hint; any other 5xx passes unchanged |
| Codex 13; Gemini 8.1 | The heuristic masked dotted real text in error messages | fixed, `c32a67f` (design/11, Round 2) |
| Gemini 8.3 | A 3xx was a success | fixed, `c32a67f` |
| Codex 14 | The live listing test passed any `*.datahub.test` name | fixed, `673a2c6`: the shared loopback guard and proxy isolation |
| Codex, earlier file #11 | A made-up pagination token passed on any error, a server error included | fixed, `673a2c6`: it must answer the swagger's 400 as a structured Onedata error (not a 5xx, not a gateway page). The error id is **not recorded** by the swagger: `BAD_TOKEN_ERROR_ID` is a named constant, `TODO(first live run)`, to be set from what the first run prints. **Superseded by the first live run (2026-11-21):** 25.0 answers a made-up token with `500 internalServerError`, not a 400. The test now pins `BAD_TOKEN_STATUS = 500` and `BAD_TOKEN_ERROR_ID = "internalServerError"`, still requires a structured Onedata error (not a gateway page), and checks that the same directory lists, unchanged, without a token right before and right after, because a 500 does not by itself point at the token (`5e05990`) |
| Codex 1–11, 15; Gemini 1–6, 9, 10 | Harness (the repeated-token listing hang in `children()` included) | not changed here |

## Gate 5: record

This file. To complete it: fill in the TODOs of gates 2 to 4; then, in the same PR, move `list_children` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, change its tier in README's tool table, and update `test/unit/test_tool_maturity.py`, which lists it as experimental.
