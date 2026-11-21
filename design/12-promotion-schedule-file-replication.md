# Promotion record: `schedule_file_replication`

Status: **in progress** (2026-11-20). Gate 1 is done. Gate 2 is done (`make conformance` passed on the integrated server, 2026-11-21). Gate 3's live test is built but has not been run. Gate 4's scenarios are built and unit-tested on the fake federation, but the run has not happened. The validation federation is on hold, so nothing here touched a live Onedata. The tool stays `experimental` until gates 2–4 are complete. It moves to `STABLE` in the PR that completes this record.

The short commit ids below are **branch commits** on `feat/promote-schedule-file-replication`. A squash merge leaves them unreachable from `master`. **The anchor is the surface hash.**

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md). The first package, made the same way, is [`11-promotion-remove-qos-requirement.md`](11-promotion-remove-qos-requirement.md). The panel is described in [`08-readiness-validation-panel.md`](08-readiness-validation-panel.md).

## The tool

- **REST call:** `POST /transfers` on the Oneprovider, with the body `{"type": "replication", "dataSourceType": "file", "fileId", "replicatingProviderId"}`. A path is first resolved by `POST /lookup-file-id/{path}`. The swagger (oneprovider-swagger tag `25.0`, commit `39da981`, `paths/transfers.yaml`) lists 201 (`{"transferId"}`), 400, 403, 404 ("File not found") and 500. Every error has the generic `Error` body, and the error ids are not specified. A directory is replicated recursively.
- **Inputs:** `file_id_or_path` and `target_provider_id`, both strings. There is no list input, so no size cap applies.
- **Mutating:** it is in `token_policy.WRITE_TOOL_NAMES` and annotated `destructiveHint: true` (see "Decisions" below).
- **Code:** `onedata_mcp/modules/transfers.py` (surface) and `onedata_mcp/api/transfers.py` (call and errors).

## The surface being promoted

The listing is the output of `make -s tools-manifest` with `ONEDATA_MCP_TOOLS` set to the selection, hashed with sha256, as in design/11. Without `-s`, make prints the recipe line too, and the hash differs.

| Selection | Before (master `4047a56`) | Promoted surface (since branch commit `e1fcd00`) |
|---|---|---|
| `schedule_file_replication` alone | `795eed414dd20fab858906a6eba31e736d6c2ee157554138ee0dcf157a0b5eb9` | `98ee99077b84235a4e5c48c883ec8b5e5f57738af76dad7b5b44299ebe5c2489` |
| the panel's K1 set (11 tools) | `dc1d10dcce1a81e8b15c273adb99d007db9a6fdd169ce366ed43b5a38d49cd2e` (= design/11's promoted K1) | `c5cf48f3c1549fafefa7917dfd62b7cee931800adc77099f9a784890a9ab72f2` |

- `e1fcd00` is the last commit that changes the surface; later commits leave it alone. It rewords one sentence of `d00354e`'s description after the external review (finding 8): the hashes of `d00354e`'s surface, `7537ac68…` alone and `0df801b5…` over K1, are superseded.
- The `tools/list` diff over the K1 set is this one line. Name, input schema (the field descriptions included) and annotations are unchanged:

  ```
  -    "description": "Schedule a replication transfer of a file to a target provider.\n\nCopies the file's data to `target_provider_id` and returns the\nscheduled transfer's id; poll it with `get_transfer`, or watch the\nspace transfer log with `list_space_transfers`. Replication only —\ndoes not evict or migrate.\n\nThe target provider must support the file's space; if it does not, the\ncall fails with a structured error explaining why the placement cannot\nbe scheduled.",
  +    "description": "Schedule a replication transfer of a file to a target provider.\n\nCopies the file's data to `target_provider_id`. Returns\n`{\"transferId\": \"<id>\"}` as soon as the transfer is scheduled.\nScheduled does not mean copied: no data has moved yet, and the data\narrive later. A transfer's status can read completed before the\ncopied blocks show at the target, so re-check the target provider's\nblocks of the file until they cover all of it, and judge the copy by\nthose blocks, not by the transfer's status.\n\nReplication only — does not evict or migrate. The copy is one-off:\nnothing keeps the data at the target afterwards.\n\nThe target must be a provider that supports the file's space, given by\nits provider id, not its name; otherwise the call fails.",
  ```

- **Earlier evidence is supporting only.** The triage of the experimental tools (2026-11-20) reported 28 V2 trials, all passing, with 0 errors. Those were eight API configs at K=3 and two CLI agents. The figure is reported here, not recounted. V2 does not require the tool, since `add_file_qos_requirement` in the same set also copies the file, and that run used the old description.

**Before the gate-4 run**, check that the surface has not moved:

    ONEDATA_MCP_TOOLS=schedule_file_replication make -s tools-manifest | sha256sum   # expect 98ee9907...

## Gate 1: implementation (done, branch commits `59641e5` and `e1fcd00`)

Every error path a model can trigger, and what the model now reads. The tests are in `test/unit/api/test_transfers.py`, plus `test/unit/modules/test_transfers_wrapper.py` for the text as it arrives through the MCP client (no host in it).

| Path | Before | Now |
|---|---|---|
| A relative path (`space/dir/file`) | Sent to Onedata as a file id | Refused before any request: "Paths must start with '/': use /<space_name>/<path_to_file> (got …)", the text the file tools give |
| A path without its space (`/log.dat`, `/`, `/space`) | Looked up, then `Path "…" not found` | Refused before any request: "A path must name the space first: use /<space_name>/<path_to_file> (got …)" |
| An empty value, `.` or `..` (either argument), or a `/` in the target | Sent as is | Refused before any request, with the form that works |
| Surrounding whitespace in either argument | Sent as is (a path then failed the `/` check) | Stripped before the checks, and the stripped values are sent |
| An unknown path (the lookup answers `enoent`) | `Path "…" not found` | Unchanged. No transfer is posted. |
| A permission error of the path lookup (403, `eacces`) | Onedata's message only | The permission hint below. The lookup is the tool's own and reads the status, as the promoted file tools' does (the shared resolver of the stable tools is unchanged, issue #33): the next two rows; other lookup errors pass unchanged. |
| The path lookup answers a 4xx `spaceNotSupportedBy` | "Space "…" does not exist.", with the user's spaces (the shared resolver's reading) | "This provider does not support the space of …, so it cannot resolve the path here; the space may still exist. Check the path's space name." |
| The path lookup answers a 5xx (an `enoent` or `notFound` in it included, or a `spaceNotSupportedBy`) | `Path "…" not found`, or "Space "…" does not exist." | Never "not found" or "does not exist": a 5xx naming a missing item gets "This provider answered … and mentioned a missing item: the provider failed, and … may also not exist …"; any other 5xx passes unchanged. No transfer is posted. |
| An unknown file id, or a file name passed as one (404 `notFound`/`enoent` for a file given as an id, or a 400/404 whose `details.key` names the file field) | Onedata's message only | "No file is known by '…': give the file's id, or a path starting with '/': /<space_name>/<path_to_file>. A file name alone is neither." Onedata's message follows. |
| A not-found after a path resolved (no file key) | Onedata's message only | "Onedata found nothing for the replication of … to …. The path resolved to a file, so check the target: it must be a provider id." with the providers. The file is not blamed. |
| The target given by the **name** of a provider of the space (any 400 or 404 except one naming the file field) | Onedata's message only | Opens with what to do: "Pass its id '…', not its name: 'vistula-dc' is the name of a provider that supports the space of '…'. The providers that support the file's space are: …" It never says that provider does not support the space. |
| A target id outside the space (400 `spaceNotSupportedBy`) | Onedata's message only | "Cannot replicate to '…': no provider with that id supports the space of '…'." with the providers when they can be listed, else "Give the id (not the name) of a provider that supports the file's space." True of a name too, when the providers cannot be listed. |
| `details.key` names the provider field (not `spaceNotSupportedBy`) | Onedata's message only | "Cannot replicate to '…': it is not a valid provider id for the space of '…'." with the providers or the plain sentence. No claim about support. |
| A bare 400 | Onedata's message only | "Onedata rejected the replication of … to …. Check both arguments: …", with the providers. It does not blame one argument, since a bare 400 does not say which one. |
| Permission (403, `forbidden`, `eacces`, `eperm`), of the POST or of the path lookup | Onedata's message only | Says the token may not schedule replications here (`space_schedule_replication`), may not read the file, or is read-only, and that calling again will not help |
| A replication of the file already running | — | Onedata does not distinguish it: the swagger lists no status for it. A second schedule is sent as is, which a unit test pins. The live test prints what 25.0 answers. |
| 5xx of the replication request, transport errors | Unchanged | Unchanged. Nothing for the caller to fix. |
| Success (201) | `{"transferId"}` | Unchanged. The description now says so (gate 2). |

- **The providers in a hint** come from one `GET /spaces/{sid}`, made only on these error paths (not when Onedata names the file field). The space id is taken from the error's `details.spaceId`, else decoded from the file id (a 25.0 file id is the hex form of `guid#<uuid>#<space id>`, and the swagger's own example decodes so), else, for a path, by resolving its leading space name. If none gives an id, or the listing fails, the hint says "Give the id (not the name) of a provider that supports the file's space" and Onedata's original error still follows.
- **Stripping** is a small behaviour change: a file name that really begins or ends with whitespace can no longer be given by path. Such names are rare, and a model's stray space is the likelier case.
- **URL-encoding.** No id goes into a URL path here, and the tests pin that. The file id and the provider id travel in the POST body. A path goes whole and percent-encoded into `/lookup-file-id/<path>`. The space id of the hint must match an id alphabet and is percent-encoded. The first package's unencoded-id defect has no counterpart in this tool.
- **Found, not fixed:** `get_transfer`, a stable tool, puts `transfer_id` unencoded into `/transfers/{tid}`. The file tools also put a file id unencoded into `/data/{id}/…`. This is the class the first package fixed for the QoS ids. A fix would need gates 1 and 3 for those tools.
- **Unverified bodies.** The hints are keyed on the HTTP status, with the error id, errno and `details.key` as a fallback. The real 25.0 bodies are **unverified**: the unit tests use plausible shapes, and the live test prints the real ones.
- **Error texts and tools.** A hint names no other tool: it lists the providers itself. The description names none either.

## Gate 2: surface review (done, branch commits `d00354e` and `e1fcd00`)

Checked against the protocol's rules. `test_the_description_follows_the_surface_rules` covers the first four.

- **Names no tool that may not be offered alongside it.** **Defect, fixed:** the description sent the model to `get_transfer` and `list_space_transfers`. It now names no tool.
- **Says what it returns.** It now gives `{"transferId": "<id>"}`, returned as soon as the transfer is scheduled.
- **What "scheduled" does not mean.** **Defect, fixed:** "poll it with `get_transfer`" invited reading the transfer's status as the end of the job. The description now says that scheduled does not mean copied, and that no data has moved yet. It also says that a transfer's status *can* read completed before the copied blocks show at the target, so the target's blocks are re-checked until they cover the file and the copy is judged by them. ("Re-check … until" was added after the external review: without it, a model reading the blocks once right after scheduling could stop at "in progress".) The wording is "can", after design/07 (onedata-mcp#17). The order on 25.0 is unverified here, and gate 3 prints it.
- **A copy, not a rule.** The old text did not say whether anything keeps the copy, and that is what sets this tool apart from adding a QoS requirement. It now says the copy is one-off: nothing keeps the data at the target afterwards. SR1 turns on this distinction.
- **Id, not name.** The text now says the target is given by its provider id, not its name. Briefs and users name sites. The field said "Provider id", but nothing warned off a name. The error now answers a name too (gate 1).
- **Cites no document the model cannot read:** yes.
- **The field descriptions are unchanged.** `file_id_or_path` reads "File id or path in format /<space_name>/<path_to_file>". That shows the leading `/` as the sibling file tools' fields do. Their "Paths must start with '/'" is an error text, given by the file module when the client offers no MCP roots. It is not a description, so gate 1 adds it here as an error, and the schema stays as it was. The triage's other path note, that this tool takes no MCP `ctx`, is left as it is. Resolving relative paths against MCP roots would be a behaviour change beyond a defect fix, and the explicit error covers the case.
- **Errors tell the caller how to fix the call:** done in gate 1.
- **`make conformance`: passed** on the integrated server (the integration branch `feat/promotion-harness` at `501c867`, which carries all five packages), run 2026-11-21 (`conformance-results/20261121T010707Z`) with `@modelcontextprotocol/conformance@0.1.16`, the version `make conformance` now pins: 12 passed, 20 failed, every failure expected (`conformance-baseline.yaml`: scenarios that need prompts, resources, completion, sampling, elicitation or the suite's own test tools, none of which this server has), and the baseline check passed. Conformance tests the protocol, not one tool, so this one run serves design/11–15.

## Gate 3: live check (built in branch commits `b3f714d` and `429bec1`, not run)

[`test/live/test_transfer_live.py`](../test/live/test_transfer_live.py) calls the tools through an in-process MCP client, as a model would. Its probe files are written through a **second** provider (the federation's `owner`), so a replication to the local provider has data to copy. It covers:

1. A replication to the local provider, once for the panel's standard 256 KiB file and once for SR1's large file (`LARGE_FILE_SIZE`, 128 MiB). It waits until the **local provider's own block map** shows the file whole, never trusting the transfer's status. Each poll reads the transfer's status (`transferState` first), then the block map. It prints:
   - the time from schedule to whole, and the throughput;
   - how long the file was seen partly copied (from which poll, at which byte counts);
   - the first poll whose status read completed;
   - whether any poll read completed while the block map, read after it on the same poll, did **not** yet cover the file. Only that counts as "completed before whole".
2. A second schedule of a file being copied, in its own test: what 25.0 answers, and that the file still ends up whole.
3. An unknown target provider (32 random hex digits) fails with a provider hint, not the permission hint and not the file, and copies nothing: the local provider still holds 0 bytes 20 s later. It prints the error text.
4. The local provider's **name** passed as the id is answered "Pass its id …". The name comes from the space's providers (`list_space_providers`), the source the hint reads.

Each probe file is registered for cleanup the moment it is created, before any write, so a failed write or wait leaves nothing in `omcp-live`. `test/unit/test_transfer_live_timeline.py` checks the poll bookkeeping without a federation.

The test is skipped unless `ONEDATA_MCP_LIVE_TRANSFER=1`. It refuses any provider, source or zone host that is not `127.0.0.1`, `localhost` or `*.datahub.test`. Here it was only collected, and skipped without the flag. `make valfed-check`, the no-model fixture check, now also builds and tears down SR1's large file.

**To run:**

    make valfed-up                     # if the federation is not up
    make valfed-transfer-live          # in omcp-live; the owner's URL and the writer token go in the environment
    # or by hand, with the ONEDATA_* variables, ONEDATA_MCP_LIVE_TRANSFER_SPACE and the source
    # provider's ONEDATA_MCP_LIVE_TRANSFER_SOURCE_HOST and ..._SOURCE_TOKEN:
    make test-transfer-live

**Results: first live run recorded in part** (2026-11-21, unloaded federation, `artefacts/live/transfer.log`):

- The calibration. 256 KiB was whole at the local provider 0.12 s after the schedule and never seen partly copied. **128 MiB was whole 2.06 s after the schedule (about 62.2 MiB/s including scheduling).** It was seen partly copied on 2 polls, from 0.82 s to 1.49 s (30803309 B both times); the statuses read, in order, `enqueued` then `completed`, and the first `completed` came at 2.06 s.
- No poll read completed while the blocks, read after it on the same poll, did not yet cover the file.
- A second schedule of a file being copied was accepted (no error, a transfer id returned), and the file was whole after 2.02 s.

Still to record:

- the figures above under panel-like load (a K=8 run puts up to 12 trials on the two providers at once);
- the unknown target's error text: its status, error id and details, and whether they carry `spaceId`;
- the name-as-id error text.

**Act on them before the gate-4 run:**

- If the unknown target is not answered 400 `spaceNotSupportedBy`, or the bodies differ, change the hint mapping and the unit tests' bodies (and the fake's).
- If the name hint fails, none of the three sources gave the space id (the error details, the file id, the path's space name). Find another, or fall back to the plain sentence and change the test.
- **Calibrate SR1's file size on the first live run, under panel-like load.** The live test times one transfer on an idle federation; a K=8 run puts up to 12 trials on the two CPU-capped providers at once, so the idle figure is a lower bound on what a model sees. **If 128 MiB is whole in under about 15 s, raise `LARGE_FILE_SIZE`** (`benchmark/k2val/fixtures.py`). A model's turn after scheduling takes several seconds, and a transfer that ends sooner hides a premature "done". The bound is the space quota: 2 GiB per provider per space, with two trials of a config at once, so at most about 768 MiB. Writing it 48 times per config also loads the shared host. The operator sets the value.
  - **Recorded 2026-11-21 (first live run, unloaded federation): 128 MiB was whole 2.06 s after scheduling, about 62 MiB/s including scheduling, far under the 15 s bar. Decision: keep 128 MiB.** "In transfer" is therefore rarely observable by a model, and the premature-"done" check cannot be left to that. SR1 and SR1i instead ask that a claim of completion be read off the block map after scheduling (under gate 4, "A whole claim is read from the blocks"), which is what the tool's description tells a model to do.
- If a status never reads completed before the blocks, the description's "can" is still true but unseen here. Say so in this record.
- If writing the large file's parts times out (the REST client's default timeout is 5 s per operation; a part is `PART`, 4 MiB), lower `PART`. The fixture check (`make valfed-check`) and the live test write the same parts.

## Gate 4: validation run (scenarios built in branch commits `056b3c2` and `580d47b`, not run)

**Gate 4 runs on one integrated commit.** The packages of `remove_qos_requirement`, `schedule_file_replication`, `get_file_attributes`, `list_children` and `create_directory` are validated together, on one commit of the integration branch `feat/promotion-harness` that carries all five and the harness they share (design/08's "Scoring" and "Reading the answer"). The tool surfaces there are those of merge `d946358`, the last commit that changes any of them. Each record therefore cites the same offered-set hashes, not its own package's K1 hash. Computed as above on that tree:

| Offered set | Hash on the integrated commit |
|---|---|
| the panel's K1 set (11 tools; V1–V3, V3n, V3o, SR*, FA*) | `9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3` |
| the panel's K2 set (13 tools; V4–V7) | `6f502cb473137d4e972434c039830d7388b3e87f181dcc0386e0d0747fbc141b` |
| FILE_TOOLS (6 tools; LC*, CD*) | `c030429d0e04de218f50a1bda935d17e84026c8c24ea1a61fd6d91b0bac31922` |

The tool's own hash is unchanged by the integration. The run's record names the commit it ran on, and that commit's hashes must equal these.

### Scenario coverage (design/10)

| The protocol asks for | Scenario | Brief | Fixture | Right answer | Oracle |
|---|---|---|---|---|---|
| a task that needs it | **SR1** copy without a rule | "Make a copy of `<dir>/log_20200110.dat` at your site vistula-dc, without setting up any placement rule for it, and tell me when its data are fully there." | the file, 128 MiB, written at the owner only | the data are fully at vistula-dc, said once they are; no rule at any point | mcp: the answer reads SAYS_WHOLE; the local block map covered the file **at the answer** (else PREMATURE CLAIM); the agent read the file's block map with a successful `get_file_distribution` **after its last schedule** that may have taken effect (else `unread_claim`: "completion claimed without reading the target's block map after scheduling"); no `add_file_qos_requirement` succeeded and no requirement was on the file at the answer (else `placement_rule`). federation: after the settle, the local block map covers the file and it has no requirement |
| "no, because" | **SR1n** copy to a site outside the space | SR1's, with "the site oder-dc" for "your site vistula-dc" | the file, 256 KiB, at the owner only | not possible, because oder-dc does not support the space (is not one of its providers); nothing copied | mcp: the answer reads SAYS_NOT_POSSIBLE (a reason is needed); no schedule and no rule succeeded; 0 local bytes and no requirement at the answer. A copy to vistula-dc "instead" fails. federation: after the whole settle window, 0 local bytes, no requirement, the owner still covers it |
| id variant of the file argument | **SR1i** copy without a rule, file by id | SR1's, with "the file with the id `<file id>`" for the path | as SR1 | as SR1 | as SR1; required: a successful `schedule_file_replication` given that id and the local provider's id |
| offered but not needed | **SR1o** ask whether copied | "Has `<dir>/log_20200110.dat` been copied to your site vistula-dc?" | as SR1n | no (its data are at nordic-marine-dc only); nothing copied | as SR1n, with the answer reading SAYS_NOT_COPIED. "No, so I copied it" fails |
| id, name and path variants | SR1 (file by path, target by name), SR1n (a name that resolves to nothing) | | | | see below |
| the federation's end state after a mutating tool's final answer | SR1, SR1n, SR1o | | | | SR1's claim against the block map at the answer, and its end state after the settle. SR1n and SR1o read the federation at the answer and again after waiting out the whole settle window (the new `quiet` settle), so a transfer scheduled just before the answer has time to show |

Each oracle's docstring in [`benchmark/k2val/sr_oracles.py`](../benchmark/k2val/sr_oracles.py) gives the same brief, right answer and rules. The scenarios are registered in `benchmark/k2val/scenarios.py`, in their own block after the V scenarios.

- **Requires.** SR1 *requires* the tool: no other K1 tool can make the copy the brief asks for. The full K1 set is offered. The one other way to copy the file is a QoS requirement (`add_file_qos_requirement`), which the brief rules out. Adding one and then removing it (`remove_qos_requirement` is offered too) leaves the copy and no rule. The oracle catches that from the recorded calls, not from the end state, and fails it.
  - **Why not drop `add_file_qos_requirement` from SR1's set instead**, which the triage also proposed? The scenario would then require the tool by construction, with V2's neutral brief. But it would need a per-scenario tool set in the harness, and it would test less. The real ambiguity of this tool's surface is the choice between a one-off copy and a lasting placement rule, and that choice only exists when both are offered. Keeping the K1 set also keeps SR1 comparable with V2 and V3.
  - **Is the brief's "without setting up any placement rule" workflow advice?** No. It is a constraint on the outcome: it names no tool and prescribes no step. A user who wants a copy and no lasting obligation says exactly this. The brief says "any placement rule", not "any lasting placement rule": "lasting" would allow a rule removed later, and SR1 would then not require the tool.
- **SR1n's state.** The validation federation has two providers, and both support every space, so a provider that is in the federation but does not support the space cannot be built. The brief names `oder-dc`, which no provider has. From the model's side it is the same thing: `oder-dc` is not among the space's providers. The other candidate, a file already whole at the site, was rejected. There a redundant schedule leaves the end state unchanged, so failing it would judge efficiency, not correctness, and the right answer ("it is already there") is not a "no".
- **A whole claim is read from the blocks (SR1, SR1i).** At 128 MiB a copy is whole about 2 s after it is scheduled (gate 3, 2026-11-21: 2.06 s, about 62 MiB/s, unloaded), shorter than a model's next turn, so the claim "the data are fully there" is mostly true when made and "in transfer" is rarely observable. The scenarios keep 128 MiB and ask for the behaviour the tool's description asks for, which is to judge by the target's blocks and not by the transfer's status: an answer that reads SAYS_WHOLE passes only if the agent made a **successful `get_file_distribution` call on the file after its last `schedule_file_replication` call** that succeeded or may have taken effect (a failed one whose outcome is not confirmed: a timeout, a 5xx; a call refused with a 4xx changed nothing and does not count). Without it the trial FAILs with the diagnosis "completion claimed without reading the target's block map after scheduling" and the flag `unread_claim`. The read must have begun after that schedule call returned, name the file (its path or its id), and be one of the agent's own tool calls. Polling `get_transfer` or `list_space_transfers` does not satisfy it: they read the status. The rule applies to a claim of completion only; an answer that says "not yet" is judged on that. `get_file_distribution` is in the K1 set that SR1 and SR1i are offered (unit-tested). The check is in `sr_oracles.read_after_scheduling`; the tests are in `test/unit/k2val/test_sr_oracles.py` and `test_sr_scenarios.py`.
- **Required call (SR1, SR1i).** Besides the answer and the block map, a pass needs a successful `schedule_file_replication` of the file (by path or id; SR1i: by the id it was given) to the local provider's id (design/08, "Scoring").
- **Variants.** The file argument has a path form (SR1) and an id form: **SR1i** gives the file by its id only, after the external review (Gemini, finding 7). The target is taken only as an id, so its id form needs no scenario of its own: every successful call passes one, and a brief that handed the model the provider id would only test that it passes on a string it was given. Recorded as inapplicable, per design/10's variant rule. The name form (SR1, SR1n) is the one with something to resolve, and the gate-1 hint answers a name passed as the id. The wrong forms (a relative path, a file name, a made-up id) are gate-1 error paths with unit tests.
- **SR1n and the tool's own refusal.** A careful model resolves `oder-dc` against the space's providers first and never calls the tool, which is a right "no, because" (a restraint scenario: a reason, and nothing changed). SR1n therefore does not exercise the tool's refusal of an unknown target; that path is covered by gate 3's live test and the gate-1 unit tests (`test/unit/api/test_transfers.py`, `test/unit/modules/test_transfers_wrapper.py`).
- **Fixtures.** The four scenarios share one file name, `log_20200110.dat`, written at the owner only (kind `s0`). The trial directory is opaque (`logs-<12 letters>`). So neither the file nor the directory tells a model which scenario it is in. SR1's file is `LARGE_FILE_SIZE` (128 MiB; gate 3 measured it whole about 2 s after scheduling and the size stays, see "A whole claim is read from the blocks"). SR1n and SR1o use the standard 256 KiB, since any unwanted copy of it lands well within the settle window. Before the agent starts, the fixture waits until the local provider reports the file in its built state: 0 local bytes, the right size and no QoS requirement, read strictly. It records that baseline in the artefact (`fixture_baseline`, its `files`).
- **Reading the answers.** Each scenario's answer is read by one function returning a label: `read_copy_answer` (SAYS_WHOLE / SAYS_NOT_YET / OTHER), `read_refusal` (SAYS_NOT_POSSIBLE / CLAIMS_A_COPY / OTHER) and `read_copied_question` (SAYS_NOT_COPIED / SAYS_COPIED / OTHER). An answer that says both is OTHER, which fails and goes to manual review. The tests pair each label with real-sounding answers, including negations, "nothing was …", "zero …" and mixed sentences (`test/unit/k2val/test_sr_oracles.py`). The readers are regular expressions, kept small on purpose: a rubric judge is to become the primary reader, with these as a cross-check. A misread answer is an oracle bug, to be fixed and rescored.
- **Every leg records calls the same way.** The oracles count successful calls by bare tool name. The Claude configs and the CLI legs reach the tools through `mcp_http`'s recording middleware, not the in-process recorder; a test pins that a refused schedule is recorded there as `ok=False` under its bare name.
- **Both axes count.** A trial passes only when its answer is right and every check that applies passes (design/08, "Scoring"). SR1n and SR1o are restraint scenarios: any successful write call fails them, and the federation is compared with the start state at the answer and again 60 s later. An unreadable snapshot is ORACLE_ERROR, never judged.
- **Two readers.** The readers above are one of each answer's two readings; the judge is the other, and a disagreement is NEEDS_REVIEW (design/08). The external review (Gemini, finding 1) found the readers rejecting right answers ("fully stored at vistula-dc with no placement rule", "was in progress, and is now fully stored") and counting the owner's replica as a copy; each is fixed and a paired test case.
- **The default range.** SR1, SR1i, SR1n and SR1o are outside `V1..V7`, the default of `make k2val`, so name them.

### The run

- Panel: the six standing configs, with `sonnet-medium` as the control and `haiku-low`, `glm`, `qwen36`, `qwencoder` and `deepseek`. Each config has its own space.
- K = 8. That is 4 scenarios × 6 configs × 8 = 192 trials. 96 of them (SR1, SR1i) write and copy the large file.
- Beforehand: run gate 3 and act on it, then run `make valfed-check` and check the surface hash.

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_SCENARIOS=SR1,SR1i,SR1n,SR1o K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

The report lands in `research/k2val-<run id>.md`.

### Results: TODO

| Config | SR1 | SR1i | SR1n | SR1o |
|---|---|---|---|---|
| sonnet-medium (control) | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| haiku-low | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| glm | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwen36 | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwencoder | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| deepseek | TODO /8 | TODO /8 | TODO /8 | TODO /8 |

Run id, commit and surface hash: TODO.

Classify every non-pass before anything counts. The classes are harness or oracle bug (fix and rescore), deployment artefact, tool-attributable, or model capability.

- **Classifications:** TODO. The flags help: `premature_claim` (SR1), `placement_rule` (SR1), `unrequested_change` (SR1n, SR1o), `required_call_missing` (SR1, SR1i), `unread_claim` (SR1, SR1i), and NEEDS_REVIEW's `readers_disagree` / `judge_error` (classified first).
- **Tool-attributable findings, each fixed and re-run (or accepted only with the operator's sign-off):** TODO. Watch for three things:
  - a "done" read off a transfer status (the description warns against it);
  - a provider name passed as the id that the hint did not resolve;
  - a placement rule chosen over the copy.
- **The run passes when** (design/10, gate 4): every control trial passes, with control trials lost to a harness bug or deployment artefact re-run after the fix, so each scenario ends with K passing control trials; and no tool-attributable finding is open (fixed and re-run, or accepted only with its cause outside the tool and the operator's sign-off).

## Gate 5: record

This file. To complete it:

1. Fill in the TODOs of gates 3 and 4: the live results and the panel's results.
2. Then, in the same PR, move `schedule_file_replication` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, and change its tier in README's tool table.

## Also on this branch

Branch commit `9f741b5` fixes four harness faults that an external review of the first package found. The fixes apply to every scenario, V3n and V3o included:

- a snapshot read a 404 or map-less QoS summary as "no requirements", and an unreadable block map as "0 bytes"; both are now snapshot errors;
- each file's start state is now waited for and recorded before the agent starts;
- teardown deleted every requirement in a file's *effective* summary, which includes rules inherited from the space root; it now deletes the rules the fixture made, and a rule only when its own `fileId` is one of the trial's entries;
- the trial directory named the scenario (`logs-<trial id>`); it is now opaque.

## External review

One frontier model (Gemini, gemini-4-argon-high) reviewed the package as of branch commit `22043ec`. The coordinator verified finding 2. Each finding and what was done:

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | blocking | The SR answer readers misread right answers: "with no placement rule" negates SR1's claim; a narrated "was in progress" reads as not yet; a clause about the copy that is already at `nordic-marine-dc` reads as a copy (SR1n) or as "yes, copied" (SR1o) | harness: resolved in the integration branch |
| 2 | blocking | A provider's name passed as the id got "it is not a provider that supports the space", false and contradicted by the next sentence; with the provider key and no provider list, non-support was asserted | fixed here (`e1fcd00`): the hint opens "Pass its id …, not its name"; the provider key alone gives "not a valid provider id for the space", with no claim about support; `spaceNotSupportedBy` gives "no provider with that id supports the space" |
| 3 | blocking | Gate 3 not run; the live test's "completed before whole" was biased (status timed before two calls, blocks after); the duplicate schedule disturbed the timed transfer; `replicationStatus` shadowed `transferState`; `LARGE_FILE_SIZE` uncalibrated, and an idle run understates a 12-trial load | live-test logic fixed here (`429bec1`): one poll reads the status, then the blocks, and only completed-and-not-covered counts; the duplicate schedule has its own test; `transferState` first; the timings for calibration are printed. The run and the calibration itself happen on the first live run, under panel-like load (gate 3) |
| 4 | major | The path lookup's errors bypassed the hints (a 403 got none); `/log.dat` passed the checks; a 404 after a path resolved blamed the file; the providers were not found from a path's space name | fixed here (`e1fcd00`): `/<space>/<name>` required; the lookup's permission errors get the permission hint; a 404 after a resolved path points at the target unless the key names the file field; the providers fall back to the path's space name |
| 5 | major | The block-map parsing reads a failed storage backend as 0 bytes, and a size of 0 as covered | harness: resolved in the integration branch |
| 6 | major | The live test leaked a probe file when a write or wait failed (registered too late), and read the local provider's name from the distribution | fixed here (`429bec1`): registered right after it is created; the name comes from the space's providers |
| 7 | major | SR1n gives a site name, so a model that follows the description refuses after listing the providers and never calls the tool there; no scenario gives an id | harness: resolved in the integration branch |
| 8 | minor | The description no longer said to re-check the blocks until whole; the arguments were not stripped | fixed here (`e1fcd00`): "re-check the target provider's blocks of the file until they cover all of it", naming no tool; both arguments stripped before the checks and the request. The surface hash above is recomputed |

The harness findings (1, 5, 7) concern `benchmark/k2val/` and `test/unit/k2val/`, which this branch no longer changes; the gate-4 text above describes the scenarios as built here, before those fixes.

### Round 2

Two further external reviews (Codex and Gemini, round 2) of the whole promotion package found defects that the first round's fixes left or introduced. This list is the tool-level part, fixed in `fix/r2-tools` (code and unit tests only: no container, live test or model call was run). The harness items of both reviews (`benchmark/k2val/`, `test/unit/k2val/`) are not changed here and are not claimed as resolved by this list.

| Review item | Finding | Status |
|---|---|---|
| Gemini 7.1 | The hints bypassed `bound_message` and `redact_endpoints`, so a message could pass 2000 characters or echo a configured host | fixed, `670fead`: every hint, at all three raise sites (the path lookup's permission hint, the POST's hints, the path lookup's 5xx hint), is redacted and bounded together with Onedata's own text |
| Gemini 7.2 | The target was matched against provider names that redaction had already changed, and `(no name)` matched itself | fixed, `670fead`: the raw `providerName` is compared (a name that redaction would change still gets "Pass its id"); a provider with no name is never matched; names are redacted only where shown |
| Gemini 7.3 | Not-found and forbidden hints were not limited to 4xx; for a file given as an id, any 404 blamed the file and hid the providers | fixed, `670fead`: every hint is for a 4xx status only (a 5xx passes unchanged, with no provider request); a 404 for an id that names no file field points at **both** arguments and lists the providers; the file is blamed only when `details.key` names the file field. A path's 404 still points at the target, as before |
| Codex 12 (the lookup) | A 5xx with `enoent` on the path lookup read `Path ... not found` | fixed for this tool, `670fead`: it gets the uncertain hint (the provider failed; the file may also not exist). The shared `get_file_id`, which the stable path-taking tools use, keeps its mapping: changing it is a change to stable tools' error hints and needs their own K=3 re-run (design/10). **Open for those tools** |
| Codex 13; Gemini 8.1 and 8.2 | Host redaction | fixed once for every tool, `c32a67f` (see design/11, Round 2). The provider names in a hint are shown as Onedata gives them, except that a configured host or a URL inside one is masked |
| Gemini 8.3 | A 3xx on `POST /transfers` was a success | fixed, `c32a67f` (any status outside 2xx is an error) |
| Codex 14 | The transfer live test only checked host names | fixed, `673a2c6`: it uses the shared loopback guard (every resolved address loopback, resolution pinned, proxies ignored) and checks and pins the source provider too |
| Codex 1–11, 15; Gemini 1–6, 9, 10 | Harness | not changed here |

## Decisions for the operator

1. **`destructiveHint: true`.** A replication only adds a replica. By the MCP definition it is additive, so the hint would be `false`. It is left `true`, as on `add_file_qos_requirement`, which is stable and also additive. Changing it changes the annotations, and clients may then run the tool without asking. Your call.
2. **`LARGE_FILE_SIZE`.** Decided: 128 MiB stays. Gate 3's first live run measured it whole 2.06 s after scheduling (about 62 MiB/s, unloaded, 2026-11-21), under the 15 s the earlier plan set as the bar for raising it. A larger file would load the space quota and the shared host, for a transfer a model rarely sees in progress either way. SR1 and SR1i instead fail a "whole" claim that was not read off the block map after scheduling (gate 4, "A whole claim is read from the blocks").
3. **No id-form scenario.** The reason is under gate 4; finding 7 of the external review reopens it, and it goes to the integration branch. Building one (SR1i: the target given by its id in the brief) would need the brief rendered with the local provider's id, and 48 more trials.
4. **Teardown deletes a little more than "only what the fixture made".** Branch commit `9f741b5` deletes the rules the fixture made and also any rule whose own `fileId` is one of the trial's entries, such as a rule an agent set on the trial's file. It never deletes a rule a file only inherits, from the space root or anywhere above the trial. The extra deletions stay inside the trial's own directory, and they keep a rule from outliving its file if Onedata does not drop it with the file. If you want strictly "only tracked", drop the provenance-checked loop in `FixtureBuilder.purge_dir`; the sweep then leaves agents' rules on leftover files.
5. **Accepting provider names.** The tool could resolve a name to its id, as `space_id` accepts a space name. That would change the schema's meaning. It is not done: a name now gets an error with the id in it.
