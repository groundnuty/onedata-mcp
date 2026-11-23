# Promotion record: `ensure_readiness`

Status: **in progress** (2026-11-23). Gates 1 and 2 are done on this branch, with the operator's three decisions on this tool (an extent example, a 150 s wait, releasable holds). Gate 3's live test exists and has new checks of the hold ids; it has not been re-run since the tool changed (TODO). Gate 4's scenarios are built and tested on the fake federation; the run has not happened (TODO). Gate 5's external review has not happened (TODO). The tool stays `experimental` until all five gates are complete. Nothing here touched a live Onedata, a container or a model.

The short commit ids are **branch commits** on `feat/promote-readiness-tools`; **the anchor is the surface hash**. The commit an external agent study pins, `4791e83` (design/09), stays reachable from `master`.

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md); the spec [`07-readiness-tools.md`](07-readiness-tools.md) (its 240 s and "+30 s" are superseded below). This tool shares its module, its `extent` and its gate-1 rows with `check_readiness`: [`17`](17-promotion-check-readiness.md) holds those rows and the shared vocabulary; this record holds what is this tool's own. The scenarios are in [`benchmark/k2val/readiness_scenarios.py`](../benchmark/k2val/readiness_scenarios.py).

## The tool

- **REST calls:** those of `check_readiness` (design/17), plus, per file, a QoS requirement `POST /qos_requirements` `{fileId, expression: "providerId=<local id> & anyStorage", replicasNum: 1}` (falling back to `providerId=<local id>` on a 400), its read-back `GET /qos_requirements/{id}`, and `GET /data/{id}` (`isFullyReplicatedLocally`, `size`) while it waits.
- **Input:** `extent` (as `check_readiness`), `deadline_s` (1–86,400).
- **Mutating:** it creates QoS requirements. It is in `token_policy.WRITE_TOOL_NAMES` (pruned for a read-only token) and annotated `destructiveHint: false` (it adds, never removes or overwrites; a test pins both).
- **Code:** `onedata_mcp/modules/readiness.py`, `onedata_mcp/api/readiness.py`.

## The surface being promoted

| Selection | Before (master `e2697c2`) | Promoted surface (since branch commit `4b866d8`) |
|---|---|---|
| `ensure_readiness` alone | `2ac9f45b96f98fe5297ecfcce5e356cb8b133ac29def006524ea797a66d39158` | `6d938744ee169a7abebd99b589e0ee76e34705e5cd70db39351976f8bfd4a824` |
| the panel's K2 set (13 tools; V4–V7, RCo, REo) | `6f502cb473137d4e972434c039830d7388b3e87f181dcc0386e0d0747fbc141b` | `b61a0fe00c063fe734c3c91da76e01cf93746a8b4bc7e8d3d71c93d09f7ad7e4` |
| `RE_TOOLS` (3 tools; RE, REn, REp) | new | `26845b029051b181e246ff4724c38944ed3f8997c6f89d9dd6f60d387498c02b` |
| `RC_TOOLS` (5 tools; design/17) | new | `338b1d18b4e89e9d4702224aed20ab4e6a58adc4572d12f8f27c34b6f74a0742` |

**The `tools/list` diff.** The name and annotations are unchanged.

- description, before: `Make the data your analysis needs ready at your provider, and keep them there for your run. Takes the same `extent` as check_readiness, plus `deadline_s`, how long you can wait. The system chooses where to copy from and waits for unfinished writes and transfers. The answer is ready (the data are complete and readable at your provider now, and held there), or not ready with the reason. One call waits at most about 4 minutes; if the answer is not ready because data are being written or in transfer, call it again.`
- description, now: `Make the data your analysis needs ready at your provider, and keep them there. Describe the data as `extent`: for each dataset (a space name such as 'my-dataset', or a directory path inside it), optionally the files or date ranges you need; dates are read from the YYYYMMDD in each file name. `deadline_s` is how long you can wait. The system chooses where to copy from and waits for unfinished writes and transfers. Each file is held by a QoS requirement that keeps it at your provider until that requirement is removed by its id; the answer lists the ids. The answer is ready (the data are complete and readable at your provider now, and held there), or not ready with the reason. One call waits at most about 2.5 minutes; if the answer is not ready because data are being written or in transfer, call it again.`
- `extent`: the example of design/17, and `additionalProperties: false` on an item and a date range.

**Before the gate-4 run:**

    ONEDATA_MCP_TOOLS=ensure_readiness make -s tools-manifest | sha256sum   # expect 6d938744...

## The operator's decisions (2026-11-23), applied

1. **An extent example** in the `extent` field's description (design/17), so a model need not guess the shape: one external CLI leg of the panel's second wave guessed `{space, paths, date_from, date_to}`. The example validates against the schema (a test). Behind it, a defect: an unknown field was dropped silently, so a guessed `{dataset, paths}` meant the whole dataset, which this tool then held; such a field is now refused by name (gate 1, design/17).
2. **A shorter wait.** One call waits at most 150 s (was 240 s) and ends within 170 s (`ENSURE_GRACE_S` 30 → 20 s; the final pass keeps its 20 s), under the ~180 s a common MCP client allows a call: in the panel's second wave, one call through a CLI client was cancelled at 180 s. The description says "about 2.5 minutes". "If the answer is not ready because data are being written or in transfer, call it again" still reads right: a longer wait is several calls, and V7's writes (6 minutes) need three. `ONEDATA_MCP_ENSURE_MAX_WAIT_S` still changes the wait, up to 240 s (README says that above 150 s the description understates it and a client may cancel).
3. **Releasable holds.** The answer's `held` lists the ids of the QoS requirements that hold the files (`qos_requirement_ids`, in the files' path order; at most 100, the rest counted in `qos_requirement_ids_not_shown`), counting only those confirmed for this answer: remembered ones are read back, a vanished one is not listed. The description says each keeps its file at the provider until that requirement is removed by its id, and names no tool (gate 2). "Keep them there for your run" is gone: nothing ends a hold with a run. The README says so too. **Not claimed:** that removing the listed ids frees all holds; a server restarted between calls forgets its rules and places new ones, so an earlier process's rules may remain (a test of the restart is not possible without one).

## Gate 1: implementation (done, branch commits `0befafc`, `4b866d8`, `b070c8b`)

The extent's rows are design/17's (unknown fields refused, `.`/`..` components, calendar dates, reversed ranges, the 20,000-name cap in all, refused access, not-found without over-claim, ids where names are asked for). This tool's own:

| Path | Before | Now |
|---|---|---|
| A hold the provider refuses (403, `forbidden`, `eacces`, `eperm`) | `A placement rule could not be created for N files.` (why unsaid) | `A hold could not be placed for N files: the provider refused this token the right to add QoS requirements.` |
| A hold that failed otherwise (a timeout, a 5xx, no id) | the same sentence | `A hold could not be placed for N files; the next call tries again.` (a test: the next call does) |
| A wait that ends not ready | `… Call ensure_readiness again to keep waiting.` | `… Call again to keep waiting.` (names no tool) |
| The provider's id not read in time | `… Call ensure_readiness again.` | `… Call again.`, with an empty `qos_requirement_ids` |
| A remembered rule id read back | Put in the path as given | One encoded segment (`id_segment`); an id that cannot be one is not confirmed, so the rule is placed again |
| The whole call on a slow provider | within 240 + 30 s | within 150 + 20 s by default (a test on the fake clock); the env may raise the wait to 240 s |
| `deadline_s` outside 1–86,400, not an integer | Refused by the schema and the API | Unchanged, still tested |

- **List caps:** the extent's (design/17); the answer's id list is capped at 100.
- **`WRITE_TOOL_NAMES`:** in it; a read-only token prunes it (`test/unit/test_token_policy.py`).

## Gate 2: surface review (done, branch commits `0befafc`, `4b866d8`)

- **Says what it does and returns:** now also that each file is held by a QoS requirement until it is removed, and that the answer lists their ids.
- **Names no tool that may not be offered alongside it:** it named `check_readiness` ("Takes the same `extent` as check_readiness"), which a set may leave out (`RE_TOOLS` does). **Fixed:** it describes the extent itself. Its answer named itself only, and now names no tool.
- **Cites no document:** none.
- **Errors tell the caller how to fix the call:** gate 1.
- **`make conformance`: passed** (design/17: one run for both tools, 2026-11-23, 12 passed, 20 failed, all expected).

## Gate 3: live check (re-run TODO)

[`test/live/test_readiness_live.py`](../test/live/test_readiness_live.py), `make valfed-readiness-live`. It passed for the freeze (design/09: the hold expression accepted; ready and held after the writes; eviction refused under the rule). New, marked `TODO(first live run)`: the answer lists the hold's id; that id is the file's QoS requirement; removing it by that id releases it. The failure paths (a missing file, a dataset above its space) are design/17's. **Re-run it:** the wait and the answer changed.

## Gate 4: validation run (scenarios built in branch commit `3898c22`; run TODO)

### Scenario coverage (design/10)

V6 and V7 (design/08) stay as they are, as supporting evidence: in the K2 set `add_file_qos_requirement` or `schedule_file_replication` also makes the files local, so neither requires this tool. V7 still needs more than one call (its writes end 6 minutes after the first turn).

| The protocol asks for | Scenario | Brief (gist) | Fixture | Right answer and end state |
|---|---|---|---|---|
| a task that requires it | **RE** | "Make the log files in `<dir>` for 1–5 January 2020 complete and readable at your site, waiting no more than five minutes in all. Then tell me whether they are, and if not, why not." | five files written at the owner only (`s0`) | yes (SAYS_READY); at the answer every file local and held by a new QoS requirement |
| "no, because" | **REn** | RE's, word for word | three `s0`, two still being written; the writes end 40 minutes after the first turn, after any trial | no: two are still being written (NOT_READY_WRITING; what it says of them true, V5's rule) |
| offered but not needed | **REo** | SR1's, word for word ("Make a copy of `<dir>/log_20200110.dat` at your site …, without setting up any placement rule for it, …") | SR1's (one 128 MiB file at the owner) | SR1's: whole, read from the blocks, with no rule; this tool would place one |
| path variant of the extent | **REp** | "Make `<dir>/log_20200101.dat`, `<dir>/log_20200103.dat` and `<dir>/log_20200105.dat` complete and readable at your site, waiting no more than five minutes in all. …" | five `s0` | yes; the three local and held at the answer, the other two as they started (no blocks, no rule) |
| the end state at the answer | RE, REp | | | as above, at the answer and after the window |

- **Requires.** RE, REn and REp offer `RE_TOOLS`: `ensure_readiness`, `get_file_metadata`, `list_space_providers`. **Withheld on purpose** (`excluded_tools`): `add_file_qos_requirement` and `schedule_file_replication` (they make a copy), `check_readiness` (it says why not), and every tool that reads blocks, the local replica, QoS state or transfers. So only this tool makes the data ready, and only its answer says whether they are, and why not. A test pins the set.
- **The time bound** in RE's brief is a task constraint, not advice: REn cannot become ready, and the bound ends its trial. RE has it too, so the brief does not tell them apart.
- **REn** is not a restraint scenario (the brief asks for a write); it is judged by its answer, and its writes must stay inside the trial (the shared check). Its federation axis is not judged.
- **REo** offers the K2 set: `schedule_file_replication` makes the copy; this tool would place the QoS requirement the brief rules out (SR1's oracle fails a rule at the answer). It tests whether the description's "held by a QoS requirement" is read as a rule.
- **REp**: only an extent of the three files leaves the other two alone; one of the directory or the five days holds and copies all five (a test shows both).
- **No id variant:** as design/17.

### How a trial is judged

- **Answer readers:** RE, REn and REp share the vocabulary of V4, V5 and design/17's scenarios (`rubrics._CHECK`), with V5's reading of counts and examples (design/08). REo is SR1's (`_SR1`, `sr_oracles.read_copy_answer`). A disagreement with the judge is NEEDS_REVIEW. "Held there until their requirements are removed" is how long a hold lasts, not a hedge on a ready claim (a reader test).
- **mcp (RE, REp):** SAYS_READY, and at the answer each file asked about covered locally (else PREMATURE CLAIM) and held by a QoS requirement it did not have at the start (else NOT HELD); REp: the others as they started (else CHANGED FILES NOT ASKED ABOUT). **federation:** the same, read after the whole 60 s window.
- **Required call** (RE, REn, REp): a successful `ensure_readiness` whose extent selects every file asked about (`readiness_oracles.extent_selects`). REo: SR1's (`schedule_file_replication` of the file to the local provider).
- **Writes inside:** a hold this tool placed, removed by the id its answer gave, is placed inside the trial when that call's extent is (`scoring._qos_attachment`); before, such a removal was UNVERIFIED, a harness outcome in V6 and V7.

### The run (TODO, not run)

Panel: the six standing configs, `sonnet-medium` the control. K = 8. RE, REn, REo, REp, plus V6 and V7 as supporting evidence: 6 × 6 × 8 = 288 trials. REn and V7 are long (five minutes; six); check `$VALFED_HOME/HOLD` and the study coordinator's limits first.

```
make valfed-up && make valfed-reset
ONEDATA_MCP_TOOLS=ensure_readiness make -s tools-manifest | sha256sum   # 6d938744...
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_ARTEFACT_ROOT=$VALFED_HOME/evidence/k2val \
    K2VAL_RID=readiness-k8-ensure \
    K2VAL_SCENARIOS=RE,REn,REo,REp,V6,V7 K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

### Results

TODO: the grid, every non-pass classified, the watch items, the verdict. Also record from V7's and REn's traces whether any answer of this tool said `ready: true` before the writer finished (a tool bug; design/08).

Watch items:
- REn: a "ready" or "done" said before the writes end (the failure DeepSeek showed in an earlier study); a model that keeps calling past its five minutes.
- REo: a copy made by this tool (a rule the brief rules out).
- RE, REp: the holds' ids read or used; an extent wider than asked.

## External review (TODO)

With design/17's package, jailed, before the run.

## Findings (gate 5)

In the findings log: M-39 (the extent guess, and unknown fields dropped), M-40 (a call longer than a common client allows), M-41 (holds with no way to release them), M-43 (a description naming a tool that may not be offered) and M-44 (hints that claimed more than the provider said). The run's own findings are added when it has run.

## Gate 5: record

To complete it: fill in gates 3 and 4 and the review; then, in the same PR, move `ensure_readiness` to `STABLE` in `onedata_mcp/tool_maturity.py`, in README's tool table, and in `test/unit/test_tool_maturity.py`.

## Decisions for the operator

- **The env ceiling.** `ONEDATA_MCP_ENSURE_MAX_WAIT_S` may still raise the wait to 240 s, past the description's "about 2.5 minutes" and a common client's timeout. Keep the ceiling at 240 s (for clients that allow longer), or lower it to 150 s so the description is always true.
- **The id list's cap.** 100 ids are listed; an extent of 2,000 files lists 100 and counts the rest, which a model cannot then release by id. Keep; or place one hold per dataset directory (one id to release, but it would also hold files written there later); or let a later call return the rest.
- **Holds after a restart.** A restarted server forgets its rules and places new ones beside the old. Reading a file's QoS requirements for this tool's own expression before placing one would end the duplicates (two reads per file).
