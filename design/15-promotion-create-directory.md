# Promotion record: `create_directory`

Status: **in progress** (2026-11-20; external review addressed, see below). Gate 1 is done. Gate 2 is done except for `make conformance`, which has not been run. Gate 3's live test is built but has not been run. Gate 4's scenarios are built and unit-tested on the fake federation, but the run has not happened. The validation federation is on hold, so nothing here touched a live Onedata. The tool stays `experimental` until gates 2–4 are complete. It moves to `STABLE` in the PR that completes this record.

The short commit ids below are **branch commits** on `feat/promote-file-tools`. A squash merge leaves them unreachable from `master`. **The anchor is the surface hash.**

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md). The scenarios are in [`benchmark/k2val/file_scenarios.py`](../benchmark/k2val/file_scenarios.py), each with its brief, right answer, rules and expected end state. The model for this record is [`11-promotion-remove-qos-requirement.md`](11-promotion-remove-qos-requirement.md); [`13`](13-promotion-get-file-attributes.md) describes the harness shared by the three file tools.

## The tool

- **REST call:** `PUT /data/{space root id}/path/{relative path}?type=DIR&create_parents=…` on the Oneprovider (swagger `create_file_at_path`, oneprovider-swagger tag `25.0`, commit `39da981`, `paths/data/id/path.yaml`). The space root's id comes from `POST /lookup-file-id/{path}` with the path `/<space>`. The swagger lists 201, 400, 403, 404 and 500, with the generic `Error` body; "If the file already exists, the operation fails with an error."
- **Input:** `path`; `create_parents`, a boolean (default true). No list input, so no cap applies.
- **Mutating:** it is in `token_policy.WRITE_TOOL_NAMES` and annotated `destructiveHint: true`.
- **Code:** `onedata_mcp/modules/files.py` (surface) and `onedata_mcp/api/files.py` (call and errors).

## The surface being promoted

| Selection | Before (master `4047a56`) | Promoted surface (since branch commit `49370ff`) |
|---|---|---|
| `create_directory` alone | `593b7054e77159c611cda2fbb2b3225005b4b3a07cf5175ae102b588532c2515` | `e9ff811829dca5ae33f89a93b77926c8a49c9aa6c76e11532f51788c6341211f` |
| FILE_TOOLS (6 tools; the CD scenarios run on it) | `43dd847ef84ddcd542278d3705a3b6e2affa3e769601545bc067da09fec6d114` | `c030429d0e04de218f50a1bda935d17e84026c8c24ea1a61fd6d91b0bac31922` (with design/13's and design/14's surfaces) |
| `create_file` alone (stable; not changed) | `a57ae5e4e4975118be448f7e59e414f643f9060dd4c01bd475b9fe8405c0f898` | the same |

- `49370ff` is the last commit that changes this tool's surface. Before the external review it was `a9f3b12`, with hash `6fc35c645a119106d0d98a28925c54fe1b09fe4e8bd1b7198d4a84f41482001d` (FILE_TOOLS `3ace77c4…`); the review's finding 6 made the promise of the existing entry's type conditional.
- The `tools/list` diff. The name, input schema (types, defaults) and annotations are unchanged; only text changed:
  - description: `Create a new DIRECTORY at the given path. Returns {fileId, path} of the created directory. Maps to PUT /data/{space_id}/path/{relative}?type=DIR (Oneprovider 25.0). Raises FileExistsError if the path already exists. Use this whenever you need a directory — never call create_file with empty content for that purpose. See research/empirical-mcp-server-findings.md M-11.` → `Create a directory at the given path. Returns {fileId, path} of the new directory. By default, missing parent directories along the path are created too. Fails if something already exists at the path; when the provider can tell, the error says whether it is a directory or a regular file.`
  - `path`: `Path to the directory in format /<space_name>/<path_to_directory>` → `Path of the new directory, /<space_name>/<path_to_directory>`.
  - `create_parents`: `Create missing intermediate directories along the path. Defaults to True.` → `Create missing parent directories along the path. Defaults to true.`
- **Earlier evidence:** none counts. The one call in the K=8 run came from GLM in A4, where the tool was not offered (the triage, 2026-11-20).

**Before the gate-4 run**, check that the surface has not moved:

    ONEDATA_MCP_TOOLS=create_directory make -s tools-manifest | sha256sum   # expect e9ff8118...

## Gate 1: implementation (done, branch commits `a9f3b12`, then `49370ff` and `40bd036` after the external review)

The tests are in `test/unit/api/test_create_directory.py`, through the API and through an MCP client.

| Path | Before | Now |
|---|---|---|
| A `..`, `.` or empty component (`/s/a/../b`, `/s/a//b`) | **Defect:** sent as is, and the HTTP client resolves `a/../b` to `b`: **the directory was made at another path**, `/s/b`. `/s/a/..` became `…/path` with no name. | Refused before any request: `'…' has an empty, '.' or '..' component. Give the new directory's full path, /<space_name>/<path_to_directory>.` |
| `?`, `#`, a space or `%` in a name | Encoded already (`quote(…, safe="/")`) | Unchanged, now tested; the space root's id is encoded too |
| A relative path (`reports/2020`), left as given by a client with an empty list of roots | **Defect** (review, 7): it reached the API, and its first component was read as a space, so it could make `/reports/2020` | Refused before any request: `Paths must start with '/': use /<space_name>/<path_to_directory> (got 'reports/2020').` |
| A first component that is not a space name (`//x/y`, `/./y`, `/../y`) | Sent | Refused: `'…' does not start with a space name. …` |
| The space root (`/s`) | `… To target the space root, use the existing namespace tools instead — the root always exists.` (vague; names no tool) | `'/s' is the root directory of a space, which always exists. Give the new directory's full path, …` |
| An empty path | The space-root error | `The path is empty. …` |
| Something already exists at the path (`eexist`, or 409) | **Defect:** `Directory '…' already exists`, **also when a regular file is there**, so a model would report a directory that does not exist | With `eexist`, or a 409 whose path then resolves, it says what is there: `'…' already exists as a directory, so nothing was created.` / `… as a regular file, so no directory was created.` / `… as a symbolic link …`. With `eexist` and no type read: `Something already exists at '…' (eexist), so no directory was created.` A 409 that nothing explains is not called an existing entry (review, 6): `This provider refused to create '…' with a conflict (409) and did not say why, so no directory was created. Check what is at the path.` (CDn depends on the first.) |
| A regular file on the path (`enotdir`) | Onedata's message only | `A component of '…' above the new directory is not a directory, so nothing was created.` (`enotdir` does not say it is a regular file.) |
| A missing parent with `create_parents` false, as a structured not-found (`enoent`, `notFound`) | Onedata's message only | `The parent directory of '…' does not exist, so nothing was created. Create it first, or call again with create_parents set to true.` |
| A bare 404 (no structured body) on the creation | Onedata's message only | **Not** read as a missing parent (review, 5): `This provider answered 404 for '…' without saying why: a parent may not exist, its space may not be supported by this provider, or the token may not see it. Check the path or id, the space and the token's access. Nothing was created.` |
| `spaceNotSupportedBy` | Onedata's message only | `This provider does not support the space of '…', so nothing was created here; the space may still exist.` and the spaces the token sees |
| A space this provider reports missing (a structured not-found on `/s`) | `Path "/s" not found` (says nothing about a space) | `No space named 's' is visible at this provider, so nothing was created. Spaces this token sees at this provider: …`. A bare 404 or `spaceNotSupportedBy` on that lookup is not called a missing space. |
| No access (403, `forbidden`, `eacces`, `eperm`) | Onedata's message only | `Not allowed to create '…': the token may be read-only or have no write access in this space. Calling again will not help.` |
| A relative path, client without MCP roots | `Paths must start with '/'` hint | Unchanged |
| A relative path with no MCP session (the panel's API legs) | `RuntimeError: session is not available …` | The same `Paths must start with '/'` hint (review, 7) |
| 500, transport errors | Unchanged | Unchanged |
| Success (201) | `{fileId, path}` | Unchanged |

The 25.0 error bodies are unverified, as in design/13; the live test prints them. Paths are resolved with the three tools' own lookup, so the stable tools keep the shared resolver unchanged.

## Gate 2: surface review (done except `make conformance`, branch commits `a9f3b12` and `49370ff`)

`test_the_description_follows_the_surface_rules` covers the first three:

- **Says what it returns:** yes (`{fileId, path}`); now also what happens when something exists at the path (the existing entry's type only "when the provider can tell", review finding 6), and that parents are made by default.
- **Names no tool that may not be offered alongside it:** it named `create_file`, which is not offered under `ONEDATA_MCP_MATURITY=experimental` or a hand-written selection. **Fixed:** removed.
- **Cites no document the model cannot read:** it cited `research/empirical-mcp-server-findings.md M-11`, and the REST call and a Python exception name. **Fixed:** removed.
- **Errors tell the caller how to fix the call:** done in gate 1.
- **`make conformance`: TODO.**

### Stable `create_file` points to this tool (reported; not changed)

`create_file`'s description ("Use `create_directory` instead") and its directory-intent error ("Use create_directory(…) instead") name this tool. Per design/10 a tool must not name one that may not be offered alongside it. Today that is broken in three places:

- under `ONEDATA_MCP_MATURITY=stable`, where this tool, being experimental, is not offered;
- in the PPAM benchmark's HEADLINE set, which offers `create_file` without `create_directory` (the triage traced GLM's unoffered call in A4 to this pointer);
- under any hand-written `ONEDATA_MCP_TOOLS` without it.

Promoting this tool fixes the first. The second is the frozen benchmark's own record and stays as it is. Rewording `create_file` (a stable tool) costs it a smoke re-run (design/10, "Staying stable") and is **not done here**, as asked. The CD scenarios offer both tools, so the pointer is valid in them.

## Gate 3: live check (built in branch commits `daf6452` and `ab05369`, not run)

[`test/live/test_files_live.py`](../test/live/test_files_live.py), section `create_directory`, in a fresh probe directory:

1. A directory is made; its `fileId` matches a lookup and its type is `DIR`. Creating it again fails with `already exists as a directory` (the error printed).
2. **CDn's state:** a regular file at the path; the error must say `already exists as a regular file, so no directory was created`, and the file must still be a file.
3. A nested path with a missing parent (`2020/01`) is made with both levels `DIR`; a missing parent with `create_parents` false fails with the parent hint (printed, since the hint keys on an `enoent` the swagger does not pin); a regular file on the path fails (printed).
4. A name with `?` and `#` is made with those characters (or Onedata's refusal is printed).

Gated and guarded as design/13 describes; run with `make valfed-files-live` or `make test-files-live`. The probe directory is made exclusively and deleted by its id (review, 4). The expected failure contracts (a missing parent, a file on the path) are asserted and marked `TODO(first live run)`. `make valfed-check` now also builds CDn's fixture (a regular file named `archive`).

**Results: TODO.** Record the error texts of the second creation, the file in the way, the missing parent and the file on the path. If 25.0 answers "exists" with something other than `eexist` or 409, or a missing parent with something other than a structured not-found (`enoent`, `notFound`), change the mapping and the unit tests' bodies before the run: the hints claim only what such evidence establishes.

## Gate 4: validation run (scenarios built in branch commit `cfe684e`, not run)

### Scenario coverage (design/10)

| The protocol asks for | Scenario | Brief | Fixture | Right answer and end state |
|---|---|---|---|---|
| a task that needs it | **CD** | "Create an empty directory `<dir>/archive`." | one small file, `log_20200101.dat` | created; the tree is the baseline plus `archive` (DIR), empty |
| "no, because" | **CDn** | CD's, word for word | `log_20200101.dat`, and a **regular file** named `archive` | not created, because a file has the name; the tree unchanged |
| offered but not needed | **CDo** | "Create the file `<dir>/reports/summary.txt` containing the text `ok`." | `log_20200101.dat` | created; the tree is the baseline plus `reports` (DIR) and `reports/summary.txt` (REG, text `ok`) |
| path variant | **CDp** | "Create an empty directory `<dir>/2020/01`." | `log_20200101.dat` | created; the baseline plus `2020` (DIR) and `2020/01` (DIR), empty |
| the end state after the final answer | all four | | | checked at the answer and after it, against the baseline |

- **Requires.** CD and CDp offer FILE_TOOLS (`create_directory, create_file, download_file, get_file_attributes, list_children, list_files_recursively`). `create_file` cannot make an empty directory: with empty content and no extension it refuses (directory intent); a file written under `archive/` makes `archive` but not empty; a file at `archive` is a regular file. **`delete_file` and `move_file` are withheld on purpose:** with them, writing a file under the new directory and deleting it (or moving it out) would also leave an empty directory, and CD would not require this tool. A test pins the set.
- **CDn** keeps CD's brief, so a model that claims a creation without looking fails. Making the directory under another name (`archive2`) is an unrequested change and fails both axes.
- **CDo** is done by one `create_file` call (its `create_parents` defaults to true). Using this tool for the file's path makes a DIRECTORY named `summary.txt`, and fails. Creating `reports` first with this tool, then the file, is right.
- **CDp** tests the nested path and `create_parents`' default.
- **No id variant:** the tool takes a path only.

### How a trial is judged

- **Answer reader** (`file_oracles.cd_label`, one small function to be swapped for the rubric judge): "not_created" when the answer says something is already there, it could not or did not create, it failed or was unable; else "created" when it says created, made, done, now exists, succeeded; else "unclear". `test/unit/k2val/test_scenarios_cd.py` reads a table of real-sounding answers per label, among them "I created it; it did not exist before" (created) and "could not be created: a regular file named archive already exists" (not_created).
- **The end state** (`file_oracles.end_state`): the trial directory's tree at the local provider, against the baseline recorded before the agent started: exactly the scenario's `added` entries, each of its type; an added directory with nothing in it but other added entries; an added file with the expected text (stripped). Anything else added, removed, or changed (another id, type, size or mode at a path) is named in the diagnosis.
- **mcp:** the reader returns the expected label AND the tree at the answer is the expected end state. A "created" whose end state appears only after the answer is a **PREMATURE CLAIM**; a "created" with no directory at all fails both axes.
- **federation:** the tree after the answer is the expected end state. Creation is synchronous at the local provider, so no settle wait is set; the tree is read from the local provider, where the agent works.
- **An unreadable tree is never judged:** the trial is `ORACLE_ERROR`. The baseline is the tree the local provider showed before the agent started; a listing that fails while the fixture waits for it is retried until the timeout, then the trial is `RESET_FAIL`.
- **Teardown** removes the trial's own directory (made by the fixture, tracked by id), with whatever the agent made inside it. Something the agent made outside it, at a path the brief did not give, is not removed by the trial; the next run's sweep removes `logs-*` directories only.

### The run

- Panel: the six standing configs, `sonnet-medium` the control. K = 8: 4 scenarios × 6 configs × 8 = 192 trials.

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_SCENARIOS=CD,CDn,CDo,CDp K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

### Results: TODO

| Config | CD | CDn | CDo | CDp |
|---|---|---|---|---|
| sonnet-medium (control) | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| haiku-low | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| glm | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwen36 | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwencoder | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| deepseek | TODO /8 | TODO /8 | TODO /8 | TODO /8 |

Run id, commit and surface hash: TODO. Classify every non-pass before anything counts.

- **Classifications:** TODO. In CDn, an answer that reports "the directory already exists" (rather than a file) is tool-attributable only if the error the model got said so; the new error says which.
- **Tool-attributable findings:** TODO.
- **The run passes when:** the control passes all four, or each control failure is a harness bug or an artefact; and no tool-attributable finding is left open.

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

This file. To complete it: fill in the TODOs of gates 2 to 4; then, in the same PR, move `create_directory` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, change its tier in README's tool table, and update `test/unit/test_tool_maturity.py`, which lists it as experimental.

## Decisions for the operator

- **`create_file`'s pointer** (gate 2): leave it, or reword it and re-run its scenarios on the smoke pair at K=3.
- **The PASS rule.** The runner records PASS from the answer axis alone. The CD oracle already fails the answer axis when the end state is not there at the answer, so a false "created" is a FAIL today; the harness fix that requires both applicable axes will also fail a trial whose end state changes after the answer.
