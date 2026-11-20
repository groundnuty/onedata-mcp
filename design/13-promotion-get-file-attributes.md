# Promotion record: `get_file_attributes`

Status: **in progress** (2026-11-20; external review addressed, see below). Gate 1 is done. Gate 2 is done except for `make conformance`, which has not been run. Gate 3's live test is built but has not been run. Gate 4's scenarios are built and unit-tested on the fake federation, but the run has not happened. The validation federation is on hold, so nothing here touched a live Onedata. The tool stays `experimental` until gates 2–4 are complete. It moves to `STABLE` in the PR that completes this record.

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
| `?`, `#` or `/` in an id | **Defect:** `?` and `#` cut the path short, so another file's attributes came back; `/` turned the call into another endpoint (`ab/children`) | The id is percent-encoded |
| An empty id, `.` or `..` | Sent: `/data/..` is the API root | Refused before any request: `Give a path, /<space_name>/<path>, or a file id.` |
| An id that names nothing, as a structured not-found (`notFound`, `enoent`) | Onedata's message only | `This provider reports nothing at file id '…': it may have been deleted, or the value is not a file id. Give a path, /<space_name>/<path>, or a file id.` Onedata's message follows. |
| A bare 404 (no structured body), by id or while resolving a path | Onedata's message only | Its possible causes, not one of them (review, 5): `This provider answered 404 for … without saying why: it may not exist, its space may not be supported by this provider, or the token may not see it. Check the path or id, the space and the token's access.` |
| A malformed id (400 `badValueIdentifier`, or a bad value with key `id`) | Onedata's message only | `This provider rejected file id '…' as a file id.` and the same advice |
| `spaceNotSupportedBy`, by id or while resolving a path | Onedata's message only | `This provider does not support the space of …, so it cannot read the attributes of it here; the space may still exist.` and the spaces the token sees. Before the review it said `Space "…" does not exist` (review, 5). It is an `OnedataApiError`, which the readiness tools' reads catch. |
| A component of the path is not a directory (`enotdir` while resolving) | Onedata's message only | `A component of path '…' before its last part is not a directory, so this provider cannot resolve the path.` (review, 6) |
| A path that names nothing | `Path "…" not found` | Unchanged |
| No access (403, `forbidden`, `eacces`, `eperm`) | Onedata's message only | `Not allowed to read the attributes of …: the token has no access to it. Calling again will not help.` |
| A relative path, client without MCP roots | `Paths must start with '/'` hint | Unchanged |
| A relative path with no MCP session (the panel's API legs) | `RuntimeError: session is not available …` | The same `Paths must start with '/'` hint (review, 7) |
| 500, transport errors | Unchanged | Unchanged. Nothing for the caller to fix. |
| Empty result | Not applicable: a file or directory always has attributes | |

- The hints are keyed on the HTTP status, with the error id and errno as a fallback; a specific diagnosis needs distinguishing evidence (review, 5). The real 25.0 bodies are **unverified**: the unit tests use plausible shapes, and the live test prints the real ones.
- Paths are resolved with the three tools' own lookup. The stable tools keep `get_file_id`, whose shared mapping still reads `spaceNotSupportedBy` as a missing space; changing it would need their own gates.
- Gate 1 also asks for list caps: `attributes` was the tool's only list, and it had none.

## Gate 2: surface review (done except `make conformance`, branch commit `a77989b`)

Checked against the protocol's rules; `test_the_description_follows_the_surface_rules` covers the first three:

- **Says what it returns:** it did not ("Get attributes for a file id or a logical path"). **Fixed:** it returns an object of the requested attributes, and without `attributes` the default set, now named. A model that needs the owner or a time other than `atime` and `mtime` must know it has to ask.
- **Names no tool that may not be offered alongside it:** yes. **Cites no document:** yes.
- **Says it works for directories:** it did not ("path to the file"). Fixed in the description and the field.
- **Errors tell the caller how to fix the call:** done in gate 1.
- **`make conformance`: TODO.** It needs `npx` and a local port, and was not run here.
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

## Gate 4: validation run (scenarios built in branch commit `c69fb6b`, not run)

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
- **Required call.** A pass needs a successful `get_file_attributes` of each of the four files (by path or id): the odd one cannot be known without reading all four.
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

### Results: TODO

| Config | FA | FAi | FAn | V1 |
|---|---|---|---|---|
| sonnet-medium (control) | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| haiku-low | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| glm | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwen36 | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwencoder | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| deepseek | TODO /8 | TODO /8 | TODO /8 | TODO /8 |

Run id, commit and surface hash: TODO.

Classify every non-pass before anything counts: harness or oracle bug (fix and rescore), deployment artefact, tool-attributable, or model capability. An answer the reader misread is an oracle bug.

- **Classifications:** TODO. Check first whether Qwen3-Coder still sends `attributes` as JSON text: it should now succeed.
- **Tool-attributable findings, each fixed and re-run or accepted with a reason:** TODO.
- **The run passes when:** the control passes all four scenarios, or each control failure is shown to be a harness bug or an artefact; and no tool-attributable finding is left open.

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

## Gate 5: record

This file. To complete it:

1. Fill in the TODOs of gates 2 to 4: conformance, the live results and the panel's results.
2. Then, in the same PR, move `get_file_attributes` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, and change its tier in README's tool table. `test/unit/test_tool_maturity.py` lists it among the experimental tools; update that test in the same PR.

## Decisions for the operator

- **Run order and the K1 hash.** This promotion changes the K1 set's surface (`dc1d10dc…` → `21aef26a…`), which design/11 records as the surface for V3, V3n and V3o. Run design/11's gate 4 before this branch merges, or record its run against the new K1 hash. The parallel package of `schedule_file_replication` uses the K1 set too.
- **The API legs' direct call path.** For the `openai_compat` and `anthropic` legs the panel calls `app.call_tool` with no MCP session (the Claude SDK and CLI legs go through the loopback HTTP server). A relative path then made `ctx.list_roots()` raise a `RuntimeError` ("session is not available"), not the `Paths must start with '/'` hint a real client gets. **Fixed for the three file tools** (review, 7: they catch it and give the hint). The stable path-taking tools keep `_resolve_with_mcp_root` as it is; fixing them there needs their own gates.
