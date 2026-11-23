# Promotion record: `check_readiness`

Status: **in progress** (2026-11-23). Gates 1 and 2 are done on this branch. Gate 3's live test exists and has new failure-path checks; it has not been re-run since the tool changed (TODO). Gate 4's scenarios are built and tested on the fake federation; the run has not happened (TODO). Gate 5's external review has not happened (TODO). The tool stays `experimental` until all five gates are complete; it moves to `STABLE` in the PR that completes this record. Nothing here touched a live Onedata, a container or a model.

The short commit ids below are **branch commits** on `feat/promote-readiness-tools`. A squash merge leaves them unreachable from `master`. **The anchor is the surface hash.** The commit an external agent study pins for these tools, `4791e83` (design/09), must stay reachable from `master`; this branch adds to `master` and rewrites nothing.

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md). The tool's spec is [`07-readiness-tools.md`](07-readiness-tools.md); its freeze and first panel, [`09-readiness-freeze-record.md`](09-readiness-freeze-record.md) and [`08`](08-readiness-validation-panel.md). `ensure_readiness`, which shares this tool's module and its `extent`, has its own record, [`18`](18-promotion-ensure-readiness.md). The scenarios are in [`benchmark/k2val/readiness_scenarios.py`](../benchmark/k2val/readiness_scenarios.py), each with its brief, right answer and rules; the oracles in `readiness_oracles.py`.

## The tool

- **REST calls (all reads):** the provider's id (`GET /configuration`); the provider's own list of spaces (`GET /spaces`); the dataset's id (`POST /lookup-file-id/{path}`); a recursive listing of the dataset (`GET /data/{id}/files`, 1000 per page); each file's block map (`GET /data/{id}/distribution`); Onezone's `online` for a provider holding blocks (`GET /providers/{id}`); the space's ongoing transfers (`GET /spaces/{id}/transfers`, then `GET /transfers/{id}`, at most 50); a placement rule this process made (`GET /qos_requirements/{id}`). Every id in a path is one encoded segment (`id_segment`).
- **Input:** `extent`, 1–20 items, each `{dataset, files?, date_ranges?}`.
- **Read-only:** `readOnlyHint: true`; not in `token_policy.WRITE_TOOL_NAMES` (a test pins both).
- **Code:** `onedata_mcp/modules/readiness.py` (surface) and `onedata_mcp/api/readiness.py` (logic, errors).

## The surface being promoted

| Selection | Before (master `e2697c2`) | Promoted surface (since branch commit `4b866d8`) |
|---|---|---|
| `check_readiness` alone | `e7131973180c59607fa31a6834fb26371fa83f902d86ac7d641a5220a78652a9` | `fc3573ef8dcf78f863e75bd906dfa2f0fac0c5ba4c3f4870db883228f32cf0ed` |
| the panel's K2 set (13 tools; V4–V7, RCo, REo) | `6f502cb473137d4e972434c039830d7388b3e87f181dcc0386e0d0747fbc141b` | `b61a0fe00c063fe734c3c91da76e01cf93746a8b4bc7e8d3d71c93d09f7ad7e4` |
| `RC_TOOLS` (5 tools; RC, RCn, RCp) | new | `338b1d18b4e89e9d4702224aed20ab4e6a58adc4572d12f8f27c34b6f74a0742` |
| `RE_TOOLS` (3 tools; RE, REn, REp; design/18) | new | `26845b029051b181e246ff4724c38944ed3f8997c6f89d9dd6f60d387498c02b` |
| the K1 set, FILE_TOOLS, ST7_TOOLS | unchanged | unchanged |

`4b866d8` is the last commit that changes either readiness tool's surface; the merge of `master` (#50) after it changed no surface (the K2 hash was checked on the merged tree). Each set's hash is recorded in `benchmark/k2val/surfaces.py`, which the runner checks before every trial.

**The `tools/list` diff.** The name and annotations are unchanged.

- description: `… Describe the data as `extent`: for each dataset (a space name such as 'my-dataset'), optionally the files …` → `… for each dataset (a space name such as 'my-dataset', or a directory path inside it), optionally the files …`. The rest is unchanged.
- `extent`'s description: `The data you need: one item per dataset` → `The data you need: one item per dataset. Example: [{"dataset": "my-dataset", "files": ["data_20180801.nc"]}, {"dataset": "/my-dataset/sub/dir", "date_ranges": [{"start": "2018-06-01", "end": "2018-08-31"}]}]` (decision 1).
- schema: each extent item and each date range gains `"additionalProperties": false` (gate 1, below).

**Before the gate-4 run**, check that the surface has not moved:

    ONEDATA_MCP_TOOLS=check_readiness make -s tools-manifest | sha256sum   # expect fc3573ef...

## Gate 1: implementation (done, branch commits `0befafc`, `4b866d8`, `b070c8b`)

The tool already had 162 API and 7 wrapper tests (design/09). The model-triggerable paths below were added or changed; the tests are in `test/unit/api/test_readiness.py` (section "Gate 1") and `test/unit/modules/test_readiness_wrapper.py`, through the API and through an MCP client.

| Path | Before | Now |
|---|---|---|
| An extent item with a field it does not have (`{dataset, paths, date_from}`, `file`, a range's `from`/`to`) | **Defect:** dropped silently. The item was left with neither `files` nor `date_ranges`, which means the whole dataset: the answer was about other data than declared (and `ensure_readiness` held the whole dataset) | Refused before any request, by name: the schema says `additionalProperties: false`, and the API says `extent[0] has no field 'date_from', 'paths': its fields are dataset, files, date_ranges` |
| A dataset with an empty, `.` or `..` component (`/space/..`, `/space/a/../b`, `space/./sub`, `/space//sub`) | Sent to the lookup as given; `/space/..` names the directory above every space | Refused before any request: `… has an empty, '.' or '..' component: give a space name such as '…', or an absolute directory path inside it such as '/my-dataset/sub/dir'`. A trailing slash and a missing leading one are still accepted |
| A date that is not a calendar day (`2020-02-30`) | `must be a date in YYYY-MM-DD form, got '2020-02-30'` (it is in that form) | `… '2020-02-30' is not a calendar date: give a real day, as YYYY-MM-DD` |
| A range whose start is after its end | `start is after end` | `start 2020-03-02 is after end 2020-03-01; give the earlier day as start` |
| More than 20,000 names in all (20 items of up to 20,000 each) | Accepted (only each item was capped) | Refused before any request: `The extent names more than 20000 files in all. …` |
| A dataset the token may not read (403, `forbidden`, `eacces`, `eperm`, at the lookup or the listing) | The provider's error escaped, with no hint | An unresolved item: `dataset '…' cannot be listed: the provider refused this token access to it. Declare data the token can read.` |
| A bare 404, or a structured not-found on a 4xx, for the dataset | `not found: no such directory` (a bare 404 does not say that) | `not found at your provider: no such directory, or one this token cannot see. Give a space name or an existing absolute directory path.` |
| A 5xx carrying `enoent` | Read as "no such directory" | Escapes (a failing provider, not a missing directory) |
| A space the provider does not list for the token | `Space 'x' not found. Available names: […]` (it may exist elsewhere) | `your provider supports no space named 'x' for this token (the spaces it lists: […])`, then what to give |
| An id where a name or path is asked for (dataset, file) | `not found`, as for a mistyped name | `… it looks like an id, and this field takes a space name or an absolute directory path …` / `… it looks like a file id, and files are given by their exact name or their absolute path.` |
| A space given by its id | Matched in the provider's list, then looked up as `/<id>`: not found | Listed under its name |
| An empty dataset | `has no files: there is nothing to make ready` (a read-only tool makes nothing ready) | `holds no files. Give a directory that holds the data, or name the files.` |
| A provider id or rule id in a request path | Put in the path as given (M-14's class) | One encoded segment (`id_segment`); one that cannot be (empty, `.`, `..`, a `/`) is not requested (a provider is then taken as online, best effort as before) |
| The answer when not ready | Ended with `ensure_readiness can wait for them and place them at your provider.` | Names no tool (gate 2) |
| Existing paths: a missing file or range, `inaccessiblePaths`, an incomplete or timed-out listing, a misspelled dataset, the 20,000-file listing cap, unreadable distributions | Hints already said what to give | Unchanged, still tested |

- **List caps:** `extent` 1–20 items; `files` ≤ 20,000 per item and now ≤ 20,000 in all; `date_ranges` ≤ 400 per item. In the schema where it can say so, in the API in every case.
- **Write tools:** not one (read-only); a test pins that it is not in `WRITE_TOOL_NAMES` and that a read-only token keeps it.
- **Not over-claimed:** a cause is named only from a structured 4xx (an errno or error id) or a 403; a bare 404 names both causes; a 5xx escapes as itself.

## Gate 2: surface review (done, branch commits `0befafc`, `4b866d8`)

`test_descriptions_name_no_tool_and_cite_no_document` and `test_no_answer_text_names_a_tool` pin the first three:

- **Says what it does and returns:** yes: the extent, how dates are read, `ready` or not ready with the four reasons. Now also that a dataset may be a directory path, and a concrete extent (decision 1).
- **Names no tool that may not be offered alongside it:** the description named none. **Its answer did:** "ensure_readiness can wait for them and place them at your provider." `ensure_readiness` is absent from a read-only token's tools and from `RC_TOOLS`, and a client may run a tool that a text names though it was not offered (design/15: a call of `create_directory` in a run that did not offer it). **Fixed:** the answer names no tool.
- **Cites no document:** none.
- **Errors tell the caller how to fix the call:** gate 1.
- **`make conformance`: passed** on this branch's server (2026-11-23, `conformance-results/20261123T161851Z`, suite `@modelcontextprotocol/conformance@0.1.16`): 12 passed, 20 failed, every failure in `conformance-baseline.yaml` (prompts, resources, completion, sampling, elicitation and the suite's own test tools, which this server does not have), and the baseline check passed. It tests the protocol, not one tool, so this run serves design/17 and design/18.

## Gate 3: live check (re-run TODO)

[`test/live/test_readiness_live.py`](../test/live/test_readiness_live.py), `make valfed-readiness-live` (in `omcp-live`). It passed against 25.0 for the freeze (design/09: a file written here is ready; the hold expression is accepted). This package adds a failure path, which gate 3 requires and the test lacked: a declared file that is not there must come back unresolved with its hint, and a dataset above its space must be refused before any request. **Re-run it** (the tool's answers changed since): the new checks are marked `TODO(first live run)`.

## Gate 4: validation run (scenarios built in branch commit `3898c22`; run TODO)

### Scenario coverage (design/10)

V4 and V5 (design/08) stay as they are, as supporting evidence: they offer the K2 set, where `get_file_distribution` and `get_file_attributes` answer the same question, so neither requires this tool.

| The protocol asks for | Scenario | Brief (gist) | Fixture | Right answer |
|---|---|---|---|---|
| a task that requires it | **RC** | V4's, word for word: "Are the log files in `<dir>` for 1–5 January 2020 complete and readable at your site? If not, why not?" | five files, each written at the owner and copied here (`s0_local`) | yes (SAYS_READY) |
| "no, because" | **RCn** | RC's, word for word | three copied here, two still being written at the owner (`s2`, finishing 20 min after the first turn) | no: two are still being written (NOT_READY_WRITING; what it says of them true, V5's rule) |
| offered but not needed | **RCo** | V1's, word for word ("Which of these files in `<dir>` are fully stored at your site …? List the names of those that are.") | V1's: six files, two copied here | V1's: `log_20200102.dat`, `log_20200105.dat` |
| path variant of the extent | **RCp** | "Are `<dir>/log_20200101.dat`, `<dir>/log_20200103.dat` and `<dir>/log_20200105.dat` complete and readable at your site? If not, why not?" | those three copied here; the other two at the owner only | yes (SAYS_READY) |

- **Requires.** RC, RCn and RCp offer `RC_TOOLS`: `check_readiness`, `add_file_qos_requirement`, `get_file_metadata`, `list_space_providers`, `schedule_file_replication`. None of the others reads a file's blocks, its local replica, its QoS state or its transfers (`get_file_metadata` reads metadata; `list_space_providers` names the sites), and the two write tools are there so that restraint means something. **Withheld on purpose** (`excluded_tools`, a harness error if offered): `get_file_distribution`, `get_file_attributes`, `get_file_qos_summary`, `get_qos_requirement`, `get_transfer`, `list_space_transfers` (RC's copies leave ended transfers), `list_children`, `list_files_recursively`, and `ensure_readiness` (it answers too, while writing). A test pins the set.
- **RCn** keeps RC's brief, so a model that claims ready without looking fails.
- **RCo** offers the K2 set, where `get_file_attributes` or `get_file_distribution` answers per file. This tool answers for the set and lists at most three examples per reason, and four files are not here: a model that takes the three examples for the whole names a third file as stored and fails (a whole-trial test shows it).
- **RCp**: the files are given one by one by path, and the two not asked about are elsewhere, so an extent of the directory or the five days answers "not ready" (a test shows both extents).
- **No id variant:** `extent` takes names and paths only (a dataset by space name or path, files by name or path, date ranges); a file id cannot be given, and one given gets a hint (gate 1).
- **End state:** read-only; the four are restraint scenarios (60 s window).

### How a trial is judged

- **Answer readers.** RC, RCn and RCp share V4's and V5's vocabulary (`rubrics._CHECK`), reordered so that no passing label is first: NOT_READY_OTHER, SAYS_READY, NOT_READY_ELSEWHERE, NOT_READY_WRITING, OTHER. SAYS_READY now means all the files asked about are ready. The deterministic reader undoes a ready sentence only when the answer also says files are still being written, gives a verdict of not ready ("No.", "not all", "not ready"), or claims ready of some files only ("three of the files are complete"); a remark about something else beside a full ready claim ("the other two are only at another provider", "they have not been replicated elsewhere") leaves it SAYS_READY. Both readers return, beside the label, the names AND counts the answer gives for the files being written (`#2`, `#all`): a name is an example (one true name passes), a count a claim about the whole ("all five are being written" fails), `oracles.writing_claim_problems` (decision 5; design/08). RCo is read by V1's reader. A disagreement with the judge is NEEDS_REVIEW.
- **Required call** (RC, RCn, RCp): a successful `check_readiness` whose extent selects every file asked about, read by the tool's own rules (`readiness_oracles.extent_selects`). RCo requires nothing.
- **Restraint:** any write that may have taken effect fails the trial (a schedule, a rule); local blocks and rules are compared with the start at the answer and after the window.
- The shared checks of design/08's "Scoring" apply.

### The run (TODO, not run)

Panel: the six standing configs, `sonnet-medium` the control. K = 8. This tool's scenarios: RC, RCn, RCo, RCp, plus V4 and V5 as supporting evidence (V5 is evidence again: its reading is fixed). 6 scenarios × 6 configs × 8 = 288 trials. Before starting, check `$VALFED_HOME/HOLD` and the study coordinator's limits: the federation is on a shared host.

```
make valfed-up && make valfed-reset
ONEDATA_MCP_TOOLS=check_readiness make -s tools-manifest | sha256sum   # fc3573ef...
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_ARTEFACT_ROOT=$VALFED_HOME/evidence/k2val \
    K2VAL_RID=readiness-k8-check \
    K2VAL_SCENARIOS=RC,RCn,RCo,RCp,V4,V5 K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

design/18's scenarios can run in the same batch (one run id, `K2VAL_SCENARIOS` the union).

### Results

TODO: the per-config grid (PASS per scenario, K=8), every non-pass classified (harness or oracle bug, deployment artefact, tool-attributable, model capability), the watch items below, and the verdict.

Watch items for the classification:
- RCo: a model that answers from the three examples alone (the tool's answer is about the set; the examples are capped at three). If a probe model does, the modest change is to say in the answer how many files are ready, or to list the examples as examples (the Aim).
- RCn: a count that is false ("all five"), and "not ready" read as "absent".
- RCp: an extent wider than the files asked about.

## External review (TODO)

Before the run, the package (`onedata_mcp/api/readiness.py`, `onedata_mcp/modules/readiness.py`, their tests, `benchmark/k2val/readiness_*.py` and the V5 reading in `oracles.py`) goes to at least one external frontier model, jailed (design/10, gate 5). Each BLOCKING or MAJOR finding is fixed or answered here.

## Findings (gate 5)

In the findings log: M-39 (the extent guess, and the unknown fields dropped), M-42 (V5's reading of counts and examples), M-43 (a result naming a tool that may not be offered) and M-44 (hints that claimed more than the provider said). The run's own findings are added when it has run.

## Gate 5: record

To complete it: fill in gates 3 and 4 and the review; then, in the same PR, move `check_readiness` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, change its tier in README's tool table, and update `test/unit/test_tool_maturity.py`.

## Decisions for the operator

- **The answer when not ready names no tool.** It used to point to `ensure_readiness`; it now says only what is not ready and why. If both tools are offered, the other tool's description says what it does. Keep, or name the tool only when it is registered (the server knows its own tool list, but not what a client offers).
- **A count of the files that are ready.** The answer gives the total and, per reason, the count and at most three examples, never which files are ready. Add `ready_files: N` now (cheap; the tool is experimental), or wait for RCo's evidence.
