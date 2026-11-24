# Promotion record: `get_file_attributes`

Status: **in progress** (updated 2026-11-23). Gates 1 to 3 are done: the live test passed against Onedata 25.0 (2026-11-21, 10 passed). Gate 4 ran 2026-11-22 and passes (results below); FAi is being re-run at K=8 after the mis-copied-id guard (PR #46) before promotion.

The short commit ids below are **branch commits** on `feat/promote-file-tools`. A squash merge leaves them unreachable from `master`. **The anchor is the surface hash.**

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md). The panel is described in [`08-readiness-validation-panel.md`](08-readiness-validation-panel.md); the scenarios below are in [`benchmark/k2val/file_scenarios.py`](../benchmark/k2val/file_scenarios.py), each with its brief, right answer and rules. The first record, [`11-promotion-remove-qos-requirement.md`](11-promotion-remove-qos-requirement.md), is the model for this one. `list_children` ([`14`](14-promotion-list-children.md)) and `create_directory` ([`15`](15-promotion-create-directory.md)) are promoted alongside, on the same branch.

## The tool

- **REST call:** `GET /data/{id}` with a JSON body `{"attributes": [...]}` on the Oneprovider (swagger `get_attrs`; oneprovider-swagger tag `25.0`, commit `39da981`, `paths/data/id.yaml`). A path is first resolved with `POST /lookup-file-id/{path}`. The swagger lists 200, 400, 403, 404 and 500, every error with the generic `Error` body; the error ids are not specified.
- **Input:** `file_id_or_path`, a string; `attributes`, an optional list of names, **now capped at 64** (the swagger names 34 attributes, and `xattr.<key>` adds any number).
- **Read-only:** annotated `readOnlyHint: true`; not in `token_policy.WRITE_TOOL_NAMES`.
- **Code:** `onedata_mcp/modules/files.py` (surface) and `onedata_mcp/api/files.py` (call and errors).
- **Callers of the same API function:** stable `download_file` and the readiness tools call `api.files.get_file_attributes`. They get the encoding and the hints below; their surfaces are unchanged (`download_file` alone hashes the same before and after, `516446b1…`), and the readiness code keys on the error's status and errno, which the hints keep.

## The surface being promoted

The listing is `scripts/tools_manifest.py`'s output (`make -s tools-manifest`) with `ONEDATA_MCP_TOOLS` set to the selection, hashed with sha256, as in design/09 and design/11.

| Selection | Before (master `4047a56`) | Promoted surface (since branch commit `a77989b`) |
|---|---|---|
| `get_file_attributes` alone | `c308db7fa881b1561c454ec2dd70bbe338f3271b60828ccc75bb80cb16060fb9` | `cfe0051ecfed92228b5df58f5b0420a9e1e1750efbd6b1e68069f41750754073` |
| the panel's K1 set (11 tools; FA and V1 run on it) | `dc1d10dcce1a81e8b15c273adb99d007db9a6fdd169ce366ed43b5a38d49cd2e` (= design/11's promoted K1 surface) | `21aef26ae05b9d86ef26a24d365b92c099e8935ac6a39c16b542196b3032a6b3` |

- `a77989b` is the last commit that changes this tool's surface; later commits leave it alone. Over the whole default list, only `get_file_attributes`, `list_children` and `create_directory` differ from master.
- The `tools/list` diff for this tool. The name and annotations are unchanged; the input schema gains `maxItems: 64` on `attributes`, which stays a list of strings:
  - description: `Get attributes for a file id or a logical path.` → `Get the attributes of a file or directory, given by path or file id.` + `Returns an object that maps each requested attribute name to its value. Without attributes, it returns path, name, type (REG for a regular file, DIR for a directory, SYMLNK for a symbolic link), size, posixPermissions, atime and mtime.`
  - `file_id_or_path`: `File id or path to the file in format /<space_name>/<path_to_file>` → `Path of the file or directory, /<space_name>/<path_to_file>, or its file id`.
  - `attributes`: the same allowed names, reworded without the indentation and trailing blanks of a Python docstring, `xattr.*` spelled `xattr.<key> for one extended attribute`, and `at most 64`.
- **Earlier evidence is supporting only.** The triage of the experimental tools (2026-11-20) reported, from the earlier panel run: V1 answered 21 of 28 times with this tool (118 calls, 2 errors, both Qwen3-Coder's JSON-text list), and `get_file_distribution` answered V1 in the other trials, so V1 does not require it. Those figures are reported, not recounted, and the run used the old surface.

**Before the gate-4 run**, check that the surface has not moved:

    ONEDATA_MCP_TOOLS=get_file_attributes make -s tools-manifest | sha256sum   # expect cfe0051e...

## Gate 1: implementation (done, branch commit `a77989b`, then `3558556`, `49370ff` and `40bd036` after the external review; the surface hash is unchanged by them)

Every error path a model can trigger, and what the model now reads. The tests are in `test/unit/api/test_get_file_attributes.py`, through the API and through an MCP client (the text as it arrives), and through `app.call_tool`, the panel's own path.

| Path | Before | Now |
|---|---|---|
| `attributes` sent as JSON text, `'["size","mtime"]'` (Qwen3-Coder, twice in V1) | pydantic `list_type` error | **Read as that list** (a `mode="before"` validator). The published schema stays a list of strings, checked by a test. |
| `attributes` as any other string (`"size"`, `"size,mtime"`, `'[1, 2]'`, a JSON object) | `list_type` error | Refused: `attributes must be a list of attribute names, for example ["size", "mtime"]; got the string …` |
| `attributes` as JSON text longer than 64 names of 128 characters, or nested too deeply to decode | — | Refused with the same hint, before decoding; a `RecursionError` is caught (review, 12) |
| More than 64 names | Sent | Refused by the schema (`maxItems: 64`) |
| A deprecated name (`mode`, `owner_id`, …) | Sent; Onedata's deprecated path | Refused before any request with the new name (`mode->posixPermissions`), as the two list tools already did |
| An unknown name (`owner`) | Onedata's 400 | Unchanged: Onedata's 400 names the value. The live test prints it. It gets no id hint. |
| `?`, `#` or `/` in an id | **Defect:** `?` and `#` cut the path short, so another file's attributes came back; `/` turned the call into another endpoint (`ab/children`) | `?` and `#` are percent-encoded. A `/` is refused before any request by the shared segment guard (`utils.id_segment`, as in the stable tools), for an id the caller gave and for the id a path lookup returned; before the review of the id guard, a looked-up `ab/cd` went out as `ab%2Fcd` (review of the id guard, 4) |
| An empty id, `.` or `..` | Sent: `/data/..` is the API root | Refused before any request: `Give a path, /<space_name>/<path>, or a file id.` |
| An id that names nothing, as a structured not-found (`notFound`, `enoent`) | Onedata's message only | `This provider reports nothing at file id '…': it may have been deleted, or the value is not a file id. Give a path, /<space_name>/<path>, or a file id.` Onedata's message follows. |
| A bare 404 (no structured body), by id or while resolving a path | Onedata's message only | Its possible causes, not one of them (review, 5): `This provider answered 404 for … without saying why: it may not exist, its space may not be supported by this provider, or the token may not see it. Check the path or id, the space and the token's access.` |
| A malformed id (400 `badValueIdentifier`, or a bad value with key `id`) | Onedata's message only | `This provider rejected file id '…' as a file id.` and the same advice |
| `spaceNotSupportedBy`, by id or while resolving a path | Onedata's message only | `This provider does not support the space of …, so it cannot read the attributes of it here; the space may still exist.` and the spaces the token sees. Before the review it said `Space "…" does not exist` (review, 5). It is an `OnedataApiError`, which the readiness tools' reads catch. |
| A component of the path is not a directory (`enotdir` while resolving) | Onedata's message only | `A component of path '…' before its last part is not a directory, so this provider cannot resolve the path.` (review, 6) |
| A path that names nothing | `Path "…" not found` | Unchanged |
| No access (403, `forbidden`, `eacces`, `eperm`) | Onedata's message only | `Not allowed to read the attributes of …: the token has no access to it. Calling again will not help.` (For a file given as an **id**, amended after gate 4: see the last rows.) |
| A relative path, client without MCP roots | `Paths must start with '/'` hint | Unchanged |
| A relative value with an MCP root mounted | Any value of 20 or more letters and digits was taken for an id: a long plain name (`quarterlyreportsarchive`) was refused as not a complete file id, and a copied id with punctuation in it (quotes, a space, a full stop) was looked up as a path under the root | A value is an id when it starts as one: the header's ten zeros and sixteen hex digits, or the hex of `guid#` (or `shareGuid#`, or the older term) near its start, punctuation and spaces left out. A long plain name is resolved under the root; a copied id, damaged or wrapped, is checked as an id and refused with the row below. `create_directory` (which takes no id) and the stable tools keep the old rule (review of the id guard, 3) |
| A relative path with no MCP session (the panel's API legs) | `RuntimeError: session is not available …` | The same `Paths must start with '/'` hint (review, 7) |
| 500, transport errors | Unchanged | Unchanged. Nothing for the caller to fix. |
| Empty result | Not applicable: a file or directory always has attributes | |
| *Rows added after gate 4 (2026-11-23), for the operator's decisions below. The description is unchanged, so the surface hashes are; these are behaviour, not surface.* | | |
| An id that is not one Onedata's encoder could have written (truncated, extended, doubled, not hex, a digit changed, a header spliced onto another file's payload), given **through the tool** | Sent; Onedata answered 403, `spaceNotSupportedBy`, 404 or 400, or, for a splice of the same length, acted on the file the payload names | Refused before any request, with no lookup: `'…' is not a complete file id; copy it exactly from the earlier answer, or give the path, /<space_name>/<path>.` The id must be as ctool's `file_id:build_objectid/2` writes it: the reserved bytes 0 and 4 zero, a payload of at most 320 bytes, the length byte equal to its length modulo 256, the CRC-16 the encoder computes (25.0 writes it and does not check it), and a payload `guid#<uuid>#<space id>`, `shareGuid#<uuid>#<space id>#<share id>` or the older Erlang term `{guid, Uuid, SpaceId}`, whose two binaries fit the payload. The space id is not empty; no part has a length cap. These checks apply to every form (review of the id guard, 1 and 2; they replace the length-byte check of 2026-11-23, which a same-length splice passed). A path is not checked, and a relative path keeps its `Paths must start with '/'` hint. |
| No access (403, `forbidden`, `eacces`, `eperm`) for an **id** given through the tool | `Not allowed to read the attributes of file id '…': the token has no access to it. Calling again will not help.` | `Not allowed to read the attributes of file id '…': the token has no access to it. If it is not the id of the file you mean, copy that file's id exactly from the earlier answer, or give the path, /<space_name>/<path>.` When one `GET /spaces` shows that the id's space is not one the token sees: ` The space in the id is not one this token sees at this provider.` For a **path** the row above stands. Until the review of the id guard it led with "the id may be mis-copied (an id with a missing or changed character is refused like this)"; such an id is now refused before any request, so an id that reaches this answer is one Onedata wrote, perhaps another file's (review of the id guard, 5). |
| `spaceNotSupportedBy` for an **id** given through the tool | `This provider does not support the space of file id '…', so it cannot read the attributes of it here; the space may still exist.` | `This provider does not support the space of file id '…', so it cannot read the attributes of it here; the space may still exist. If it is not the id of the file you mean, copy that file's id exactly from the earlier answer, or give the path, /<space_name>/<path>.` Then the unseen-space sentence when it applies, and the spaces the token sees. For a **path** the row above stands. (Reworded as the row above, review of the id guard, 5.) |

- The hints are keyed on the HTTP status, with the error id and errno as a fallback; a specific diagnosis needs distinguishing evidence (review, 5). The real 25.0 bodies are **unverified**: the unit tests use plausible shapes, and the live test prints the real ones.
- **Who gets the id handling.** Only the MCP tool: the API function takes `copied_id=True` from the tool's wrapper (`onedata_mcp/modules/files.py`). The other callers of the function (the stable `download_file`, the readiness tools) pass ids that Onedata gave them and keep their handling, which a test pins (`test_the_callers_that_pass_ids_from_onedata_keep_their_handling`).
- Paths are resolved with the three tools' own lookup. The stable tools keep `get_file_id`, whose shared mapping still reads `spaceNotSupportedBy` as a missing space; changing it would need their own gates.
- Gate 1 also asks for list caps: `attributes` was the tool's only list, and it had none.

## Gate 2: surface review (done, branch commit `a77989b`)

Checked against the protocol's rules; `test_the_description_follows_the_surface_rules` covers the first three:

- **Says what it returns:** it did not ("Get attributes for a file id or a logical path"). **Fixed:** it returns an object of the requested attributes, and without `attributes` the default set, now named. A model that needs the owner or a time other than `atime` and `mtime` must know it has to ask.
- **Names no tool that may not be offered alongside it:** yes. **Cites no document:** yes.
- **Says it works for directories:** it did not ("path to the file"). Fixed in the description and the field.
- **Errors tell the caller how to fix the call:** done in gate 1.
- **`make conformance`: passed** on the integrated server (the integration branch `feat/promotion-harness` at `501c867`, which carries all five packages), run 2026-11-21 (`conformance-results/20261121T010707Z`) with `@modelcontextprotocol/conformance@0.1.16`, the version `make conformance` now pins: 12 passed, 20 failed, every failure expected (`conformance-baseline.yaml`: scenarios that need prompts, resources, completion, sampling, elicitation or the suite's own test tools, none of which this server has), and the baseline check passed. Conformance tests the protocol, not one tool, so this one run serves design/11–15.
- **Reported, not changed:** two stable tools' texts name this tool: `get_file_metadata`'s description, and `list_files_recursively`'s `attributes` field ("Use the same allowed values as in get_file_attributes"). Under `ONEDATA_MCP_MATURITY=stable` they point to a tool that is not offered; promoting this tool fixes that for the stable tier. Changing their wording would cost them a smoke re-run.
- **Not stated:** the unit of the times. The swagger says seconds, but its examples have 13 digits. The live test prints a real value before anything is said.

## Gate 3: live check (built in branch commits `b7cd89a` and `ab05369`, not run)

[`test/live/test_files_live.py`](../test/live/test_files_live.py), section `get_file_attributes`, calls the tools through an in-process MCP client, as a model would, in a fresh probe directory under the live space. It covers:

1. A small file with its mode set to `600` (`PUT /data/{id}`, as the FA fixtures do): its attributes by path and by id; the mode must read back as `600` or `0600`. It prints the default attributes and `posixPermissions` as returned, and `creationTime`'s magnitude (seconds or milliseconds).
2. The attributes of a directory (`type` `DIR`), printed.
3. `attributes` given as JSON text reaches Onedata as a list.
4. The errors as the model reads them, printed: a missing path; a made-up id (32 hex digits); an unknown attribute name (`owner`; asserted to name it); a deprecated name (asserted to name its replacement).

Like the other live tests, it is skipped unless `ONEDATA_MCP_LIVE_FILES=1`, and then refuses a provider or zone host that is not `127.0.0.1`, `localhost` or `*.datahub.test`. Its probe directory is made exclusively (a taken name is retried, never adopted) and deleted by its id in a `finally`; a failed deletion fails the teardown (review, 4). The expected 25.0 failure contracts are asserted and marked `TODO(first live run)`. Here it was only collected, and skipped without the flag; run from a scratch plugin against the unit tests' fake federation, 9 of its 10 tests pass, the tenth because the fake accepts an unknown attribute name.

**To run:**

    make valfed-up                     # if the federation is not up
    make valfed-files-live             # in omcp-live, TLS verified, the token in the environment
    # or, with the ONEDATA_* variables and ONEDATA_MCP_LIVE_FILES_SPACE set by hand:
    make test-files-live

**And:** `make valfed-check` now also builds FA's fixture: modes set at the owner provider, and the build waits until the local provider shows them. A mode that never shows is a `FixtureError`.

**Results: TODO.** Record here:

- the form of `posixPermissions` (`"600"`, `"0600"`, or other). If it is not an octal string, change `fixtures.same_mode` and the fake before the run;
- the unit of the times;
- the error texts and statuses of the missing path, the made-up id and the unknown name. If the made-up id's status is not 400 or 404, or its error id is not `badValueIdentifier`, adjust the hint mapping and the unit tests' bodies before the run.

## Gate 4: validation run (scenarios built in branch commit `c69fb6b`; run 2026-11-22, results below)

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
| a task that needs it | **FA** | "Which of these files in `<dir>` has file permissions that differ from those of the others: log_20200101.dat, log_20200102.dat, log_20200103.dat, log_20200104.dat? Reply with its name, or with none if they all have the same permissions." | four small files, modes 644, 644, **600**, 644 | `log_20200103.dat` |
| "no, because" | **FAn** | FA's, word for word | the same four, all 644 | none |
| offered but not needed | **V1** (existing) | "Which of these files in `<dir>` are fully stored at your site …" | design/08 | the two locally stored files |
| id variant | **FAi** | "These are the file ids of four log files in `<dir>`: <id>, <id>, <id>, <id>. Which of these files has file permissions that differ from those of the others? Reply with its name, or with none …" | modes 644, **600**, 644, 644 | `log_20200102.dat`: the model must read each id's name and mode |
| the end state after a mutating tool's answer | not applicable: the tool reads only | | | |

- **Requires.** FA and FAi run on the K1 set (`benchmark/k2val/config.py`), which has no listing tool: `list_children` and `list_files_recursively` both return `posixPermissions` and are not offered. No other K1 tool reads a file's permissions (`get_file_distribution` gives type and size; `get_file_metadata` gives JSON, RDF and xattrs). A test pins this (`test_fa_is_offered_no_other_reader_of_permissions`).
- **Why permissions.** They are the one attribute a fixture can set (`PUT /data/{id}`, swagger `set_attr`) that no other K1 tool returns. Type and size are answered by `get_file_distribution`; the valfed layout has a single user, so the owner cannot tell files apart; times cannot be set.
- **Why a file name as the answer.** The answer reader then never has to parse a mode: the swagger's own examples disagree on the form (`"511"` for `posixPermissions`), and the comparison only needs equal or different.
- **The FA brief allows "none".** FA and FAn share it word for word, so the option of none is no hint, and a model that names a file without reading fails FAn.
- **Path variants.** The brief gives paths in the canonical form. A relative path and an empty or dot id are gate-1 error paths with hints, covered by unit tests.
- **The federation axis is `None`** (as for V1): the oracle judges no end state. But the K1 set offers write tools, so FA, FAi and FAn are restraint scenarios (design/08, "Scoring"): any successful write call fails the trial, and the tree (with each file's bytes) is compared with the baseline at the answer and 60 s later.
- **The offered set is checked before dispatch.** FA, FAi and FAn exclude `list_children` and `list_files_recursively`: a `[tools] k1` override, or a server, that would offer either is a HARNESS_ERROR before any model runs (external review, finding 9).
- **Required call.** FA, FAi and FAn need `get_file_attributes` to have RETURNED the mode (`posixPermissions`) of each of the four files: which one differs, or that none does, cannot be known otherwise, and a call that asked for other attributes only (its name, say) observes no permission. FAi's calls must pass the file ids the brief gave, not paths. (An interim relaxation to "the file that differs" alone was withdrawn after the second external review: reading one file's name and guessing it passed.)
- **Harness, shared with design/14 and 15** (branch commit `b068e71`): a scenario may name its own tool set; fixtures may be small files and directories (`EntrySpec`), made at the owner; **the trial starts only once the local provider shows them all as built, and that tree is recorded as the baseline** (`fixture_baseline` in the artefact); a brief may give a built entry's file id (`{ids[<path>]}`, rendered after the build). Branch commit `f770b7f`: the trial directory is `logs-<12 letters>`, so a path no longer names the scenario (`logs-v3n-…` did).

### How a trial is judged

- **Answer reader** (`file_oracles.fa_label`, one small function to be swapped for the rubric judge): a file name mentioned alone is the answer; otherwise the last sentence that names a file or says none decides; else "unclear". `test/unit/k2val/test_scenarios_fa.py` reads a table of real-sounding answers per label: names in prose, tables, enumerations with a conclusion, negations ("do not differ; X does"), "none of the others", "zero files", "all four have identical permissions". A known limit is pinned there too: an enumeration with no conclusion is read by its last line.
- **Pass:** the reading is the expected name (or none), as a name set both readers share (`file_oracles.fa_names`). **Fail:** anything else, "unclear" included. Since the integration, the shared checks of design/08's "Scoring" also apply: a pass needs the right answer and every check that applies; the answer is read by this reader and by the judge, and a disagreement is NEEDS_REVIEW.

### The run

- Panel: the six standing configs, with `sonnet-medium` as the control and `haiku-low`, `glm`, `qwen36`, `qwencoder` and `deepseek`. Each config has its own space.
- K = 8. That is 4 scenarios × 6 configs × 8 = 192 trials.

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_SCENARIOS=FA,FAi,FAn,V1 K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

The report lands in `research/k2val-<run id>.md`.

### Results

Run 2026-11-22 on master (begun at `216d450`, resumed at `c99883e` and `8863c3e`; the resumes changed only the orchestration, and the FA and V1 scoring is the same at all three), together with design/14's and design/15's scenarios. Run ids `files-k8-sonnet-medium-glm`, `files-k8-haiku-low-qwen36` and `files-k8-qwencoder-deepseek`, in the durable evidence store (outside any worktree). The run holds 1278 records for its 624 planned trials: 654 are superseded infrastructure records (634 RESET_FAIL, 20 ORACLE_ERROR), each followed by a valid record of the same trial. The resumed trials ran on a re-created federation (new provider and space ids, the same layout); each trial's record names its own. The offered surface every counted FA, FAi, FAn and V1 trial saw is K1, `9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3`, as above. Answers were read by the deterministic reader and the judge (Claude Sonnet 5.5, effort medium).

This tool's 192 trials, as stored: 152 PASS, 14 FAIL, 26 NEEDS_REVIEW. Re-scored offline with the reader fixes below and the stored verdicts (`rejudge --cached-only`): 175 PASS, 14 FAIL, 3 NEEDS_REVIEW. All three fail under either reading.

PASS per config and scenario, after classification (K=8):

| Config | FA | FAi | FAn | V1 |
|---|---|---|---|---|
| sonnet-medium (control) | 8/8 | 8/8 | 8/8 | 8/8 |
| haiku-low | 8/8 | 8/8 | 8/8 | 8/8 |
| glm | 8/8 | 6/8 | 8/8 | 8/8 |
| qwen36 | 8/8 | 4/8 | 8/8 | 8/8 |
| qwencoder | 6/8 | 2/8 | 8/8 | 5/8 |
| deepseek | 8/8 | 8/8 | 8/8 | 8/8 |

175 of 192 pass. Pass rates are reported; they do not gate. The control passes all 32.

- **Classifications:**
  - **Harness (oracle) bug, fixed and re-scored: the FA reader (23 NEEDS_REVIEW, now PASS; no stored PASS changed).** The judge read all 23 rightly. Three answer shapes:
    - the odd file named first, then the others listed in a closing clause: "**log_20200103.dat** — it has permissions `600`, while the other three files (log_20200101.dat, log_20200102.dat, log_20200104.dat) all have `644`." (GLM FA #0–#4, #6; FAi #0–#3);
    - "The file with different permissions is **log_20200102.dat**, which has mode 600. The other three (…) all have mode 644." (Haiku FAi #0, #1; DeepSeek FAi #2–#5; Qwen3.6 FAi #2);
    - "None of them differ. All four files (…) have POSIX permissions 644." (Haiku FAn #1–#4, #6, #7).

    The reader now takes "with different permissions" as naming the odd file, and "the other(s) … all have <mode>" as a contrast when that mode differs from the named file's. It no longer lets the "differ" of "none of them differ" cancel the none. Paired cases in `test/unit/k2val/test_gate4_readers.py`.
  - **Model capability (L3b), logged, not acted on:**
    - **FAi, Qwen3-Coder #1, #3, #4, #5, #7: no name.** It read each id's `posixPermissions` (the JSON-text `attributes` now accepted) but never asked for `name`, then answered "the second file" (#4, #7) or that it had "no way to retrieve the filename from the ID" (#1). The description says the default answer includes `name`, and the field lists it. In #2 the same model asked for `name` by id. Nothing in the surface invites it. #3, #4 and #7 are NEEDS_REVIEW (the readers disagree on how to read an answer with no name); they FAIL under either reading.
    - **FAi, Qwen3-Coder #2:** names by id, then modes by path. The answer is right, but no mode came back from a call by the ids given, which FAi requires (design/13, "Required call"). This is the id form of `file_id_or_path` not used for the modes, not a failure of it. It stays FAIL.
    - **FAi, a mis-copied id (Qwen3.6 #0, #5, #6, #7; GLM #6, #7).** Each passed one or more ids with characters dropped or changed, and never re-copied it. Qwen3.6 #0 and #7 lost all four ids and answered "none". #5 and #6 lost one and guessed it. GLM read the lost file by path instead (#6), or passed another file's id in its place (#7), so a mode by the id given is missing. See "Open: the hints on a mis-copied id", below.
    - **FA, Qwen3-Coder #3, #5:** read three files' modes and answered without the fourth (`required_call_missing`), rightly as it happens.
    - **V1 (offered, not needed), Qwen3-Coder #4, #5, #7.** #4 never called `get_file_distribution` yet answered with "physicalSize: 0 at local provider" for each file: a fabrication. #5 and #7 read `get_file_qos_summary`'s `"status": "fulfilled"` (with no requirements) as "fully stored at the local site". That is a misreading of a stable tool, logged for its smoke re-run (design/16). None misused this tool.
- **Tool-attributable findings: none.**
- **The watch item.** Qwen3-Coder sent `attributes` as JSON text in 125 calls. None was refused for it; the 13 that failed passed `<dir>/<file id>` as a path.
- **Open: the hints on a mis-copied id (a gate-1 defect, not L3a).** 35 calls in the two runs passed an id that was not the brief's (Qwen3.6, GLM and Haiku). Onedata 25.0 answers such an id with `403 forbidden`, `400 spaceNotSupportedBy`, `400 badValueIdentifier` or a `400` posix error, and two of these become hints that name a cause the response does not establish:
  - "Not allowed to read the attributes of file id '…': the token has no access to it. Calling again will not help." (Qwen3.6 FAi #7, four calls; GLM FAi #7);
  - "This provider does not support the space of file id '…' …; the space may still exist." (Qwen3.6 FAi #0, five calls; Haiku FAi #1).

  For a mis-copied id, "Calling again will not help" is false, and gate 2 asks errors to tell the caller how to fix the call. The failures are consistent with the misleading hints, but the trials do not show the hints caused them:
  - Qwen3.6 fails just the same after the accurate hint ("This provider reports nothing at file id '…': … or the value is not a file id", FAi #5, #6). It never re-copies an id, and its "none" in #0 and #7 follows both kinds of hint alike;
  - Haiku re-copied the id after the misleading hint and passed (FAi #1);
  - GLM re-copied after the accurate hint and passed (FAi #4). After the misleading one (FAi #7) it retried the same wrong id once, then read by path.

  It is therefore recorded as a gate-1 defect, not L3a. **Operator's call:** under the bar's third clause (one model with a traced mechanism), Qwen3.6's trace (id sent → `403` or `spaceNotSupportedBy` → the hint → its answer) could be read as L3a instead. That reading would block this promotion until the fix below is made and FAi re-run.

  The fix:
  - refuse an id that is not well formed before any request: here, 177 and 179 hex digits, which do not even decode; or one that does not decode to `guid#<uuid>#<space id>`;
  - for a well-formed id whose space is not one the token sees, say the id may be mis-copied, instead of claiming no access or an unsupported space.

  `schedule_file_replication` (design/12) and `list_children` (design/14) share the defect. A well-formedness check does not catch everything: GLM FAi #7 spliced one brief id's 8-byte header onto another file's `guid#…` payload, and Onedata returned that other file, so the header is not checked. The fix changes errors only, not the surface; FAi should be re-run on the smoke pair at K=3 after it.

  - **Fixed 2026-11-23 (operator's decision), with the code on this branch.** The two parts of the fix above, for an id given through the tool (gate 1, the rows added after gate 4). The check is a stricter test than "decodes to `guid#<uuid>#<space id>`": it also reads the header's sixth byte as the payload's length, which is what shows a byte dropped from an otherwise well-formed id. Over the 383 distinct ids that 25.0 returned in the validation runs, trash and archive roots included, it refuses none. The recorded calls of the three tools passed 35 distinct id values (in 55 calls) that were not an id Onedata or a brief had given; it refuses 33. The other 2 are whole in shape: 180 digits with one changed digit, in the header's checksum bytes in one and in the uuid in the other, and they are Onedata's to refuse. It also accepts the two other id shapes that the 25.0 swagger and its README show: the older id whose payload is the Erlang term `{guid, Uuid, SpaceId}` (the README's example; its length byte is checked), and the id of a file seen through a share, `shareGuid#<uuid>#<space id>#<share id>` (the swagger shows only its first characters, so its layout is unverified and its length byte is not checked). The swagger's other long hex strings that read as ids are examples cut short, and are refused as incomplete. The id handling is only for the tool's own calls (gate 1). Unit tests: `test/unit/api/test_file_ids.py` and the mis-copy cases of `test_get_file_attributes.py`, `test_list_children.py` and `test_transfers.py`. The FAi re-run is a TODO below. **Tightened 2026-11-24** after an external review: the two ids "whole in shape" above are refused now, by the CRC, with a header spliced onto another file's payload; see "Review of the id guard".
- **The run passes when** (design/10, gate 4): every control trial passes, with control trials lost to a harness bug or deployment artefact re-run after the fix, so each scenario ends with K passing control trials; and no tool-attributable finding is open (fixed and re-run, or accepted only with its cause outside the tool and the operator's sign-off).

**Verdict: PASSES gate 4 as recorded, pending the FAi re-run after the id guard (below).** The control passes all 32 trials and no tool-attributable finding is open. The hint on a mis-copied id is a gate-1 defect, not L3a (operator's call, 2026-11-23); it is fixed (above), and FAi is re-run on the smoke pair at K=3 before the promotion PR.

### Re-run after the 2026-11-23 decisions (TODO, not run)

Nothing here has run. Before starting, check `$VALFED_HOME/HOLD` and the study coordinator's limits: the federation is on a shared host. `K2VAL_ARTEFACT_ROOT` (new in the Makefile) passes `--artefact-root`. The panel and K are this record's reading of "the smoke pair at K=3" (design/10, "The standing panel"; design/10, "Staying stable"): Qwen3-Coder and Haiku at effort low. The operator did not fix them for this re-run.

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_ARTEFACT_ROOT=$VALFED_HOME/evidence/k2val \
    K2VAL_RID=fa-guard-k8 \
    K2VAL_SCENARIOS=FAi K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

- **FAi at K=8 on the full panel (the mis-copied-id guard).** 1 scenario × 6 configs × 8 = 48 trials, run in batches of two configs as the main run was. The operator's reading (2026-11-23): the full panel and K=8, not the smoke pair, because the mis-copies came from Qwen3.6 and GLM, which the smoke pair lacks, and because the record's FAi row then compares like with like. Passes when every control trial passes and no new tool-attributable failure appears: an id that was not the brief's is refused with "not a complete file id", or reaches Onedata and draws a hint that says the id may be mis-copied.

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
| Codex 12 | A file lookup answering 500 with `enoent` became `Path ... not found`; a 5xx could also get "not allowed", "does not support the space" or "a component is not a directory" | fixed, `9658ffa`: forbidden, not-found, `enotdir` and unsupported-space diagnoses need a 4xx status. A 5xx that mentions a missing item gets "the provider failed, and the target may also not exist; that does not show which", for an id and for a path (an `OnedataApiError`, not `FileNotFoundError`); any other 5xx passes unchanged |
| Codex 13; Gemini 8.1 | The heuristic read an attribute name such as `xattr.user.id` in an error as a host and replaced it | fixed, `c32a67f`: the heuristic is removed (see design/11, Round 2, with its residual) |
| Gemini 8.3 | A 3xx was a success | fixed, `c32a67f` |
| Codex 14 | The live file test passed any `*.datahub.test` name, even one resolving off-machine | fixed, `673a2c6`: the shared loopback guard and proxy isolation |
| Codex 1–11, 15; Gemini 1–6, 9, 10 | Harness | not changed here |

## Review of the id guard (2026-11-24)

An external review (Codex) of the guard on a mis-copied id (PR #46), shared by this tool, `list_children` (design/14) and `schedule_file_replication` (design/12). Each item was first reproduced as a failing unit test on master; then fixed in code and unit tests only (branch commit `2830f3c`; no container, live test or model call). The surfaces are unchanged: every recorded set hashes as before.

| # | Finding | Status |
|---|---|---|
| 1 (blocking) | A header/payload splice of the same length passes, and 25.0 acts on the file its payload names (GLM FAi #7 above); `schedule_file_replication` would copy that file. A cut share id passes too | fixed: the id must be one ctool's encoder could write, CRC-16 included, for every form (gate 1, the rows after gate 4). All 534 distinct ids that 25.0 returned in the recorded validation runs (trash, archives and space roots included) pass; of the 52 distinct values beginning with the header's ten zeros that the models passed and 25.0 did not write (values with a digit lost from those zeros, or a space in them, are not counted), the check of 2026-11-23 accepted 4 (180 digits each: the splice and three ids with changed digits), and this one refuses all 52 |
| 2 | The 128-character cap per part refuses the trash and archives roots' ids under a longer `datastore_doc_key_length` (a 126-character space id gives 132- and 140-character uuids) | fixed: no cap per part; the encoder's 320-byte payload limit bounds them, and over 255 bytes the length byte is compared modulo 256. Unit-tested with such ids; **not verified live** with a non-default key length |
| 3 | The tools' wrappers took any name of 20 or more letters and digits for an id and skipped the MCP root; punctuation in a copied id sent it to path lookup | fixed for this tool and `list_children`: an id is told by its start (gate 1). Tested through an MCP client with a root mounted. `create_directory` and the stable tools keep `_resolve_with_mcp_root` as it is (their own gates) |
| 4 | The id a path lookup returned went into the next request percent-encoded (`ab/cd` as `ab%2Fcd`), past the shared segment guard | fixed: the request and the type probe of `list_children` use `_file_id_segment`; the copy check stays on the caller's ids |
| 5 | `schedule_file_replication`'s `spaceNotSupportedBy` hint offered the id and the target as equal causes, though the error names the provider; the 403 hints led with a copy; `api/files.py` said a missing character gives these answers | fixed: the replication hint leads with the error's `details.providerId` and `details.spaceId`, naming the target or the provider the request went to, and the id comes second as a condition; a 403 leads with the permission refusal (design/12's rows on these hints are not updated by this change). This tool's 403 and `spaceNotSupportedBy` hints for an id lead with the provider's refusal, and the copy is a condition after it (gate 1) |
| 6 | The replication tool's success note | handled with design/12, not here |
| 7 (blocking) | The records list the post-change panel runs as pending | not here: the results are recorded on another branch |

What ctool's `src/utils/file_id.erl` (at `14eb9ceff6a0`) says, and where it differs from the review:

- `build_objectid/2` writes a zero byte, the 3-byte enterprise number (0), a zero byte, `Length:8`, and a CRC-16 (polynomial 0x8005 reflected, initial value 0; check value 0xBB3D) over the id written with that field zero, big-endian; it refuses a payload over 320 bytes. The swagger's and its README's example ids carry that CRC.
- `objectid_to_guid/1` checks only the two zero bytes: not the enterprise number, the length byte or the CRC. The enterprise number is not checked on its own here either; the CRC covers it.
- `unpack_share_guid/1` reads only `guid#…` and `shareGuid#…`. The older Erlang-term id of the README is therefore refused by 25.0 itself; it is still accepted here (with its binaries bounded, as the review asks) so that no id Onedata documents is refused locally.
- op-worker 25.0's transfer validation checks that the provider the request went to supports the file's space, then that the target does, and `spaceNotSupportedBy` names the provider that failed in `details.providerId` (ctool's `od_error_space_not_supported_by` gives it with `spaceId`); the replication hint relies on that order.

## Gate 5: record

This file. To complete it:

1. Fill in the TODOs of gates 3 and 4: the live results and the panel's results.
2. Then, in the same PR, move `get_file_attributes` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, and change its tier in README's tool table. `test/unit/test_tool_maturity.py` lists it among the experimental tools; update that test in the same PR.

## Decisions for the operator

- **Run order and the K1 hash.** This promotion changes the K1 set's surface (`dc1d10dc…` → `21aef26a…`), which design/11 records as the surface for V3, V3n and V3o. Run design/11's gate 4 before this branch merges, or record its run against the new K1 hash. The parallel package of `schedule_file_replication` uses the K1 set too.
- **The API legs' direct call path.** For the `openai_compat` and `anthropic` legs the panel calls `app.call_tool` with no MCP session (the Claude SDK and CLI legs go through the loopback HTTP server). A relative path then made `ctx.list_roots()` raise a `RuntimeError` ("session is not available"), not the `Paths must start with '/'` hint a real client gets. **Fixed for the three file tools** (review, 7: they catch it and give the hint). The stable path-taking tools keep `_resolve_with_mcp_root` as it is; fixing them there needs their own gates.
