# Promotion record: `remove_qos_requirement`

Status: **in progress** (2026-11-20). Gate 1 is done, and was redone after two external reviews (see [External review](#external-review)). Gate 2 is done (`make conformance` passed on the integrated server, 2026-11-21). Gate 3's live test is built but has not been run. Gate 4's scenarios are built and unit-tested on the fake federation, but the run has not happened. The validation federation is on hold, so nothing here touched a live Onedata. The tool stays `experimental` until gates 2–4 are complete. It moves to `STABLE` in the PR that completes this record.

The short commit ids below are **branch commits** on `feat/promote-remove-qos-requirement`; the fixes that answer the external review are on `fix/qos-review-findings`. A squash merge leaves them unreachable from `master`. **The anchor is the surface hash.**

The protocol is [`10-tool-maturity.md`](10-tool-maturity.md). The panel and its scenarios are in [`08-readiness-validation-panel.md`](08-readiness-validation-panel.md).

## The tool

- **REST call:** `DELETE /qos_requirements/{qid}` on the Oneprovider. The swagger (oneprovider-swagger tag `25.0`, commit `39da981`, `paths/qos_requirements/qid.yaml`) lists 204, 400, 403, 404 and 500. Every error has the generic `Error` body, and the error ids are not specified.
- **Input:** `qos_id`, a string. There is no list input, so no size cap applies.
- **Mutating:** it is in `token_policy.WRITE_TOOL_NAMES` and annotated `destructiveHint: true`.
- **Code:** `onedata_mcp/modules/qos.py` (surface) and `onedata_mcp/api/qos.py` (call and errors).

## The surface being promoted

The listing is `scripts/tools_manifest.py`'s output (`make -s tools-manifest`) with `ONEDATA_MCP_TOOLS` set to the selection, hashed with sha256, the same way as [`09-readiness-freeze-record.md`](09-readiness-freeze-record.md). Without `-s`, make prints the recipe line too, and the hash differs.

| Selection | Before (master `4cd4c47`) | Promoted surface (since branch commit `ac6141a`) |
|---|---|---|
| `remove_qos_requirement` alone | `471147719eef6f8c9e0b42c1ff6b3d7816ba014bebcbc02e23950923eddb7924` | `107ce0b82032cd183a25ec0f323f7eddc6b40bb38c9b05cb95f54400da1657c7` |
| the panel's K1 set (11 tools) | `4c8ef3f22fee7805e7c7ef72b4258a6a3bc4ee2f95ef1dafb7d0edd8ebf61b66` (= design/09's "existing 11") | `dc1d10dcce1a81e8b15c273adb99d007db9a6fdd169ce366ed43b5a38d49cd2e` |

- `ac6141a` is the last commit that changes the surface; later commits leave it alone.
- The `tools/list` diff over the K1 set is this one line. Name, input schema and annotations are unchanged:

  ```
  -    "description": "Remove a QoS requirement by id. Does not roll back already-replicated data.",
  +    "description": "Remove a QoS requirement by id. Returns nothing on success.\nDoes not roll back already-replicated data. A requirement listed for a file\nmay be set on one of its parent directories: removing it removes it for every\nfile under that directory.",
  ```

- **Earlier evidence is supporting only.** The triage of the experimental tools (2026-11-20) counted the earlier panel run (V1–V7) and reported 28 V3 trials, all passing, with the tool called and no error. Those were eight API configs at K=3 and two CLI agents. The figure is reported here, not recounted. That run used the old description; gate 4 needs a run at the surface above.

**Before the gate-4 run**, check that the surface has not moved:

    ONEDATA_MCP_TOOLS=remove_qos_requirement make -s tools-manifest | sha256sum   # expect 107ce0b8...

The review fixes (below) change the tool's errors and results, not its `tools/list` entry. Both hashes were recomputed on `fix/qos-review-findings` with the commands above (the second with the 11 tools of the K1 set in `ONEDATA_MCP_TOOLS`), and are **unchanged**:

    107ce0b82032cd183a25ec0f323f7eddc6b40bb38c9b05cb95f54400da1657c7  -
    dc1d10dcce1a81e8b15c273adb99d007db9a6fdd169ce366ed43b5a38d49cd2e  -

## Gate 1: implementation (done, branch commit `6a9fe20`; the error paths redone on `fix/qos-review-findings` after the external review)

Every error path a model can trigger, and what the model now reads. The tests are in `test/unit/api/test_qos.py`, plus `test/unit/modules/test_qos_wrapper.py` for the text as it arrives through the MCP client.

A *structured* Onedata error is a JSON body whose `error.id` is `notFound` or whose `error.details.errno` is `enoent` (the field names of the swagger's `Error` model and of the parser in `onedata_mcp/utils.py`). A specific diagnosis needs one of these on a 4xx status. A bare or HTML 404, an unrecognised 400 and a 5xx that mentions a missing item get a generic hint that keeps the uncertainty. Every hint names no tool, because the tool that shows a file's QoS summary may not be offered alongside this one.

| Path | Before | Now |
|---|---|---|
| Unknown id: a structured not-found on a 404 or 400 | Onedata's message only | Says no requirement has this id: it may be removed already, or the value is not a requirement id (a file id is not one), and *a file's current QoS requirement ids are the keys of `requirements` in its QoS summary*. Onedata's message follows. |
| A 404 that does not say the requirement is missing (an HTML page, no body, another error id) | Said the requirement does not exist | "Onedata answered 404 without saying the requirement is missing; check the id and the provider", and the pointer to the ids. The page is never shown. |
| An unrecognised 400 (including a file id passed as the id) | "Onedata rejected … as a QoS requirement id" | "Onedata answered 400 without saying the requirement is missing; check that `<id>` is a QoS requirement id (a file id is not one) and the provider", and the pointer. It does not blame the id for a POSIX or domain error. |
| A 5xx that carries `notFound` or `enoent` | Took the not-found branch | "Onedata answered 5xx and mentioned a missing item; that does not show that the requirement is gone, so the removal is not confirmed" |
| Any other 5xx, a 401, transport errors | Unchanged | Unchanged. Nothing for the caller to fix. |
| A redirect (3xx) | **Defect:** a 3xx was returned as an empty success while the requirement was still there | An error: "Onedata answered 3xx, not 204: … the removal is not confirmed and the requirement may still exist". `request` now raises on any non-2xx, for every tool. The redirect's page and target are never shown. |
| A path, an empty id, `.` or `..` | Sent as is: `..` resolved to a `DELETE` on the API root, and a path to `//space/...` | Refused before any request, with the pointer to where the ids are |
| Whitespace around the id | Sent as `%20`, naming another id | Stripped; the stripped id is used |
| Whitespace inside the id, a control character, `=` (a QoS expression passed as an id), a backslash | Sent | Refused before any request, saying what the value looks like, with the pointer |
| `?` or `#` in the id | **Defect:** the path was cut short, which removed the requirement named by the part before it | The id is percent-encoded |
| Permission: a 403, or a 400 or 404 carrying `forbidden`, `eacces` or `eperm` | Onedata's message only | Says the token may lack the privilege to manage QoS in the space this requirement belongs to (`space_manage_qos`), or be read-only; that a retry with this id will not help; and that a requirement in a space where the token has the privilege is not affected |
| Success (204, as documented) | Empty answer | Empty answer; the description says so (gate 2). A 202 is an error ("accepted but removal not confirmed"), and so is any other 2xx. |

- Which statuses and error ids a 25.0 provider really answers is **unverified**: the swagger lists 400, 403, 404 and 500 with the generic body only. The unit tests use plausible shapes. The live test asserts that an unknown id fails with a 400 or 404 and that the hint fits the body, and prints the real answers.
- **Host-free, bounded errors** (the change is in `onedata_mcp/utils.py`, so it applies to every tool). With or without `ONEDATA_MCP_NODE_NAMES`, no error text a model reads names a host or carries a URL:
  - the Onezone and Oneprovider hosts this server is configured with are replaced by `onezone` and `oneprovider` (a node-list entry for the same host wins);
  - a URL is masked;
  - the value of a domain-bearing field of the error (`serviceDomain`, which every 25.0 error carries in its `ctx` and which names the service that answered, a peer provider included; also `domain`, `domainName`, `subdomain`, `host`, `hostname`, `fqdn`) reads the declared name when the whole value is one known node, else `a provider`;
  - a host name written into the free text of a description is NOT masked unless it is a configured host, a listed node or part of a URL (the residual recorded under the External review);
  - `host=` and `port=` pairs are still masked only with a node list. An IP address that no field names as a host passes.
  - The message is bounded to 2000 characters, cut after the redaction, and a path segment (an id) is echoed at no more than 128 characters.
  - This fixes a leak; it does not change what a tool means (design/10). The tests that pinned "no list, so byte for byte" were rewritten to the new rule.
- The protocol's "empty result" path has no separate case for a removal. Removing an id that names nothing is the unknown-id path above.
- `get_qos_requirement`, a stable tool, shares the id guard. See [its own section](#get_qos_requirement-a-stable-tool-changed-on-this-branch).

## Gate 2: surface review (done, branch commit `ac6141a`)

Checked against the protocol's rules; `test_the_description_follows_the_surface_rules` covers the first three:

- **Says what it returns:** it did not, and a removal answers with empty content. **Fixed:** "Returns nothing on success." This was the one real defect.
- **Names no tool that may not be offered alongside it:** yes. The description and the `qos_id` field name no tool.
- **Cites no document the model cannot read:** yes.
- **Errors tell the caller how to fix the call:** done in gate 1. The first version of the error text named `get_file_qos_summary`, which is not offered in a hand-written `ONEDATA_MCP_TOOLS` and may not be under `ONEDATA_MCP_MATURITY`. After the external review the text names no tool: it says a file's requirement ids are "the keys of `requirements` in its QoS summary".
- **`make conformance`: passed** on the integrated server (the integration branch `feat/promotion-harness` at `501c867`, which carries all five packages), run 2026-11-21 (`conformance-results/20261121T010707Z`) with `@modelcontextprotocol/conformance@0.1.16`, the version `make conformance` now pins: 12 passed, 20 failed, every failure expected (`conformance-baseline.yaml`: scenarios that need prompts, resources, completion, sampling, elicitation or the suite's own test tools, none of which this server has), and the baseline check passed. Conformance tests the protocol, not one tool, so this one run serves design/11–15.
- **Inherited requirements:** a file's QoS summary also lists requirements inherited from ancestor directories, and removing such an id removes the directory's requirement for every file under it. **Fixed before the gate-4 run:** the description now says so.

## Gate 3: live check (built in branch commit `d5f7ff5`, reworked on `fix/qos-review-findings`, not run)

[`test/live/test_qos_live.py`](../test/live/test_qos_live.py) calls the tools through an in-process MCP client, as a model would. It covers:

1. A requirement (`anyStorage`, one replica) is added on a probe file and **polled until the summary lists it** (not assumed to be immediate). `get_qos_requirement` reads it back (id, file id, expression, replica count). The tool removes it, answering nothing, and the summary stops listing it. It prints how long the appearing and the disappearing took.
2. Removing the **same id again** fails. The test asserts the actual status (400 or 404) and that the hint fits the body: the specific hint only if Onedata's body says `notFound` or `enoent`, the generic one otherwise, never the permission hint. It prints the error text.
3. An id that never existed (32 random hex digits) fails the same way. It prints whether 25.0 answers 404 or 400.
4. A real **file id** passed as the requirement id fails the same way, and the requirement that was added is still listed afterwards.
5. `get_qos_requirement` for an id that never existed fails with a 400 or 404. This and the read-back in item 1 are the live check of that tool (see below).

Every read of the summary must carry a `requirements` mapping; a missing field fails the test, and is never read as "gone".

**Cleanup** removes only the requirement ids the test created, which it tracks as soon as each add returns. It never takes ids from a summary: an effective summary also lists requirements inherited from ancestor directories, the space root included. Each cleanup step may fail without stopping the next.

Like the readiness live test, it is skipped unless `ONEDATA_MCP_LIVE_QOS=1`. It then refuses a provider or zone host that is not `127.0.0.1`, `localhost` or `*.datahub.test`, **and** refuses unless every address the host resolves to is a loopback address (a `*.datahub.test` name mapped to another address is refused). Once it passes, name resolution for those hosts is pinned to the addresses that were checked, the URL keeps its host name for TLS, and proxy variables are ignored. The guard, the pin and the cleanup are unit-tested in `test/unit/api/test_qos.py`; the test itself was only collected, and skipped without the flag. **Not built:** a live 403 case. It needs a token without `space_manage_qos`, which the test is not given; the permission hint is covered by unit tests only.

**To run:**

    make valfed-up                     # if the federation is not up
    make valfed-qos-live               # in omcp-live, TLS verified, the token passed in the environment
    # or, with the ONEDATA_* variables and ONEDATA_MCP_LIVE_QOS_SPACE set by hand:
    make test-qos-live

**Results: TODO.** Record here:

- the time the summary took to list the new id, and to drop it after the removal;
- the second removal's status and error text;
- the unknown id's, and the file id's, status and error text;
- the status and text of `get_qos_requirement` for an unknown id, and its read-back of a real requirement.

If a status is outside 400 and 404, or the hint does not fit the body Onedata sent, change the hint mapping and the unit tests' bodies before the gate-4 run.

**V3n and V3o wait out a settle window.** They are restraint scenarios (design/08, "Scoring"): any successful write call fails them, and the federation is read again 60 s after the answer, even when the first reading matched, so a removal the summary shows late is still caught. If the live test shows the summary taking longer than that to drop an id, lengthen their `settle_s`. V3 and V3i, which require the tool, also wait out the whole window and are read again after it (design/08, "Scoring"); they fail if a placement rule was added to the trial's file.

## Gate 4: validation run (scenarios built in branch commits `a04db6d` and `90984db`, not run)

**Gate 4 runs on one integrated commit.** The packages of `remove_qos_requirement`, `schedule_file_replication`, `get_file_attributes`, `list_children` and `create_directory` are validated together, on one commit of the integration branch `feat/promotion-harness` that carries all five and the harness they share (design/08's "Scoring" and "Reading the answer"). The tool surfaces there are those of merge `d946358`, the last commit that changes any of them. Each record therefore cites the same offered-set hashes, not its own package's K1 hash. Computed as above on that tree:

| Offered set | Hash on the integrated commit |
|---|---|
| the panel's K1 set (11 tools; V1–V3, V3n, V3o, SR*, FA*) | `9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3` |
| the panel's K2 set (13 tools; V4–V7) | `6f502cb473137d4e972434c039830d7388b3e87f181dcc0386e0d0747fbc141b` |
| FILE_TOOLS (6 tools; LC*, CD*) | `c030429d0e04de218f50a1bda935d17e84026c8c24ea1a61fd6d91b0bac31922` |

The tool's own hash is unchanged by the integration. The run's record names the commit it ran on, and that commit's hashes must equal these.

### Scenario coverage (design/10)

| The protocol asks for | Scenario | Brief | Fixture | Oracle |
|---|---|---|---|---|
| a task that needs it | **V3** remove rule (existing) | "A placement rule keeps `<dir>/log_20200107.dat` at vistula-dc. Remove that rule." | one file with a fulfilled rule `providerId=<local>` | mcp: says it was removed (SAYS_REMOVED), and the fixture's rule was gone when the answer was given (else PREMATURE CLAIM); required: a successful `remove_qos_requirement` of the fixture's rule id; federation: no requirement left after the settle (`qos_gone`) |
| "no, because" | **V3n** remove rule, none there | V3's, word for word | one file **replicated to the local site by a transfer**, no rule; it starts only once its effective summary reads with no rule at all (inherited ones included) | mcp: says there is no rule to remove (SAYS_NO_RULE); a claim of removal fails. Restraint: any successful write fails; the federation equals the start state at the answer and 60 s later |
| offered but not needed | **V3o** ask about rule | "Does a placement rule keep `<dir>/log_20200107.dat` at your site vistula-dc?" | V3's | mcp: says yes (SAYS_RULE); a claim of removal fails. Restraint as V3n: a rule removed and re-added fails on the recorded calls, even when the summary reads as before |
| id, name and path variants | **V3i** remove rule, by id | "The placement rule with the id `<qos id>` keeps a file at vistula-dc. Remove that rule." | V3's | as V3: removed (REMOVED), gone at the answer, and a successful `remove_qos_requirement` of that id (the id form of `qos_id`, given directly) |
| the federation's end state after a mutating tool's final answer | V3, V3n and V3o | | | V3's federation axis after the settle. V3n and V3o compare the requirement ids by id, against the fixture's, at the answer (mcp) and after (federation) |

- **Requires.** V3 *requires* the tool: no other K1 tool removes a requirement. V3n and V3o offer it without requiring it. They are the coverage the protocol asks of the tool, and they test that it is not misused.
- **The scenario set.** Each brief names no tool and gives no advice. The panel's K1 set (11 tools, `remove_qos_requirement` included) is offered in all three scenarios.
- **The V3n brief.** It keeps V3's premise ("a placement rule keeps …") on purpose: only the federation's state tells the two apart. A model that claims a removal without looking fails. The premise is half true, since the replica is at the site, so a model that reads the distribution alone may answer wrongly.
- **V3i, the direct-id task** (added after the second external review, Codex #15). The brief gives the requirement's id itself, so the model can pass it on without reading the QoS summary.
- **No V3p.** The tool takes one opaque argument, `qos_id`, which has no name or path form. V3's brief gives the file by path, and the model gets the id from `get_file_qos_summary`. A file-id-versus-path variant would test that stable tool's argument, not this one. The wrong kinds of id (a path, a file id, a made-up id) are gate-1 error paths with hints. The unit tests cover them, including a V3n trial with a made-up id.
- **The default range.** V3n and V3o are outside `V1..V7`, the default of `make k2val`, so name them.
- **Scoring and reading.** A trial passes only when its answer is right and every check that applies passes (design/08, "Scoring"); `runner.py` no longer scores from the answer axis alone. Each answer is read by the scenario's deterministic reader and by the judge; a disagreement is NEEDS_REVIEW. The external reviews' misreadings of V3, V3n and V3o are paired cases of the readers' tests (`test_the_reviewed_answers_get_the_label_they_claim`).

### The run

- Panel: the six standing configs, with `sonnet-medium` as the control and `haiku-low`, `glm`, `qwen36`, `qwencoder` and `deepseek`. Each config has its own space.
- K = 8. That is 4 scenarios × 6 configs × 8 = 192 trials.

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_SCENARIOS=V3,V3i,V3n,V3o K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

The report lands in `research/k2val-<run id>.md`.

### Results: TODO

| Config | V3 | V3i | V3n | V3o |
|---|---|---|---|---|
| sonnet-medium (control) | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| haiku-low | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| glm | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwen36 | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| qwencoder | TODO /8 | TODO /8 | TODO /8 | TODO /8 |
| deepseek | TODO /8 | TODO /8 | TODO /8 | TODO /8 |

Run id, commit and surface hash: TODO.

Classify every non-pass before anything counts, every NEEDS_REVIEW first (which reading is right). The classes are harness or oracle bug (fix and rescore), deployment artefact, tool-attributable, or model capability. An answer a reader misread is an oracle bug.

- **Classifications:** TODO.
- **Tool-attributable findings, each fixed and re-run (or accepted only with the operator's sign-off):** TODO.
- **The run passes when** (design/10, gate 4): every control trial passes, with control trials lost to a harness bug or deployment artefact re-run after the fix, so each scenario ends with K passing control trials; and no tool-attributable finding is open (fixed and re-run, or accepted only with its cause outside the tool and the operator's sign-off).

## Gate 5: record

This file. To complete it:

1. Fill in the TODOs of gates 3 and 4: the live results and the panel's results.
2. Check that every row of [External review](#external-review) is closed, and that the items listed as open there are done.
3. Run `get_qos_requirement`'s smoke re-run (below) and record it.
4. Then, in the same PR, move `remove_qos_requirement` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, and change its tier in README's tool table.

## `get_qos_requirement`: a stable tool changed on this branch

An earlier version of this record said `get_qos_requirement` was untouched. It was not. `get_qos_requirement` is a stable tool, and its code changed twice:

- **master (#31, `4047a56`):** its id is percent-encoded into the path, and refused when empty, a path (a `/`), or a dot segment.
- **this branch:** it shares the id guard of `remove_qos_requirement`. The id is stripped of surrounding whitespace; whitespace inside, a control character, `=` and a backslash are refused before any request, each with a hint that names no tool.

Its `tools/list` entry (name, description, input schema, annotations) is unchanged; the K1 hash above covers it. What changed is its **behaviour** in design/10's sense: the error a model reads for a wrong kind of id. `get_file_qos_summary` calls it for each requirement and swallows a failure (`onedata_mcp/modules/qos.py`), so a failing getter would not show through the summary. That is why the getter has its own live checks.

- **Gate 1 (done):** the tests are `test_get_qos_requirement_*` in `test/unit/api/test_qos.py` (returns the full record; percent-encodes `?` and `#`; refuses each kind of wrong id, with its hint, before any request; strips surrounding whitespace and requests the stripped id) and `test_get_qos_requirement_refuses_the_same_ids_with_a_hint` in `test/unit/modules/test_qos_wrapper.py` (through the MCP client).
- **Gate 3 (built, not run):** `test_qos_live.py` reads back a real requirement (success) and reads an id that never existed (not found, asserting a 400 or 404). The federation is on hold.
- **Smoke re-run (owed, not run):** the amended protocol sends a change to the wording of a stable tool's error hints back to the K=3 smoke re-run. The new id-guard hints are such a change, so `get_qos_requirement` owes the re-run of its scenarios on the smoke pair (Qwen3-Coder and Haiku at effort low). It stays stable if no new tool-attributable failure appears. TODO: run it once the federation is off hold, and record the result here.

## External review

Two external frontier models reviewed the package before the validation run, each through a jailed wrapper (gate 5 of design/10). Codex gave 13 findings and Gemini 9, numbered as in their reports. Neither ran `make conformance`, the surface hashes or a live Onedata. The hashes were recomputed above; conformance has since passed (gate 2) and the live test is still open.

The status is one of: **fixed in this branch** (`fix/qos-review-findings`); **harness: resolved in the harness change** (the oracles, runner, fixtures, scenario coverage and protocol, which are not tool code); or **open**, with what is missing.

### Codex

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | BLOCKING | A fabricated V3 removal counts as a pass; V3 ignores `at_answer` | harness: resolved in the harness change |
| 2 | BLOCKING | V3n and V3o can pass after an unauthorized mutation (first snapshot reused as the settled one) | harness: resolved in the harness change |
| 3 | BLOCKING | The answer readers reverse clear answers in all three scenarios | harness: resolved in the harness change |
| 4 | BLOCKING | The harness can mistake an unreadable summary for an empty one | harness: resolved in the harness change |
| 5 | BLOCKING | Cleanup can delete a rule that belongs to the whole space | live test: fixed in this branch (cleanup removes only the ids the test tracked). `benchmark/k2val/fixtures.py`: harness: resolved in the harness change |
| 6 | MAJOR | The planned run leaves a gate-4 coverage row empty (no direct-id task; name and path forms) | harness: resolved in the harness change |
| 7 | MAJOR | The rendered path reveals the scenario | harness: resolved in the harness change |
| 8 | MAJOR | Error hints assert more than the response establishes; the id lookup hint needs a tool that may not be offered | fixed in this branch |
| 9 | MAJOR | A redirect can look like a successful deletion | fixed in this branch |
| 10 | MAJOR | Host-free, bounded errors are only partly tested; the request path repeats a long id | fixed in this branch |
| 11 | MAJOR | The live checks have locality and assertion gaps | fixed in this branch |
| 12 | MAJOR | The record overlooks a changed stable tool (`get_qos_requirement`) | fixed in this branch (this record; live GET checks). The smoke re-run it owes is **open** |
| 13 | MAJOR | Gate 4 can pass without usable control evidence | harness: resolved in the harness change (protocol; design/10 as amended in #32 already requires K passing control trials and all of the tool's scenarios) |

### Gemini

| # | Severity | Finding | Status |
|---|---|---|---|
| 1 | BLOCKING | The V3 oracle ignores the federation's end state for `mcp_pass`, and `at_answer` | harness: resolved in the harness change |
| 2 | BLOCKING | V3's `claims_removed` has false positives and false negatives | harness: resolved in the harness change |
| 3 | BLOCKING | The V3n and V3o answer readers misclassify realistic answers | harness: resolved in the harness change |
| 4 | BLOCKING | Gates 2 and 3 are unrun; V3n and V3o have no settle wait; local coverage is missing from the V3 variants' `mcp_pass` | settle wait and local coverage: harness: resolved in the harness change. `make conformance`: passed (gate 2, 2026-11-21). The live test is **open** (the federation is on hold) |
| 5 | MAJOR | Over-broad 404 and 400 hint mapping | fixed in this branch |
| 6 | MAJOR | `get_qos_requirement` was changed without being recorded (6.1); the id is not stripped and is under-validated (6.2) | fixed in this branch (6.1: this record; 6.2: the id guard) |
| 7 | MAJOR | The live cleanup can delete ancestor rules; no live check of a file id, of a 403, or of the status | fixed in this branch for the cleanup, the file id and the status. The live 403 case is **open**: it needs a token without `space_manage_qos`, and the permission hint is covered by unit tests only |
| 8 | MAJOR | Protocol loopholes in design/10 | harness: resolved in the harness change (protocol; design/10 as amended in #32) |
| 9 | MINOR | `sweep_space` does not clean rules on the space root (9.1); a JSON error body is unredacted without a node list (9.2) | 9.1: harness: resolved in the harness change. 9.2: fixed in this branch |

### Round 2

Two further external reviews (Codex and Gemini, round 2) of the whole promotion package found defects that the first round's fixes left or introduced. This list is the tool-level part, fixed in `fix/r2-tools` (code and unit tests only: no container, live test or model call was run). The harness items of both reviews (`benchmark/k2val/`, `test/unit/k2val/`) are not changed here and are not claimed as resolved by this list.

| Review item | Finding | Status |
|---|---|---|
| Codex 12 and earlier #9; Gemini 8.3 | DELETE read a 202 as a completed removal, and a 3xx was an error in this tool only | fixed in `fix/r2-tools`, `c32a67f`: the tool succeeds only on a 204; a 202 or any other 2xx is an error that says the removal was accepted (or answered) but is not confirmed and the requirement may still exist; `request` itself raises on any status outside 2xx, so no tool returns a redirect as a result. The redirect hint of this tool is unchanged in wording |
| Codex 13; Gemini 8.1 | The bare-hostname heuristic masked real text (`xattr.user.id`, `file.name`, `notes.v1.md`) and still missed peers | fixed, `c32a67f`: the heuristic (`_mask_bare_host`, the top-level-domain lists) is **removed**. Only structural redaction remains: the configured Onezone and Oneprovider hosts, the `ONEDATA_MCP_NODE_NAMES` entries, URLs, and the values of domain-bearing fields. The 2000-character bound is kept |
| Gemini 8.2 | A domain-bearing field's `a provider` overwrote a configured or listed node's declared name | fixed, `c32a67f`: the node-name mapping runs first. A value that is, as a whole, one known node (with or without its port) reads as that node's declared name; anything else, a known node mixed with other text included, reads `a provider` |
| Gemini 8.3 | A 3xx was a success in `request` and in the direct download and CDMI move calls | fixed, `c32a67f`: any status outside 2xx is an error, redirect page and target never shown |
| Codex 14 (QoS part) | The live guard | the QoS live test already pinned loopback resolution and proxies; the guard is now shared with the other live tests, `673a2c6` |
| Codex 15 | No QoS trial gives the requirement id directly | harness: not changed here |
| Codex 1–11, Gemini 1–6, 9, 10 | Scoring, federation evidence, rubrics, judge, fixtures, report | harness: not changed here |

**Residual (known, accepted).** A peer host written into the free text of an error description, or into any field that is not domain-bearing, is not masked unless it is a configured host, a listed node or part of a URL. Guessing hosts from the shape of a word was removed because it rewrote attribute and file names; a deploy that must hide its peers lists them in `ONEDATA_MCP_NODE_NAMES`. A unit test pins this (`test_an_unlisted_host_name_in_free_text_is_not_masked_a_known_residual`).

Gate 1 above was updated to match this round (redaction, 204-only success, redirects).

## Also on this branch

Branch commit `65df0b3` fixes a unit test that failed on master `4cd4c47`. Once the Claude configs moved to the subscription backend, they had no key file, and the test still expected every config to have one. `make check` was red on master before this branch.
