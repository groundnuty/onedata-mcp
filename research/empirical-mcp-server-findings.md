# Empirical findings — onedata-mcp server design

**Started 2026-05-02.** This file documents MCP-server design issues
surfaced by the PPAM 2026 multi-LLM benchmark — issues that 100/100
unit tests + the read-only `mcp_smoke.py` had not exposed because
those tests don't exercise the *cross-tool reasoning* an LLM agent
performs against live federation state.

Scope distinction:

- `empirical-onedata-25.0-findings.md` — Onedata-side observations
  (REST API quirks, federation eventual-consistency behaviour).
- This file — onedata-mcp wrapper-side observations (tool design,
  response shapes, cross-call consistency).
- `papers/ppam-2026/research/28-empirical-spec-corrections.md` —
  paper-text corrections.

---

## Meta-observation: the benchmark IS an integration test

The PPAM 2026 benchmark was designed to measure LLM behaviour against
a federated data API. Beyond that purpose, **running 5 LLMs (Claude
Sonnet 4.5 + 4 Cyfronet Forge models) over 18 scenarios on the live
SPICE federation surfaced concrete MCP-tool design issues that
synthetic unit tests had missed**.

Why: each LLM probes the tool surface differently. They fall into
slightly different traps. When a class of agents converges on the
same wrong answer for a scenario, the cause is almost always a tool
or fixture issue, not a model issue. The benchmark's diversity
makes the failure-mode signal cleaner than a single-model test
sweep would.

The findings below were filed after the K=1 sweep
`artefacts/20260502T112609/` (5 LLMs × 18 scenarios = 90 trials).

---

## M-1. `query_by_metadata` returns space-relative paths

**Surface:** `onedata_mcp/api/metadata.py::query_by_metadata`

**Behaviour:** the tool returns matches with `"path": "a6/batch01/f1.txt"`
— space-relative form, missing the `/<space>/` prefix.

**Why this matters:** every other tool in the surface uses absolute
paths (`/<space>/...`). Oracles + agent prompts both assume the
absolute form. The inconsistency forces the LLM to either (a) recognise
the gap and prepend the prefix (Claude did, OSS legs didn't) or (b)
return the path as-is and fail the oracle's path-set comparison.

**Evidence:** A6 (`path-set mismatch` for all 5 LLMs in run
20260502T112609; tool-call dump shows verbatim `a6/batch01/f1.txt`
in the tool response).

**Root cause:** the implementation passes through Onedata's
`list_files_recursively` response, which uses space-relative paths in
its `path` field. The Onedata REST quirk is documented; the wrapper
should normalize to the absolute form for consistency.

**Fix:** in `query_by_metadata`, prepend `/<space>/` to each match's
`path` before returning. Single-line change at the response-construction
site.

---

## M-2. Cross-scenario fixture pollution → space-wide queries see
        residue from other scenarios

**Surface:** `benchmark/fixture_runner.py::prepare_trial` + scenario
authoring assumptions.

**Behaviour:** each per-trial federation reset wipes only the
scenario's own subtree (`/<space>/<scenario_id>/`). Other scenarios'
fixtures from prior trials persist. Any scenario whose answer depends
on a *space-wide* query sees them.

**Concrete instance — A6 vs D5:**

- A6 fixture creates `/a6/batch01/{f1,f2}.txt` with
  `{pipeline_stage: raw}`.
- D5 fixture creates `/d5/sample.txt` with
  `{pipeline_stage: raw, owner: agent, created: ...}`.
- A6's brief asks for "every file in space `ppam_2026_mcp_tests`
  whose JSON metadata key `pipeline_stage` equals `raw`".
- After D5 has been run earlier in the same sweep, A6's space-wide
  `query_by_metadata(predicate="pipeline_stage=raw")` correctly
  returns 3 matches (a6/f1, a6/f2, d5/sample). The oracle expects
  only the 2 A6 files.

**Why this matters:** the harness's per-trial reset is subtree-
scoped, but A6's brief is space-scoped. The scenario design and
the harness's reset granularity are mismatched.

**Evidence:** A6 oracle diagnoses across all 5 LLMs in run
20260502T112609. Different LLMs hit the bug differently:
- Claude normalized paths to absolute → diagnosed as
  `unexpected: ['/d5/sample.txt']`
- OSS legs left paths space-relative → diagnosed as
  `missing all`

**Fix candidates:**

1. **Re-author A6 to subtree-scope its query.** Brief becomes "every
   file under `/<space>/a6/`...". Tool call passes `path=/a6/`. No
   harness change. Cleanest.
2. **Wipe ALL scenario subtrees at the start of each trial.** Slow;
   ~17× more federation work per trial.
3. **Avoid cross-scenario metadata-key collisions.** Have D5 use a
   unique metadata key like `experiment=D5` instead of the generic
   `pipeline_stage=raw`. Spot-fix; brittle (next collision waiting).

We pick (1) — preserves the harness contract, no slowdown, and the
re-authored scenario still tests the agent's ability to scope a
metadata query to a subtree (a real-world skill).

---

## M-3. Strict `space_id` parameter — no name→ID resolution

**Surface:** every MCP tool taking a `space_id` parameter:
- `list_space_providers`
- `list_space_transfers`
- (and any future tool requiring a space scope)

**Behaviour:** the parameter is documented as `Space id` (Onedata
hex spaceId) and is passed verbatim to `GET /spaces/<value>`. When
an LLM passes the human-readable name from the brief
(`ppam_2026_mcp_tests`), the API returns HTTP 403.

**Why 403 not 404?** Onedata's permission model treats unknown space
IDs as "you don't have access to this resource" — same shape as a
real auth failure. The error is misleading.

**Why this matters:** scenario briefs use the human name (everyone
knows the space as `ppam_2026_mcp_tests`, not as
`9742830720c0ef94496dad1d96595736ch776e`). The tool design forces
a two-call lookup pattern (`list_user_spaces` to find the spaceId,
*then* the actual call). Most LLMs don't perform that lookup
discipline reliably.

**Evidence:** D4 across the K=1 sweep:
- 4 of 5 models passed name verbatim → 403 → FAIL.
- Only Qwen3.6-35B noticed the 403, called `list_user_spaces`,
  retried with the resolved ID, and PASSed.

**This is a tool-affordance bug, not a model bug.** The simpler-
surface principle (per `feedback_metadata_simpler_than_harvester.md`
in the workbench memory): if the tool can do the resolution
internally, it should. Forcing the LLM to chain calls for name
resolution adds friction without testing anything we want to measure.

**Fix:** introduce a helper `resolve_space_id_or_name(value)` in
`onedata_mcp/api/spaces.py` that:
1. If `value` looks like a hex spaceId (38 chars, hex with `ch`
   separator), return as-is.
2. Otherwise, call `list_user_spaces` and look up by `name` or
   `spaceId` field equality.
3. Raise `ValueError` with the available names if nothing matches.

Wire into `list_space_providers`, `list_space_transfers`, and any
future tool. Update parameter docstrings to "space name or id".

---

## M-4 (lower urgency). Inactive Forge models still appear in
       `/v1/models`

Out of scope for the MCP server itself but documented in the
Cyfronet-facing report at
`artefacts/20260502T112609/REPORT_cyfronet.md`. Listed here as a
cross-reference: the benchmark surfaced 4 distinct Forge-API gaps
(tags-not-exposed, max_tokens-caps-undiscoverable, inactive-models-
still-in-catalogue, undifferentiated-400-errors) which are
recommended as a Cyfronet operations-side write-up.

---

## Future findings

This file accretes as the benchmark expands. New findings get a new
M-N section with the same shape:
- Surface (where the bug lives)
- Behaviour (what's wrong)
- Why this matters (downstream impact)
- Evidence (artefact run-id pointing at concrete trials)
- Fix (the code change applied or proposed)

Cross-reference each new entry from `IMPLEMENTATION_NOTES.md`'s
findings table.
