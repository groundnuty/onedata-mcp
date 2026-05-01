# Design decision: scenario-set authoring choices

**Status:** decided 2026-05-01.
**Lives at:** `benchmark/scenarios.py` (18 scenarios, drift-tested in `test/unit/test_scenarios.py`).
**Frozen:** the scenarios are pre-registered before any LLM run, per τ-bench convention (paper §4 ¶"Tasks").

## What this doc captures

The scenario *briefs* are mostly self-explanatory. This doc records the
non-obvious authoring choices, especially where we deviated from the
original paper-spec sketches in `papers/ppam-2026/research/22-mcp-implementation-spec.md`
because of constraints that surfaced during implementation.

## Choice 1: A4 reframed from cross-space to cross-directory

**Spec §6 had:** "Cross-space copy with metadata."
**Now:** Cross-directory copy within `ppam_2026_mcp_tests`.

**Why:** the live federation hosts only one designated benchmark space
(`ppam_2026_mcp_tests`, supported by cloud-pl + Cloud-SK; see
`papers/ppam-2026/research/27-benchmark-space-snapshot.md`). Provisioning
a second space + supporting it from both providers would have used
another 20 GiB of POSIX storage and added an admin step (token mint +
support flow) for marginal scenario value — A4's tool-composition
exercise (`download_file` + `get_file_metadata` + `create_file` +
`set_file_metadata`) doesn't depend on cross-space semantics; cross-
*directory* exercises the same composition.

**Paper-text edit needed:** the spec example task list calls A4 "cross-
space copy". The published paper draft's Table 3 only says "Cross-space
copy with metadata" — the writing agent can either keep that phrasing
(the brief still produces the same metric-relevant tool sequence) or
soften to "cross-directory" for fidelity. Either is defensible.

## Choice 2: P2 unfulfillable expression is `country=DE`

**Spec §6 had:** "QoS-violation diagnostic" (no specific expression named).
**Now:** Fixture file with `country=DE` rule, which is unfulfillable
because our PL+SK pair has no German provider — the rule sits in
`impossible` status.

**Why:** static-tier oracle needs a deterministic violation. `country=DE`
is unambiguously unfulfillable on our deployment yet syntactically valid
(so the agent reads a real `impossible` status, not an `INVALID_QOS_EXPR`
parse error).

**Risk:** if the federation later acquires a German provider (azure-
interway's `Cloud` and `Edge` providers come back online — both DE), the
fixture rule would suddenly become fulfillable. Mitigation: at federation-
reset time the runner can verify P2's fixture rule is still `impossible`
before declaring the trial ready; if not, surface as `RESET_FAIL` and
re-author the scenario.

**Paper-text impact:** none — the brief just says "violates that rule",
no specific country named.

## Choice 3: P5 conflict is `country=PL` ∩ `country=SK` with replicas=1 each

**Spec §6 had:** "QoS conflict resolution" (storage-type conflict
implied: SSD vs HDD).
**Now:** Two `country=*` rules with `replicas_num=1` each, where the
file can satisfy at most one at a time without growing total replicas.

**Why:** our two providers don't expose `type=ssd` / `type=hdd` QoS
attributes (those are admin-configured per storage backend; both PL
and SK posix-local backends have no such tags). Country-based conflicts
are the simplest deterministic conflict we can author given the
federation's actual QoS attribute surface. The conceptual exercise
(the agent must reason about two rules that can't simultaneously hold)
is preserved.

**Paper-text edit needed:** Table 3 row P5 reads "QoS conflict
resolution" — that stays accurate. If §5 prose ever references SSD/HDD
specifically as the conflict shape, the writing agent should adjust.

## Choice 4: Per-scenario subdir under `/ppam_2026_mcp_tests/<id>/`

Every fixture path lives under `/ppam_2026_mcp_tests/<scenario_id_lowercase>/`
(enforced by `test_fixture_paths_anchored_in_benchmark_space`).

**Why:** the federation-reset protocol (workstream #21) deletes per-
scenario subtrees between trials. Anchoring fixtures in
`/<id>/` lets reset operate on a single subtree per scenario without
risking other scenarios' state. Also keeps the reset-cap budget
(60s soft / 120s hard from paper §4¶5) workable — each subtree is
small.

## Choice 5: P3 dynamic-tier acceptance condition is OR, not AND

**Spec §6 had:** "Ensure file ... is replicated to at least 2 EU
providers. Set the appropriate QoS rule and confirm that a replication
transfer has started."
**Now:** Oracle accepts EITHER (a) a replication transfer observed in
`list_space_transfers` OR (b) the QoS rule reaches `fulfilled` status
within the deadline.

**Why:** depending on per-trial timing, the transfer may complete fast
enough that `list_space_transfers` is empty when the oracle polls (the
transfer was observed and ended between agent-action and oracle-poll).
A strict "transfer must be observed" oracle would then fail for
spurious timing reasons unrelated to the agent's behaviour. The OR
condition captures both "I saw the transfer happen" and "I see the
goal-state was reached" as evidence the agent's QoS-add caused
replication.

**Paper-text edit needed:** §4¶5 / §5.3 narrative around P3 should
acknowledge the OR oracle. Already implied by "either... OR..." phrasing
in the brief — no change strictly required.

## Choice 6: D2 prefix-match instead of regex pattern

**Spec §6 had:** "Find files matching pattern in space X" (regex
implied).
**Now:** Prefix-match against `/ppam_2026_mcp_tests/d2/datasets/`.

**Why:** Onedata's `list_files_recursively` accepts a `prefix`
parameter but no regex. Building regex into the agent prompt would
test prompt-engineering rather than tool-composition; using prefix
keeps the focus on the recursive-listing tool's actual capability.
The agent can still solve via post-filtering if it wants regex
semantics.

## Choice 7: Required-tools list is the *minimum*, allowed-tools is the
*headroom*

For each scenario:

- `required_tools` = the tools the agent **must** call to produce a
  correct answer. The harness logs all tool calls; an oracle can flag
  a scenario as "completed without invoking a required tool" as a
  cheating signal (e.g. agent guessed the answer without grounding).
- `allowed_tools_minimal` ⊇ `required_tools`. Adds discovery / utility
  tools the agent may *want* (e.g. `list_user_spaces` to confirm the
  space exists). Subset of `HEADLINE` (15) for the headline sweep;
  may include `ABLATION_EXTRAS` tools when the scenario can be solved
  more naturally with them (e.g. P5 lists `get_qos_requirement` as an
  option).

The drift test in `test_scenarios.py::test_required_tools_subset_of_minimal_allowlist`
locks down the required ⊂ allowed_minimal invariant.

## What's NOT decided here

- **Concrete oracle implementations.** Workstream #21 turns each
  `oracle_check` description into runnable code.
- **Federation-reset protocol details.** Same workstream — runner
  walks each scenario's `Fixture`, materialises files+metadata+QoS,
  triggers/waits-on `TransferFixtureHint`s for P4.
- **Per-LLM agent prompts.** Workstream #19 (multi-LLM forge_harness
  extension) decides how briefs are wrapped with system prompts per
  LLM.

## Cross-references

- `benchmark/_scenario_types.py` — dataclass definitions
- `benchmark/scenarios.py` — the 18 scenarios
- `test/unit/test_scenarios.py` — drift / sanity tests
- `papers/ppam-2026/research/22-mcp-implementation-spec.md` §6 — original
  spec example tasks (the source we deviated from where noted above)
- `papers/ppam-2026/research/27-benchmark-space-snapshot.md` — federation
  state these scenarios run against
- `papers/ppam-2026/paper.tex` Table 3 — oracle tier distribution
  authoritative reference (8 format / 8 static / 2 dynamic)
- `design/03-tool-allowlist-curation.md` — the 15-tool headline +
  7-extras ablation surface these scenarios target
