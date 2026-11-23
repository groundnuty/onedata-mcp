# Promotion record: `ensure_readiness`

Status: **in progress** (2026-11-23). Gates 1 and 2 are done on this branch, with the operator's three decisions on this tool (an extent example, a 150 s wait, releasable holds), and were reworked after the external review (what `ready` means, a bounded call, holds kept per call). Gate 3's live test exists and has new checks of the hold ids and of a failing call; it has not been re-run since the tool changed (TODO). Gate 4's scenarios are built and tested on the fake federation; the run has not happened (TODO). Gate 5's external review has happened (design/17, and the table below). The tool stays `experimental` until all five gates are complete. Nothing here touched a live Onedata, a container or a model.

The short commit ids are **branch commits** on `feat/promote-readiness-tools`; **the anchor is the surface hash**. The commit an external agent study pins, `4791e83` (design/09), stays reachable from `master`.

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md); the spec [`07-readiness-tools.md`](07-readiness-tools.md) (its 240 s and "+30 s" are superseded below). This tool shares its module, its `extent` and its gate-1 rows with `check_readiness`: [`17`](17-promotion-check-readiness.md) holds those rows and the shared vocabulary; this record holds what is this tool's own. The scenarios are in [`benchmark/k2val/readiness_scenarios.py`](../benchmark/k2val/readiness_scenarios.py).

## The tool

- **REST calls:** those of `check_readiness` (design/17), plus, per file, a QoS requirement `POST /qos_requirements` `{fileId, expression: "providerId=<local id> & anyStorage", replicasNum: 1}` (falling back to `providerId=<local id>` on a 400), its read-back `GET /qos_requirements/{id}`, and `GET /data/{id}` (`isFullyReplicatedLocally`, `size`) while it waits.
- **Input:** `extent` (as `check_readiness`), `deadline_s` (1–86,400).
- **Mutating:** it creates QoS requirements. It is in `token_policy.WRITE_TOOL_NAMES` (pruned for a read-only token) and annotated `destructiveHint: false` (it adds, never removes or overwrites; a test pins both).
- **Code:** `onedata_mcp/modules/readiness.py`, `onedata_mcp/api/readiness.py`.

## The surface being promoted

| Selection | Before (master `e2697c2`) | Promoted surface (since branch commit `06d851e`) |
|---|---|---|
| `ensure_readiness` alone | `2ac9f45b96f98fe5297ecfcce5e356cb8b133ac29def006524ea797a66d39158` | `cc9bf3a7d21c8ced285c7ee0ca6b6ecbe36daf2549c99a10e692cf96a214e4f5` |
| the panel's K2 set (13 tools; V4–V7, RCo, REo) | `6f502cb473137d4e972434c039830d7388b3e87f181dcc0386e0d0747fbc141b` | `a64dde76bf9f507045d71623df06a0f036484b18fb94e127291a0f8cb0d4a894` |
| `RE_TOOLS` (3 tools; RE, REn, REp) | new | `4337b0ccbaf50683ae0ff2e9f947b349a8fdc6b6d3e34279731de027b1a3c043` |
| `RC_TOOLS` (5 tools; design/17) | new | `9f56fa4c26f35bbfeeafb48e08cf5c9a66799328dc24ce876149ed54aac9eb20` |

The hashes recorded before the external review (`6d938744…` for this tool alone, `26845b02…` RE_TOOLS) are superseded; the K1 set, FILE_TOOLS and ST7_TOOLS did not move.

**The `tools/list` diff.** The name and annotations are unchanged.

- description, before: `Make the data your analysis needs ready at your provider, and keep them there for your run. Takes the same `extent` as check_readiness, plus `deadline_s`, how long you can wait. The system chooses where to copy from and waits for unfinished writes and transfers. The answer is ready (the data are complete and readable at your provider now, and held there), or not ready with the reason. One call waits at most about 4 minutes; if the answer is not ready because data are being written or in transfer, call it again.`
- description, now: `Make the data your analysis needs ready at your provider, and keep them there. Describe the data as `extent`: for each dataset (a space, by its name such as 'my-dataset' or its id, or a directory path inside it), optionally the files or the date ranges you need (with both, every file that either selects); dates are read from the YYYYMMDD in each file name. `deadline_s` is how long you can wait. The system chooses where to copy from and waits for unfinished writes and transfers. Each file is held by a QoS requirement that keeps it at your provider until that requirement is removed by its id; the answer lists the ids. The answer is ready only when the data are complete and readable at your provider now and every file is held there; data_ready says whether the data alone are, and hold_failed which holds are missing and why. Otherwise it is not ready, with the reason. One call waits at most deadline_s, and never more than about 2.5 minutes, then takes up to 20 s more to confirm what it found; if the answer is not ready because data are being written or in transfer, call it again.`
- `deadline_s`: `How many seconds in total you are willing to wait` → `How many seconds this call may wait; it waits at most about 150, then takes up to 20 more to confirm its answer`.
- `extent`: as design/17 (the example; `additionalProperties: false`; `files` and `date_ranges` non-empty, never null; `dataset` by a space's name or id).

**Before the gate-4 run:**

    ONEDATA_MCP_TOOLS=ensure_readiness make -s tools-manifest | sha256sum   # expect cc9bf3a7...

## The operator's decisions (2026-11-23), applied

1. **An extent example** in the `extent` field's description (design/17), so a model need not guess the shape: one external CLI leg of the panel's second wave guessed `{space, paths, date_from, date_to}`. The example validates against the schema (a test). Behind it, a defect: an unknown field was dropped silently, so a guessed `{dataset, paths}` meant the whole dataset, which this tool then held; such a field is now refused by name (gate 1, design/17). So are an empty or null selector and a file name several files share (the external review).
2. **A shorter wait.** One call works and waits at most 150 s (was 240 s) and ends within 170 s (`ENSURE_GRACE_S` 30 → 20 s), under the ~180 s a common MCP client allows a call: in the panel's second wave, one call through a CLI client was cancelled at 180 s. The description says "about 2.5 minutes". "If the answer is not ready because data are being written or in transfer, call it again" still reads right: a longer wait is several calls, and V7's writes (6 minutes) need three. `ONEDATA_MCP_ENSURE_MAX_WAIT_S` may lower the wait, never raise it above 150 s (operator, 2026-11-23: so the description always holds; a test pins 240 → 150).
3. **Releasable holds.** The answer's `held` lists the ids of the QoS requirements that hold the files (`qos_requirement_ids`, in the files' path order; at most 100, the rest counted in `qos_requirement_ids_not_shown`, with a `note`: "Not every id is listed here (the first ones, in the files' path order, are). To list the others, call again with fewer files: a hold this server placed is read back and listed, not placed again.", naming no tool). Before the external review the note pointed at each file's QoS summary, which also lists other people's and inherited requirements. Only holds confirmed for this answer are counted and listed: remembered ones are read back, a vanished one is not listed; one that could not be read back is listed apart, under `hold_failed.unconfirmed_qos_requirement_ids`. The description says each keeps its file at the provider until that requirement is removed by its id, and names no tool (gate 2). The README says so too. **Not claimed:** that removing the listed ids frees all holds; a server restarted between calls forgets its rules and places new ones (M-45, open), and a create request that got no answer may have made a rule no answer lists (below).

## The operator's decisions after the external review, applied

4. **What `ready` means.** `ready` is true only when the data are complete and readable here AND every requested file's hold is confirmed for this answer. The answer reports the two apart: `data_ready`, `ready_files` (as check's), `held.files`, and `hold_failed` with its files, examples and `causes`, counted by what the provider showed: `refused` (403, `forbidden`, `eacces`, `eperm`), `rejected` (another error, or no id), `no_answer` (no answer in time or a broken connection: a hold may have been made), `not_confirmed` (a read-back that was unreadable or named another file or rule), `gone` (removed before the answer), `out_of_time`. The advice says each, and starts "Not ready" whenever `ready` is false ("the data are complete and readable at your provider now (data_ready), but 1 file is not held there. …").
5. **Every phase within the caller's budget.** `ensure_timing`: the provider's id, resolving, placing and waiting all end within `min(deadline_s, cap)`; then the final read-back of the files and the holds takes at most the grace, 20 s at the default cap and scaled down with it (a cap of 30 s: 4 s). `waited_s` is the call's time, measured at return.
6. **Holds kept per call.** A file's hold is read back or placed under a per-file lock (process-wide; waiting for it counts against the budget), so two calls at once on one file place one hold and report the same id. Each call keeps the (file, rule) pairs it confirmed in its own result, never rereading the process-wide map. A read-back is `confirmed` (a 200 JSON naming that rule or that file, and no other), `absent` (a 404 or a 4xx `enoent`/`notFound`: placed again) or `unreadable` (anything else: reported, never followed by a second hold beside one that may be there).

## Gate 1: implementation (done; branch commits `0befafc`, `4b866d8`, `b070c8b`, and after the external review `bc6bd77`, `3b920aa`)

The extent's rows are design/17's (unknown fields refused, empty selectors, a shared name, `.`/`..` components, slashes, calendar dates, reversed ranges, the 20,000-name cap in all, refused access, not-found without over-claim, ambiguous spaces, ids where names are asked for, partial listings). This tool's own:

| Path | Before | Now |
|---|---|---|
| A hold the provider refuses (403, `forbidden`, `eacces`, `eperm`) | `A placement rule could not be created for N files.` (why unsaid) | `A hold could not be placed for N files: the provider refused this token the right to add QoS requirements.`, counted as `refused` |
| A hold that failed otherwise | the same sentence; **(external review)** one refusal made every failure read as a refusal | Each cause its own count and sentence (decision 4): `rejected` "…: the provider rejected the request; the next call tries again.", `no_answer`, `not_confirmed`, `gone`, `out_of_time` |
| A hold whose read-back failed or whose create got no answer | **Defect (verification round):** "N files are not held there" | "the hold of N files could not be confirmed"; "not held" only for a refusal, a rejection, a hold gone or not placed in time |
| Data complete and readable, a hold missing | **Defect (external review):** `ready: true` and "Ready: … and held there" with every hold refused | `ready: false`, `data_ready: true`, the missing holds by cause |
| A wait that ends not ready | `… Call ensure_readiness again to keep waiting.` | `… Call again to keep waiting.` (names no tool) |
| The provider's id not read in time | `… Call ensure_readiness again.` | `… Call again.`, with an empty `qos_requirement_ids` |
| A remembered rule id read back | Put in the path as given; any failure to read it (a 500, a timeout) placed a second rule; an answer naming another file confirmed it | One encoded segment (`id_segment`; an id that cannot be one names no rule: placed again); decision 6 for the rest |
| Two calls at once on one file | **Defect (external review):** both found no rule, both placed one, both reported the second id | One hold, the same id in both answers (decision 6) |
| A short `deadline_s` | **Defect (external review):** resolving and placing could run to 150 s, the final reads to 170 s, and `waited_s` stopped before them | Decision 5 (tests: slow placement, slow final reads, the scaled grace) |
| The whole call on a slow provider | within 240 + 30 s | within 150 + 20 s (a test on the fake clock); the env may only lower the wait |
| `deadline_s` outside 1–86,400, not an integer | Refused by the schema and the API | Unchanged, still tested |

- **List caps:** the extent's (design/17); the answer's id list is capped at 100.
- **`WRITE_TOOL_NAMES`:** in it; a read-only token prunes it (`test/unit/test_token_policy.py`).
- **Residual, recorded:** a create request that got no answer (`no_answer`) may have made a rule whose id no answer has; the next call places another. The process cannot name it without reading the file's QoS summary and every requirement in it (M-45's cost).

## Gate 2: surface review (done; branch commits `0befafc`, `4b866d8`, and after the external review `bc6bd77`, `3b920aa`, `06d851e`)

- **Says what it does and returns:** now also that each file is held by a QoS requirement until it is removed, that the answer lists their ids, that `ready` asks for the data AND every hold (with `data_ready` and `hold_failed` apart), how long a call waits and confirms, and the union of `files` and `date_ranges`.
- **Names no tool that may not be offered alongside it:** it named `check_readiness` ("Takes the same `extent` as check_readiness"), which a set may leave out (`RE_TOOLS` does). **Fixed:** it describes the extent itself. Its answer named itself only, and now names no tool.
- **Cites no document:** none.
- **Errors tell the caller how to fix the call:** gate 1.
- **`make conformance`:** design/17 (one run for both tools; re-run with gate 3).

## Gate 3: live check (re-run TODO)

[`test/live/test_readiness_live.py`](../test/live/test_readiness_live.py), `make valfed-readiness-live`. It passed for the freeze (design/09: the hold expression accepted; ready and held after the writes; eviction refused under the rule). New, marked `TODO(first live run)`: the answer lists the hold's id; that id is the file's QoS requirement; removing it by that id releases it; and (the external review) an ensure call that resolves nothing, and one with an empty selector, place no rule on the probe file. The failure paths of the extent are design/17's, and so is the check, on every lookup, that the directory found carries the chosen space's id. **Re-run it:** the wait, the answer and `ready` changed.

## Gate 4: validation run (scenarios built in branch commit `3898c22`, judged anew in `70f9e09`; run TODO)

### Scenario coverage (design/10)

V6 and V7 (design/08) stay as they are, as supporting evidence: in the K2 set `add_file_qos_requirement` or `schedule_file_replication` also makes the files local, so neither requires this tool. V7 still needs more than one call (its writes end 6 minutes after the first turn).

| The protocol asks for | Scenario | Brief (gist) | Fixture | Right answer and end state |
|---|---|---|---|---|
| a task that requires it | **RE** | "Make the log files in `<dir>` for 1–5 January 2020 complete and readable at your site, waiting no more than five minutes in all. Then tell me whether they are, and if not, why not." | five files written at the owner only (`s0`) | yes (SAYS_READY); at the answer and after the window every file local and held there |
| "no, because" | **REn** | RE's, word for word | three `s0`, two still being written; the writes end 40 minutes after the first turn, after any trial | no: two are still being written (NOT_READY_WRITING; what it says of them, and of the other three, true); at the answer the three local and held, the two still incomplete |
| offered but not needed | **REo** | SR1's, word for word ("Make a copy of `<dir>/log_20200110.dat` at your site …, without setting up any placement rule for it, …") | SR1's (one 128 MiB file at the owner) | SR1's: whole, read from the blocks, with no rule; this tool would place one |
| path variant of the extent | **REp** | "Make `<dir>/log_20200101.dat`, `<dir>/log_20200103.dat` and `<dir>/log_20200105.dat` complete and readable at your site, waiting no more than five minutes in all. …" | five `s0` | yes; the three local and held at the answer and after the window, the other two as they started (no blocks, no rule) |
| the end state at the answer | RE, REn, REp | | | as above |

- **Requires.** RE, REn and REp offer `RE_TOOLS`: `ensure_readiness`, `get_file_metadata`, `list_space_providers`. **Withheld on purpose** (`excluded_tools`): `add_file_qos_requirement` and `schedule_file_replication` (they make a copy), `check_readiness` (it says why not), and every tool that reads blocks, the local replica, QoS state or transfers. So only this tool makes the data ready, and only its answer says whether they are, and why not. A test pins the set.
- **The time bound** in RE's brief is a task constraint, not advice: REn cannot become ready, and the bound ends its trial. RE and REp have it too, so the brief does not tell them apart. It is scored in all three, by one rule (below).
- **REn** is not a restraint scenario (the brief asks for a write). Its writes must stay inside the trial (the shared check).
- **REo** offers the K2 set: `schedule_file_replication` makes the copy; this tool would place the QoS requirement the brief rules out (SR1's oracle fails a rule at the answer). It tests whether the description's "held by a QoS requirement" is read as a rule.
- **REp**: only an extent of the three files leaves the other two alone; one of the directory or the five days holds and copies all five (a test shows both).
- **Id variant:** design/17's **RCi**. The extent is shared, and the id it takes is a space's id; one variant serves both tools.

### How a trial is judged

- **Answer readers:** RE, REn and REp share the vocabulary of V4, V5 and design/17's scenarios (`rubrics._CHECK`), with V5's reading of counts, examples and the other files' claim (design/17). REo is SR1's (`_SR1`, `sr_oracles.read_copy_answer`). A disagreement with the judge is NEEDS_REVIEW. "Held there until their requirements are removed" is how long a hold lasts, not a hedge on a ready claim (a reader test).
- **A hold, as the oracles read it** (`readiness_oracles.holds_here`): a QoS requirement on the file that it did not have at the start, put on the file itself (its `fileId`; not one inherited from a directory, which the file's summary lists too) and keeping a copy at the local provider (`stable_oracles.keeps_local`, from the rule and the storages its expression matches). RE, REn and REp's snapshots record each rule so (`FixtureSpec.rules_in_snapshots`). Before the external review, any new id in the summary counted. Every file must be in the baseline with the rules it had (`qos_ids`), else the trial is not judged (ORACLE_ERROR): a baseline without them read as "no rule" let a hold there before the agent count as new (the verification round).
- **mcp (RE, REp):** SAYS_READY, and at the answer each file asked about covered locally (else PREMATURE CLAIM) and held there (else NOT HELD); REp: the others as they started (else CHANGED FILES NOT ASKED ABOUT). **federation:** the same, read after the whole 60 s window, which RE and REp wait out quietly (RE settled on `local_covered` before; the runner already waited the whole window for a scenario in `REQUIRED`, so it could not end early).
- **REn:** mcp by V5's rule; **federation**, read at the answer (the writes go on, so not after a window): the three complete files covered locally and held there, and the two being written still incomplete at their owner (else the premise failed). Before the external review REn's federation was not judged, and a call whose holds all failed passed.
- **The wait (RE, REn, REp):** the union of the recorded work intervals of the agent's ensure_readiness calls (from their start and end; calls that overlap counted once) may be at most the brief's 300 s plus a stated tolerance of 20 s, once (one final read-back, which the tool takes after its `deadline_s`); more fails (`WAITED TOO LONG`). Two calls of 150 s pass, two of 170 s fail. Answer turns, the snapshot, the window and the judge come after the answer and are not counted. It was scored in REn only at first, with 20 s allowed per call; the operator held RE and REp to it as well, and the verification round removed the allowance per call (thirty 20 s calls, 600 s, had passed).
- **K2 violations** (a ready said before the writer finished: a tool bug, flagged, never the model's failure): recorded from V5's, RCn's, REn's and V7's traces (`oracles.k2_violations`, now called by `verify_writing` too), on `data_ready` where the answer has it, and only for calls whose own record shows they resolved a file being written (by file id): a ready about the three complete files alone is no violation. A ready before the writer finished from a call with no record is reported as unverified, never certain or suspect, and not flagged.
- **Required call** (RE, REn, REp): successful `ensure_readiness` calls that, between them, established every file asked about (design/17's rule, from each call's own record only; a call with none is a harness outcome); REp: only calls whose every selected file is one of the three. REo: SR1's (`schedule_file_replication` of the file to the local provider).
- **Writes inside:** an ensure call's writes are the files it requested a hold for, from its own record (`CallRecord.audit["writes"]`, noted by the tool before each request), read whatever the call's final status (a call cancelled after a placement keeps them; both recorders record a cancellation as an outcome not confirmed), and placed by the file's id and the space's id against the trial's own files, never by the display path (two spaces may share a name and a relative path). A removed hold is placed only by the call that created it, or by a record that ended before the removal began (`scoring._qos_attachment`); a later call's record is no evidence. Before the external review the writes were read from the extent, which ignored `date_ranges` beside `files` and placed a space with file names outside; after it, a call with no record was still read from its extent, and an empty record looked like none. Now every readiness call has a record (empty `resolved` and `writes` when it did nothing, also on an error or a cancellation), and a call with none is a trace from before the record: UNVERIFIED, a harness outcome. **Stored artefacts** made before the record rescore every ensure call as UNVERIFIED.

### The run (TODO, not run)

Panel: the six standing configs, `sonnet-medium` the control. K = 8. RE, REn, REo, REp, plus V6 and V7 as supporting evidence: 6 × 6 × 8 = 288 trials. RCi (design/17) serves this tool's id variant and runs in design/17's batch. REn and V7 are long (five minutes; six); check `$VALFED_HOME/HOLD` and the study coordinator's limits first.

```
make valfed-up && make valfed-reset
ONEDATA_MCP_TOOLS=ensure_readiness make -s tools-manifest | sha256sum   # cc9bf3a7...
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_ARTEFACT_ROOT=$VALFED_HOME/evidence/k2val \
    K2VAL_RID=readiness-k8-ensure \
    K2VAL_SCENARIOS=RE,REn,REo,REp,V6,V7 K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

### Results

TODO: the grid, every non-pass classified, the watch items, the verdict, and the K2 violations recorded from V5's, RCn's, REn's and V7's traces.

Watch items:
- REn: a "ready" or "done" said before the writes end (the failure DeepSeek showed in an earlier study); a model that keeps calling past its five minutes; `data_ready` read as `ready`.
- REo: a copy made by this tool (a rule the brief rules out).
- RE, REp: the holds' ids read or used; an extent wider than asked; a `hold_failed` read as ready; a wait past the five minutes.

## External review (done, 2026-11-23; verified once)

Two external frontier models reviewed the package, jailed: Codex (7 BLOCKING, 7 MAJOR) and Gemini (5 BLOCKING, 6 MAJOR, 1 MINOR); both said "not ready". Each BLOCKING finding was first reproduced by a test on the merged code (branch commit `b9c845b`, kept as strict xfails), then fixed. The same table is in design/17.

| Finding | Severity | Reproduced | Status | Verification round |
|---|---|---|---|---|
| Codex 1, Gemini 4, Gemini 5: an empty or null selector selected the whole dataset; a file name matched every file of that name at any depth; the union of `files` and `date_ranges` unsaid | BLOCKING | yes (both halves; through the API and the MCP client) | Fixed (`a378643`): refused in the schema and the API with a hint; a shared name refused with the paths; the union said | Resolved |
| Codex 2: the first space whose name or id matched won; an id was looked up again by a name another space may share | BLOCKING | yes (both halves) | Fixed (`a378643`): exact id or name, an ambiguous one refused with both; the space bound to its id (design/17, gate 1) | Resolved |
| Codex 3, Gemini 1: the scorer ignored `date_ranges` beside `files`, scoring an outside hold and its removal as inside; a space with file names (the extent example's own form) or a date range was scored outside | BLOCKING | yes (the pure case; the two false fails as whole trials) | Fixed (`70f9e09`): each call's own record places its writes and rules; without one, the extent is read by the tool's rule; a hold no record places is unverified | Not resolved: a removal placed by a LATER call's record; an empty record read as a legacy one and inferred from the extent. Reproduced; fixed (`9fa5f57`): evidence only from the call that created the hold or a record that ended before the removal began; every readiness call has a record (empty when it did nothing), and an absent one is a harness outcome, never inferred |
| Codex 4: REn passed without making any file ready | BLOCKING | yes | Fixed (`70f9e09`): REn's federation judged at the answer | Resolved |
| Codex 5, Gemini 6: the required call credited a path the tool did not resolve (`./`), a whole-directory check in RCp, and refused coverage across calls | BLOCKING (Gemini: MAJOR) | yes (all three) | Fixed (`a378643`, `70f9e09`) | Not resolved: with no record the required call was read from the arguments (a configuration timeout credited every file); RCp dropped unchecked files before its scope check. Reproduced; fixed (`9fa5f57`): records only, per file id; RCp/REp scope over every file selected, checked or not |
| Codex 6: concurrent calls placed two holds and reported the second id; a failed read-back placed a duplicate; a read-back naming another file confirmed; ids beyond 100 not retrievable; the QoS summary lists other rules | BLOCKING | yes (the race, the duplicate, the contradiction) | Fixed (`3b920aa`): decision 6; the note (decision 3) | Resolved |
| Codex 7, Gemini 9: `ready: true` with every hold refused; no `ready_files` | BLOCKING (Gemini: MAJOR) | yes | Fixed (`bc6bd77`): decision 4 | Resolved |
| Gemini 2, Codex 11: the writing reader took a time as a count, a ready count before "because" as the writing count, missed a count after the phrase; "both" was not in the rubric; "All five of the files" read as some; "the other three are missing" passed | BLOCKING (Codex: MAJOR) | yes, except the deterministic "both" (already `#2`; the rubric half reproduced) | Fixed (`6126728`) | Not resolved: "Of the five files, some …" read as `#5`, "On attempt 4 …" as `#4`; no judge marker for "the other three are ready here" (false in V5). Reproduced; fixed (`6e57979`): partitive wholes, tries and ordinals are no counts; `others:ready`, checked against the state |
| Gemini 3: the advice named only the reason with the most files | BLOCKING | yes | Fixed (`bc6bd77`) | Resolved |
| Codex 8, Gemini 8: a short `deadline_s` did not bound placement or the final reads; `waited_s` was taken before them | MAJOR | not required (MAJOR); tests added | Fixed (`3b920aa`): decision 5. Gemini's sub-claim that a cap below 20 s starved resolution was not reproducible: the grace and the reserve were both 20 s, so the work ended at start + cap; the grace now scales with the cap anyway | Not resolved: the selection itself was unbounded (20 × 400 ranges over 20,000 files: 24.9 s here, 78 s for the reviewer, on a 1 s budget). Reproduced; fixed (`2c7681c`): one index per listing, ranges searched, the deadline checked every 1024 files, at most 400 ranges in all |
| Codex 9, Gemini 11: RE/REp counted an inherited or elsewhere-placed rule; no hold check after the window; RE's window could end at once | MAJOR | the window half not reproducible (the runner already waited the whole window for RE) | Fixed (`70f9e09`): `holds_here` at both snapshots, a valid baseline, RE `quiet` | Not resolved (Codex 9): a baseline without `qos_ids` read as no rule, so an earlier hold counted as new. Reproduced; fixed (`96f956e`): unusable (a harness outcome) |
| Codex 10: REn's five minutes unscored | MAJOR | — | Fixed (`70f9e09`): the wait from recorded call times; RE and REp held to it too (the operator's decision) | Not resolved: 20 s allowed per call (thirty 20 s calls, 600 s, passed). Reproduced; fixed (`96f956e`): the union of the recorded intervals, at most 300 s plus 20 s once |
| Codex 12, Gemini 12: `OFFERED_TOOLS` set to the allow-list; a client filtering downstream is unseen | MAJOR (Gemini: MINOR) | — | Fixed (`bc6bd77`): the offered intersection; the production residual recorded (design/17, gate 2) | Not resolved (the production wrapper may still point at a tool a downstream client filtered): accepted by the operator as the recorded residual; no change |
| Codex 13: partial evidence read as categorical | MAJOR | — | Fixed (`bc6bd77`): unknown files, `resolved_files`, `examples_not_shown`, hold failures by cause | Not resolved: a hold whose read-back timed out was called "not held". Reproduced; fixed (`2c7681c`): "could not be confirmed" |
| Codex 14: the id variant waived; no live ensure failure | MAJOR | — | Fixed (`06d851e`): RCi; a live ensure failure test, TODO(first live run) | Not resolved: RCi credited files that a name-addressed item resolved when another item named a space by id. Reproduced; fixed (`9fa5f57`): only files resolved by an item giving the space's id. The live test is the new MAJOR below |
| Gemini 7: a ready sentence naming the source site, or followed by ", while the other two …", was not a ready claim | MAJOR | yes | Fixed (`6126728`) | Not resolved: a ready claim here that also named a replica elsewhere read as OTHER. Reproduced; fixed (`6e57979`) |
| Gemini 10: hints that misled | MAJOR | — | Fixed (`a378643`) | Resolved |
| Gemini 11: the records against the code (RE's settle, `k2_violations`, the MCP unknown-field error, a stale 240 s) | MAJOR | the MCP error reproduced | Fixed (`a378643`, `70f9e09`, these records, M-40) | Not resolved: on a trace with no record a ready about the completed files was flagged a certain K2 violation. Reproduced; fixed (`9fa5f57`): reported as unverified, never certain |
| Gemini 12: a transient read-back failure, then a duplicate | MINOR | yes (with Codex 6) | Fixed (`3b920aa`) | Resolved (with Codex 6) |

**The verification round's new findings** (Codex, 2026-11-23: 11 resolved, 14 not resolved or partly; 2 new BLOCKING, 3 new MAJOR), each reproduced first (branch commit `62e9809`, strict xfails) unless said:

| New finding | Severity | Reproduced | Status |
|---|---|---|---|
| An audited write placed by its display path alone: an ensure aimed at another space with the same relative path was scored inside | BLOCKING | yes | Fixed (`9fa5f57`): placed by file id and space id against the trial's files |
| A cancelled call's audited writes dropped (not ok, no error); `recording.py` let `CancelledError` through; removals not required to have evidence from before them | BLOCKING | yes (both recorders) | Fixed (`9fa5f57`): audited writes read whatever the call's status; a cancellation recorded as an outcome not confirmed; attachments as above |
| An empty record and a legacy trace both serialised as None (the root of Codex 5, Gemini 1, Gemini 11) | MAJOR | yes | Fixed (`9fa5f57`): every readiness call's record starts with empty `resolved` and `writes`, also on errors and cancellation, and names each file's extent items, space id and file id |
| A bare "and" split between coordinated file names ("two files, a and b, still being written" kept only b and lost the count) | MAJOR | yes | Fixed (`6e57979`): split only when the right side has its own verb before any comma |
| The live ensure failure aimed at the whole space (a name missing in one place may exist in another) | MAJOR | no: it needs a live federation, which this work may not touch | Fixed (`a2809e2`): inside a directory the test makes and deletes; TODO(first live run) |


## Findings (gate 5)

In the findings log: M-39 (the extent guessed, unknown fields dropped, empty selectors and a shared name), M-40 (a call longer than a common client allows, and phases, selection included, its deadline did not bound), M-41 (holds with no way to release them, and `ready` without them), M-43 (a description naming a tool that may not be offered), M-44 (hints and partial evidence that claimed more than the provider said) M-45 (duplicate holds: after a restart, open; between concurrent calls and after a failed read-back, fixed) and M-47 (the panel reading what a readiness call did from its arguments and its paths; the verification round). The run's own findings are added when it has run.

## Gate 5: record

To complete it: fill in gates 3 and 4; then, in the same PR, move `ensure_readiness` to `STABLE` in `onedata_mcp/tool_maturity.py`, in README's tool table, and in `test/unit/test_tool_maturity.py`.

## Decisions for the operator

Decided 2026-11-23 and applied (branch commit `29592f0`), with no surface change: the env ceiling lowered to 150 s; the id list's cap kept at 100 with the rest counted; holds after a restart not fixed now (M-45). Decided after the external review and applied: decisions 4–6 above, the scoring of RE, REn and REp, and the brief's five minutes held in all three: the union of the recorded call intervals, at most 300 s plus 20 s once (after the verification round; it was 20 s a call). Accepted as the recorded residual after the verification round: the production pointer when a client filters tools downstream (design/17, gate 2). Accepted as built: `files` renamed `resolved_files`; the per-clause ready reading kept to the check vocabulary (V2, V6, V7 and SR readers unchanged); a name seen once on a listing that stopped early not taken; the note for ids beyond 100 as it is; the review's defects folded into M-39 to M-45. Open:

- The note for ids beyond 100 says that calling again with fewer files lists them; that holds only while the server that placed them runs (M-45).
- A `no_answer` create may leave a hold no answer lists; finding it needs M-45's reads.
