# Promotion record: `create_directory`

Status: **in progress** (2026-11-20). Gate 1 is done. Gate 2 is done except for `make conformance`, which has not been run. Gate 3's live test is built but has not been run. Gate 4's scenarios are built and unit-tested on the fake federation, but the run has not happened. The validation federation is on hold, so nothing here touched a live Onedata. The tool stays `experimental` until gates 2–4 are complete. It moves to `STABLE` in the PR that completes this record.

The short commit ids below are **branch commits** on `feat/promote-file-tools`. A squash merge leaves them unreachable from `master`. **The anchor is the surface hash.**

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md). The scenarios are in [`benchmark/k2val/file_scenarios.py`](../benchmark/k2val/file_scenarios.py), each with its brief, right answer, rules and expected end state. The model for this record is [`11-promotion-remove-qos-requirement.md`](11-promotion-remove-qos-requirement.md); [`13`](13-promotion-get-file-attributes.md) describes the harness shared by the three file tools.

## The tool

- **REST call:** `PUT /data/{space root id}/path/{relative path}?type=DIR&create_parents=…` on the Oneprovider (swagger `create_file_at_path`, oneprovider-swagger tag `25.0`, commit `39da981`, `paths/data/id/path.yaml`). The space root's id comes from `POST /lookup-file-id/{path}` with the path `/<space>`. The swagger lists 201, 400, 403, 404 and 500, with the generic `Error` body; "If the file already exists, the operation fails with an error."
- **Input:** `path`; `create_parents`, a boolean (default true). No list input, so no cap applies.
- **Mutating:** it is in `token_policy.WRITE_TOOL_NAMES` and annotated `destructiveHint: true`.
- **Code:** `onedata_mcp/modules/files.py` (surface) and `onedata_mcp/api/files.py` (call and errors).

## The surface being promoted

| Selection | Before (master `4047a56`) | Promoted surface (since branch commit `a9f3b12`) |
|---|---|---|
| `create_directory` alone | `593b7054e77159c611cda2fbb2b3225005b4b3a07cf5175ae102b588532c2515` | `6fc35c645a119106d0d98a28925c54fe1b09fe4e8bd1b7198d4a84f41482001d` |
| FILE_TOOLS (6 tools; the CD scenarios run on it) | `43dd847ef84ddcd542278d3705a3b6e2affa3e769601545bc067da09fec6d114` | `3ace77c4063de4e764b36c3b245e06639cb6b8e1d72d0da340009fe1534a7299` (with design/13's and design/14's surfaces) |
| `create_file` alone (stable; not changed) | `a57ae5e4e4975118be448f7e59e414f643f9060dd4c01bd475b9fe8405c0f898` | the same |

- `a9f3b12` is the last commit that changes this tool's surface.
- The `tools/list` diff. The name, input schema (types, defaults) and annotations are unchanged; only text changed:
  - description: `Create a new DIRECTORY at the given path. Returns {fileId, path} of the created directory. Maps to PUT /data/{space_id}/path/{relative}?type=DIR (Oneprovider 25.0). Raises FileExistsError if the path already exists. Use this whenever you need a directory — never call create_file with empty content for that purpose. See research/empirical-mcp-server-findings.md M-11.` → `Create a directory at the given path. Returns {fileId, path} of the new directory. By default, missing parent directories along the path are created too. Fails if anything already exists at the path; the error says whether it is a directory or a regular file.`
  - `path`: `Path to the directory in format /<space_name>/<path_to_directory>` → `Path of the new directory, /<space_name>/<path_to_directory>`.
  - `create_parents`: `Create missing intermediate directories along the path. Defaults to True.` → `Create missing parent directories along the path. Defaults to true.`
- **Earlier evidence:** none counts. The one call in the K=8 run came from GLM in A4, where the tool was not offered (the triage, 2026-11-20).

**Before the gate-4 run**, check that the surface has not moved:

    ONEDATA_MCP_TOOLS=create_directory make -s tools-manifest | sha256sum   # expect 6fc35c64...

## Gate 1: implementation (done, branch commit `a9f3b12`)

The tests are in `test/unit/api/test_create_directory.py`, through the API and through an MCP client.

| Path | Before | Now |
|---|---|---|
| A `..`, `.` or empty component (`/s/a/../b`, `/s/a//b`) | **Defect:** sent as is, and the HTTP client resolves `a/../b` to `b`: **the directory was made at another path**, `/s/b`. `/s/a/..` became `…/path` with no name. | Refused before any request: `'…' has an empty, '.' or '..' component. Give the new directory's full path, /<space_name>/<path_to_directory>.` |
| `?`, `#`, a space or `%` in a name | Encoded already (`quote(…, safe="/")`) | Unchanged, now tested; the space root's id is encoded too |
| The space root (`/s`) | `… To target the space root, use the existing namespace tools instead — the root always exists.` (vague; names no tool) | `'/s' is the root directory of a space, which always exists. Give the new directory's full path, …` |
| An empty path | The space-root error | `The path is empty. …` |
| Something already exists at the path (`eexist`, or 409) | **Defect:** `Directory '…' already exists`, **also when a regular file is there**, so a model would report a directory that does not exist | It looks up what is there: `'…' already exists as a directory, so nothing was created.` / `… as a regular file, so no directory was created.` / `… as a symbolic link …`; when the lookup fails, `Something already exists at '…', so no directory was created.` (CDn depends on this.) |
| A regular file on the path (`enotdir`) | Onedata's message only | `A component of '…' above the new directory is a regular file, not a directory, so nothing was created.` |
| A missing parent with `create_parents` false (`enoent`, 404) | Onedata's message only | `The parent directory of '…' does not exist, so nothing was created. Create it first, or call again with create_parents set to true.` |
| A space that does not exist | `Path "/s" not found` (says nothing about a space) | `No space named 's' is visible here, so nothing was created. Available spaces: …` |
| No access (403, `forbidden`, `eacces`, `eperm`) | Onedata's message only | `Not allowed to create '…': the token may be read-only or have no write access in this space. Calling again will not help.` |
| A relative path, client without MCP roots | `Paths must start with '/'` hint | Unchanged |
| 500, transport errors | Unchanged | Unchanged |
| Success (201) | `{fileId, path}` | Unchanged |

The 25.0 error bodies are unverified, as in design/13; the live test prints them.

## Gate 2: surface review (done except `make conformance`, branch commit `a9f3b12`)

`test_the_description_follows_the_surface_rules` covers the first three:

- **Says what it returns:** yes (`{fileId, path}`); now also what happens when something exists at the path, and that parents are made by default.
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

## Gate 3: live check (built in branch commit `daf6452`, not run)

[`test/live/test_files_live.py`](../test/live/test_files_live.py), section `create_directory`, in a fresh probe directory:

1. A directory is made; its `fileId` matches a lookup and its type is `DIR`. Creating it again fails with `already exists as a directory` (the error printed).
2. **CDn's state:** a regular file at the path; the error must say `already exists as a regular file, so no directory was created`, and the file must still be a file.
3. A nested path with a missing parent (`2020/01`) is made with both levels `DIR`; a missing parent with `create_parents` false fails with the parent hint (printed, since the hint keys on an `enoent` the swagger does not pin); a regular file on the path fails (printed).
4. A name with `?` and `#` is made with those characters (or Onedata's refusal is printed).

Gated and guarded as design/13 describes; run with `make valfed-files-live` or `make test-files-live`. `make valfed-check` now also builds CDn's fixture (a regular file named `archive`).

**Results: TODO.** Record the error texts of the second creation, the file in the way, the missing parent and the file on the path. If 25.0 answers "exists" with something other than `eexist` or 409, or a missing parent with something other than `enoent` or 404, change the mapping and the unit tests' bodies before the run.

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

## Gate 5: record

This file. To complete it: fill in the TODOs of gates 2 to 4; then, in the same PR, move `create_directory` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, change its tier in README's tool table, and update `test/unit/test_tool_maturity.py`, which lists it as experimental.

## Decisions for the operator

- **`create_file`'s pointer** (gate 2): leave it, or reword it and re-run its scenarios on the smoke pair at K=3.
- **The PASS rule.** The runner records PASS from the answer axis alone. The CD oracle already fails the answer axis when the end state is not there at the answer, so a false "created" is a FAIL today; the harness fix that requires both applicable axes will also fail a trial whose end state changes after the answer.
