# Promotion record: `list_children`

Status: **in progress** (2026-11-20). Gate 1 is done. Gate 2 is done except for `make conformance`, which has not been run. Gate 3's live test is built but has not been run. Gate 4's scenarios are built and unit-tested on the fake federation, but the run has not happened. The validation federation is on hold, so nothing here touched a live Onedata. The tool stays `experimental` until gates 2–4 are complete. It moves to `STABLE` in the PR that completes this record.

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
| FILE_TOOLS (6 tools; the LC and CD scenarios run on it) | `43dd847ef84ddcd542278d3705a3b6e2affa3e769601545bc067da09fec6d114` | `3ace77c4063de4e764b36c3b245e06639cb6b8e1d72d0da340009fe1534a7299` (with design/13's and design/15's surfaces) |

FILE_TOOLS is `create_directory, create_file, download_file, get_file_attributes, list_children, list_files_recursively`.

- `a2f62e8` is the last commit that changes this tool's surface.
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

## Gate 1: implementation (done, branch commit `a2f62e8`)

The tests are in `test/unit/api/test_list_children.py`, through the API and through an MCP client.

| Path | Before | Now |
|---|---|---|
| A next page (`token`) | **Probable defect:** `offset: 0` was always sent with the token; the swagger says `offset` "cannot be provided alongside the `token` parameter". If 25.0 enforces it, no second page could ever be read. Unverified: the live test settles it. | With a token, no offset is sent |
| A token and a non-zero offset | Both sent | Refused before any request: `Pass token or offset, not both: …` |
| `offset` below 0 or above 500 | Sent | Refused by the schema |
| `?`, `#` or `/` in an id | **Defect:** the path was cut short or became another endpoint | Percent-encoded |
| An empty id, `.` or `..` | Sent: `/data/../children` is `/children` at the API root | Refused before any request |
| The parent is a regular file (`enotdir`) | Onedata's message only | `The path '…' is a regular file, not a directory: only a directory has entries to list.` |
| An id that names nothing, a malformed id, no access | Onedata's message only | The hints of design/13 (`Nothing exists at …`, `Onedata rejected … as a file id`, `Not allowed to list … Calling again will not help.`), then Onedata's message |
| A path that names nothing | `Path "…" not found` | Unchanged |
| `attributes` as JSON text | pydantic `list_type` error | Read as that list, as for `get_file_attributes` (the same validator); any other string refused with what to send |
| More than 64 names | Sent | Refused by the schema |
| A deprecated name | Refused, after the path lookup | Refused before any request |
| An unsupported space's id | Space hint | Unchanged |
| A bad token | Onedata's 400 | Unchanged: passed on, with no id hint |
| Empty result (an empty directory) | `{"children": [], "isLast": true}` | Unchanged; the description now says what `isLast` means |

The 25.0 error bodies are unverified, as in design/13.

## Gate 2: surface review (done except `make conformance`, branch commit `a2f62e8`)

`test_the_description_follows_the_surface_rules` covers the first three:

- **Says what it returns:** it did not, and above all not that a page holds 10 entries by default with more to fetch. **Fixed**: one page, its three fields, the default attributes (with `type`, which is how a model tells a subdirectory), and how to get the next page. LCp tests this text.
- **Names no tool that may not be offered alongside it:** it named `get_file_attributes`, which is not always offered with it: the panel's K1 set offers `get_file_attributes` alone, and a hand-written `ONEDATA_MCP_TOOLS` may offer either. **Fixed:** the attribute names are listed in the field.
- **Cites no document:** yes.
- **Errors tell the caller how to fix the call:** done in gate 1.
- **`make conformance`: TODO.**
- **Reported, not changed:** the curation note `design/03-tool-allowlist-curation.md` says `list_files_recursively` with `max_depth=1` reproduces this tool; that tool has no such parameter (triage). Stable `list_files_recursively`'s `attributes` field still points to `get_file_attributes` (design/13).

## Gate 3: live check (built in branch commit `d94e5c9`, not run)

[`test/live/test_files_live.py`](../test/live/test_files_live.py), section `list_children`:

1. **LCp's shape:** 11 files and 3 subdirectories (two empty) in one directory. The first page with the default limit holds 10 entries and `isLast` false; every entry is then read page by page with the token, which settles that a token page is accepted. It prints the order of the entries (LCp assumes name order puts the two empty directories on the second page), the recursive file listing of the same directory (it must show no empty directory: that is why LC requires this tool), and a page read with `offset: 10`.
2. The errors and the empty case as the model reads them, printed: listing a regular file, a missing path, a made-up token; an empty directory (asserted: `children` empty, `isLast` true).

Gated and guarded as design/13 describes; run with `make valfed-files-live` or `make test-files-live`. `make valfed-check` now also builds LCp's fixture (15 entries, waited for at the local provider).

**Results: TODO.** Record:

- whether the token page was accepted (if not, the old code's `offset` was not the cause; find what is);
- the order of the entries. If 25.0 does not list in name order, LCp may hold all three subdirectories on its first page: change LCp's names so the two empty ones sort last, before the run;
- the error texts of a regular file, a missing path and a made-up token.

## Gate 4: validation run (scenarios built in branch commit `9694c3d`, not run)

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
- **LCp** is the variant that matters for this tool's arguments: `limit`, `isLast` and `nextPageToken`, and the token fix of gate 1. A model that reads one page answers calib_runs alone and fails. It relies on name order (gate 3 settles it).
- **No id variant.** The id branch of `parent_id_or_path` is the same code as `get_file_attributes`', whose FAi covers it in a run; the encoding and the dot ids are unit-tested here. Add an LCi if the operator wants it run for this tool too.

### How a trial is judged

- **Answer readers** (`file_oracles`, each one small function to be swapped for the rubric judge): `names_claimed` returns which of the three subdirectory names the answer mentions, the empty set if it mentions none and says there are none, else unreadable; `log_names_claimed` (LCo) returns every `log_YYYYMMDD.dat` it mentions. `test/unit/k2val/test_scenarios_lc.py` reads tables of real-sounding answers: lists, bullet paths, "no other subdirectories", "zero", "does not contain any", answers that also name the files.
- **mcp:** the reader returns the expected set, AND the tree at the answer equals the baseline (no entry added, removed, or changed in id, type, size or mode).
- **federation:** the tree after the answer equals the baseline.
- **An unreadable tree is never judged:** a failed or malformed listing (no `children` list; not last with no token) raises, and the trial is `ORACLE_ERROR`, a harness outcome.
- **Known limits of the readers**, left to the judge: a subdirectory name mentioned in a negation ("there is no raw_frames") counts as claimed; a hallucinated extra subdirectory is not seen.

### The run

- Panel: the six standing configs, `sonnet-medium` the control. K = 8: 4 scenarios × 6 configs × 8 = 192 trials.

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_SCENARIOS=LC,LCn,LCo,LCp K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

### Results: TODO

| Config | LC | LCn | LCo | LCp |
|---|---|---|---|---|
| sonnet-medium (control) | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| haiku-low | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| glm | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwen36 | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwencoder | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| deepseek | TODO /8 | TODO /8 | TODO /8 | TODO /8 |

Run id, commit and surface hash: TODO. Classify every non-pass before anything counts.

- **Classifications:** TODO. An LCp failure that read one page with `isLast: false` in the answer it got is tool-attributable until shown otherwise: the description is what tells a model to page.
- **Tool-attributable findings:** TODO.
- **The run passes when:** the control passes all four, or each control failure is a harness bug or an artefact; and no tool-attributable finding is left open.

## Gate 5: record

This file. To complete it: fill in the TODOs of gates 2 to 4; then, in the same PR, move `list_children` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, change its tier in README's tool table, and update `test/unit/test_tool_maturity.py`, which lists it as experimental.
