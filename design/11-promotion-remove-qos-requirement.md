# Promotion record: `remove_qos_requirement`

Status: **in progress** (2026-11-20). Gate 1 is done. Gate 2 is done except for `make conformance`, which has not been run. Gate 3's live test is built but has not been run. Gate 4's scenarios are built and unit-tested on the fake federation, but the run has not happened. The validation federation is on hold, so nothing here touched a live Onedata. The tool stays `experimental` until gates 2–4 are complete. It moves to `STABLE` in the PR that completes this record.

The short commit ids below are **branch commits** on `feat/promote-remove-qos-requirement`. A squash merge leaves them unreachable from `master`. **The anchor is the surface hash.**

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
| `remove_qos_requirement` alone | `471147719eef6f8c9e0b42c1ff6b3d7816ba014bebcbc02e23950923eddb7924` | `1e6ccb560597d0887484b0fdccfc0c59616cb2265ebe09944f79d619deef7b78` |
| the panel's K1 set (11 tools) | `4c8ef3f22fee7805e7c7ef72b4258a6a3bc4ee2f95ef1dafb7d0edd8ebf61b66` (= design/09's "existing 11") | `f72ad02e712d8f8d761095bcf54f978b8ad18bac1b10704973ecdc1c3d4f9a04` |

- `ac6141a` is the last commit that changes the surface; later commits leave it alone.
- The `tools/list` diff over the K1 set is this one line. Name, input schema and annotations are unchanged:

  ```
  -    "description": "Remove a QoS requirement by id. Does not roll back already-replicated data.",
  +    "description": "Remove a QoS requirement by id. Returns nothing on success.\nDoes not roll back already-replicated data.",
  ```

- **Earlier evidence is supporting only.** The triage of the experimental tools (2026-11-20) counted the earlier panel run (V1–V7) and reported 28 V3 trials, all passing, with the tool called and no error. Those were eight API configs at K=3 and two CLI agents. The figure is reported here, not recounted. That run used the old description; gate 4 needs a run at the surface above.

**Before the gate-4 run**, check that the surface has not moved:

    ONEDATA_MCP_TOOLS=remove_qos_requirement make -s tools-manifest | sha256sum   # expect 1e6ccb56...

## Gate 1: implementation (done, branch commit `6a9fe20`)

Every error path a model can trigger, and what the model now reads. The tests are in `test/unit/api/test_qos.py`, plus `test/unit/modules/test_qos_wrapper.py` for the text as it arrives through the MCP client.

| Path | Before | Now |
|---|---|---|
| Unknown id (404: `notFound`, or `posix`/`enoent`) | Onedata's message only | Says no requirement has this id: it may be removed already, or the value is not a requirement id (a file id is not one), and *a file's current QoS requirement ids are the keys of `requirements` in get_file_qos_summary's answer*. Onedata's message follows. |
| Unknown id, 404 page that is not JSON | `http_404` | The same hint. The page is never shown. |
| A file id passed as the id (404 or 400) | Onedata's message only | The same hint. A 400 reads "Onedata rejected … as a QoS requirement id". |
| A path, an empty id, `.` or `..` | Sent as is: `..` resolved to a `DELETE` on the API root, and a path to `//space/...` | Refused before any request, with the same pointer to where the ids are |
| `?` or `#` in the id | **Defect:** the path was cut short, which removed the requirement named by the part before it | The id is percent-encoded |
| Permission (403, `forbidden`, `eacces`, `eperm`) | Onedata's message only | Says the token may not manage QoS here (`space_manage_qos`), or is read-only, and that a retry with this id or another will not help |
| 500, transport errors | Unchanged | Unchanged. Nothing for the caller to fix. |
| Success (204) | Empty answer | Empty answer; the description now says so (gate 2) |

- The hints are keyed on the HTTP status, with the error id and errno as a fallback. The real 25.0 bodies are **unverified**: the unit tests use plausible shapes, and the live test prints the real ones.
- The protocol's "empty result" path has no separate case for a removal. Removing an id that names nothing is the unknown-id path above.
- `get_qos_requirement`, a stable tool, puts its id into the path the same way, unencoded. It is not changed here; a fix would need gates 1 and 3 for that tool.

## Gate 2: surface review (done except `make conformance`, branch commit `ac6141a`)

Checked against the protocol's rules; `test_the_description_follows_the_surface_rules` covers the first three:

- **Says what it returns:** it did not, and a removal answers with empty content. **Fixed:** "Returns nothing on success." This was the one real defect.
- **Names no tool that may not be offered alongside it:** yes. The description and the `qos_id` field name no tool.
- **Cites no document the model cannot read:** yes.
- **Errors tell the caller how to fix the call:** done in gate 1. The error text names `get_file_qos_summary`, although the description must not. Only an error of this tool carries the name, and that tool is the stable source of these ids. It is offered with this one in every shipped set: the default list, and the panel's K1 and K2. The exceptions are a hand-written `ONEDATA_MCP_TOOLS` and `ONEDATA_MCP_MATURITY=experimental`, under which the hint points to a tool that is not there.
- **`make conformance`: TODO.** It needs `npx` and a local port, and was not run here. Command: `make conformance`.
- **Open point, not fixed:** a file's QoS summary also lists requirements *inherited* from ancestor directories. Removing such an id removes the directory's requirement, for every file under it, and the description does not say so. No scenario exercises it, and the protocol does not call it a defect, so it is left as it is. If it is to be added, add it **before** the gate-4 run: after promotion the same change costs a smoke re-run.

## Gate 3: live check (built in branch commit `d5f7ff5`, not run)

[`test/live/test_qos_live.py`](../test/live/test_qos_live.py) calls the tools through an in-process MCP client, as a model would. It covers:

1. A requirement (`anyStorage`, one replica) is added on a probe file, removed, and then disappears from `get_file_qos_summary`. It prints how long that took.
2. Removing the **same id again** fails. The error must carry the unknown-id hint, and must not be the permission hint. It prints the error text: status, error id and Onedata's description.
3. An id that never existed (32 random hex digits) fails with a hint that points to the ids. It prints whether 25.0 answers 404 or 400.

Like the readiness live test, it is skipped unless `ONEDATA_MCP_LIVE_QOS=1`. It then refuses a provider or zone host that is not `127.0.0.1`, `localhost` or `*.datahub.test`. Here it was only collected, and skipped without the flag.

**To run:**

    make valfed-up                     # if the federation is not up
    make valfed-qos-live               # in omcp-live, TLS verified, the token passed in the environment
    # or, with the ONEDATA_* variables and ONEDATA_MCP_LIVE_QOS_SPACE set by hand:
    make test-qos-live

**Results: TODO.** Record here:

- the time the summary took to drop the id;
- the second removal's error text;
- the unknown id's error text.

If a status is not 404, or the error is the permission hint, change the hint mapping and the unit tests' bodies before the gate-4 run.

**If the summary takes any time to drop the id, give V3n and V3o a settle wait before the run.** Both now settle nothing: their federation axis reads the snapshot taken right after the answer. That snapshot would then miss a removal the summary had not yet shown, and a V3o trial that removed the rule could pass. V3 polls for the same reason.

## Gate 4: validation run (scenarios built in branch commits `a04db6d` and `90984db`, not run)

### Scenario coverage (design/10)

| The protocol asks for | Scenario | Brief | Fixture | Oracle |
|---|---|---|---|---|
| a task that needs it | **V3** remove rule (existing) | "A placement rule keeps `<dir>/log_20200107.dat` at vistula-dc. Remove that rule." | one file with a fulfilled rule `providerId=<local>` | mcp: says it was removed; federation: no requirement left after the settle (`qos_gone`) |
| "no, because" | **V3n** remove rule, none there | V3's, word for word | one file **replicated to the local site by a transfer**, no rule | mcp: says there is no rule to remove; a claim of removal fails, and so does a requirement changed when the answer is given; federation: no requirement, local copy still there |
| offered but not needed | **V3o** ask about rule | "Does a placement rule keep `<dir>/log_20200107.dat` at your site vistula-dc?" | V3's | mcp: says yes; a claim of removal fails, and so does the rule changed (removed, or removed and re-added) when the answer is given; federation: the rule built is still on the file, local copy still there |
| id, name and path variants | none added (see below) | | | |
| the federation's end state after a mutating tool's final answer | V3, V3n and V3o | | | V3's federation axis after the settle. V3n and V3o compare the requirement ids by id, against the fixture's, at the answer (mcp) and after (federation) |

- **Requires.** V3 *requires* the tool: no other K1 tool removes a requirement. V3n and V3o offer it without requiring it. They are the coverage the protocol asks of the tool, and they test that it is not misused.
- **The scenario set.** Each brief names no tool and gives no advice. The panel's K1 set (11 tools, `remove_qos_requirement` included) is offered in all three scenarios.
- **The V3n brief.** It keeps V3's premise ("a placement rule keeps …") on purpose: only the federation's state tells the two apart. A model that claims a removal without looking fails. The premise is half true, since the replica is at the site, so a model that reads the distribution alone may answer wrongly.
- **No V3p.** The tool takes one opaque argument, `qos_id`, which has no name or path form. V3's brief gives the file by path, and the model gets the id from `get_file_qos_summary`. A file-id-versus-path variant would test that stable tool's argument, not this one. The wrong kinds of id (a path, a file id, a made-up id) are gate-1 error paths with hints. The unit tests cover them, including a V3n trial with a made-up id.
- **The default range.** V3n and V3o are outside `V1..V7`, the default of `make k2val`, so name them.

### The run

- Panel: the six standing configs, with `sonnet-medium` as the control and `haiku-low`, `glm`, `qwen36`, `qwencoder` and `deepseek`. Each config has its own space.
- K = 8. That is 3 scenarios × 6 configs × 8 = 144 trials.

```
make valfed-up && make valfed-reset
SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
  make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
    K2VAL_SCENARIOS=V3,V3n,V3o K2VAL_K=8 \
    K2VAL_MODELS=sonnet-medium,haiku-low,glm,qwen36,qwencoder,deepseek
```

The report lands in `research/k2val-<run id>.md`.

### Results: TODO

| Config | V3 | V3n | V3o |
|---|---|---|---|
| sonnet-medium (control) | TODO /8 | TODO /8 | TODO /8 |
| haiku-low | TODO /8 | TODO /8 | TODO /8 |
| glm | TODO /8 | TODO /8 | TODO /8 |
| qwen36 | TODO /8 | TODO /8 | TODO /8 |
| qwencoder | TODO /8 | TODO /8 | TODO /8 |
| deepseek | TODO /8 | TODO /8 | TODO /8 |

Run id, commit and surface hash: TODO.

Classify every non-pass before anything counts. The classes are harness or oracle bug (fix and rescore), deployment artefact, tool-attributable, or model capability. The answer readers of V3n and V3o are regular expressions built from a table of expected answers (`test/unit/k2val/test_oracles.py`). An answer they misread is an oracle bug.

- **Classifications:** TODO.
- **Tool-attributable findings, each fixed and re-run or accepted with a reason:** TODO.
- **The run passes when:**
  - the control passes all three scenarios, or each control failure is shown to be a harness bug or an artefact;
  - no tool-attributable finding is left open.

## Gate 5: record

This file. To complete it:

1. Fill in the TODOs of gates 2 to 4: conformance, the live results and the panel's results.
2. Then, in the same PR, move `remove_qos_requirement` from `EXPERIMENTAL` to `STABLE` in `onedata_mcp/tool_maturity.py`, and change its tier in README's tool table.

## Also on this branch

Branch commit `65df0b3` fixes a unit test that failed on master `4cd4c47`. Once the Claude configs moved to the subscription backend, they had no key file, and the test still expected every config to have one. `make check` was red on master before this branch.
