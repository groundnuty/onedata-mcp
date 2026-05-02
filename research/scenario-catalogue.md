# PPAM 2026 benchmark — scenario catalogue

**Generated 2026-05-02 from `benchmark/scenarios.py` and `benchmark/oracles/`.**
This is the canonical reference for what each of the 18 scenarios asks,
what tool surface the agent sees, what fixture the harness sets up,
and what the oracle accepts as a pass.

For the harness architecture, see `IMPLEMENTATION_NOTES.md` and
`research/empirical-mcp-server-findings.md`.

## Conventions

- **Prompt** — the literal `brief` field passed to the agent as the user
  message. Verbatim from `scenarios.py`.
- **Required tools** — `required_tools` set on the scenario; the
  agent MUST invoke each at least once. Subset of allowed.
- **Allowed-minimal surface** — `allowed_tools_minimal`; the tool
  catalogue the agent sees in this scenario (narrower than the
  16-tool HEADLINE allowlist for noise reduction).
- **Fixture** — what the harness materialises in the federation
  before the trial begins. Each per-trial federation reset wipes the
  scenario's subtree (`/<space>/<scenario_id>/`) and recreates it.
- **Pass condition** — what the oracle returns `mcp_pass=True` for.
- **Fail conditions** — observed or anticipated reasons the oracle
  returns `mcp_pass=False`. Not exhaustive; agents that fail
  unpredictably surface new modes.
- **Oracle tier**:
  - *format* — parses the answer text only.
  - *static* — parses + cross-validates federation post-state via
    REST side-channel (read-only).
  - *dynamic* — parses + polls federation state with a deadline.

The space name `ppam_2026_mcp_tests` was the original shared space.
Per-LLM spaces (per `_per_llm_spaces.py`) substitute the LLM-specific
name in fixture paths and briefs once the harness refactor lands.

---

## Discovery band — read-only metadata queries

### D1 — Spaces with provider counts

**Band**: discovery   **Tier**: format   **Difficulty**: simple

**Prompt (verbatim):**
> List every space available to you. For each space, report how many
> providers support it. Format your answer as a markdown table with
> columns 'space' and 'provider_count'.

**Required tools**: `list_user_spaces`, `list_space_providers`

**Allowed-minimal surface**: `list_user_spaces`, `list_space_providers`

**Fixture**: none (pure read scenario)

**Pass condition**: For each space the user has access to (known via
REST side-channel), the answer must contain a markdown table row with
that space's name and the correct integer provider count. Equality
check between the parsed integer and the count of providers the
federation reports for that space.

**Fail conditions**:
- Any known space omitted from the answer (`answer omits space {name}`)
- Any space's count mismatched (`expected provider_count=N, got M`)
- Integer not parseable next to the space name
- Spaces with `provider_count=0` previously mis-FAILed due to
  falsy-`or` defaulting in the oracle; fixed.

**Notes**: The benchmark token has admin scope, so `list_user_spaces`
returns ~27 spaces on the live SPICE federation, not just the
benchmark one. The agent does NOT need to call `list_space_providers`
explicitly — `list_user_spaces` returns a `providers` dict on each
space whose length is the count.

---

### D2 — Files in a directory subtree

**Band**: discovery   **Tier**: format   **Difficulty**: simple

**Prompt (verbatim):**
> Find every file in space `ppam_2026_mcp_tests` whose path begins
> with `/ppam_2026_mcp_tests/d2/datasets/`. Report the full paths as a
> bullet list, one per line.

**Required tools**: `list_files_recursively`

**Allowed-minimal surface**: `list_user_spaces`, `list_files_recursively`

**Fixture**:
- `/ppam_2026_mcp_tests/d2/datasets/alpha.txt` (1 byte)
- `/ppam_2026_mcp_tests/d2/datasets/beta.txt` (1 byte)
- `/ppam_2026_mcp_tests/d2/datasets/gamma.csv` (1 byte)
- `/ppam_2026_mcp_tests/d2/outside_prefix.txt` (distractor; doesn't match prefix)
- `/ppam_2026_mcp_tests/d2/datasets_lookalike/no.txt` (distractor; lookalike directory)

**Pass condition**: Set-equality of the paths parsed from the answer
with the expected three `/d2/datasets/` files. Path extraction uses
the `extract_paths` helper which excludes trailing-slash matches.

**Fail conditions**:
- Missing any of the three target files
- Including either distractor (oracle rejects superset)
- Including the directory header path itself (e.g. `/d2/datasets/`)
  — was an oracle bug, fixed by trailing-slash filter.
- Returning space-relative paths instead of absolute — was an MCP
  tool bug, fixed (M-6: `list_files_recursively` now normalises).

---

### D3 — Read file content + size

**Band**: discovery   **Tier**: format   **Difficulty**: simple

**Prompt (verbatim):**
> Read the file at `/ppam_2026_mcp_tests/d3/manifest.txt` and report
> (a) its size in bytes and (b) the first 50 characters of its
> content. Format the answer as: 'size=<N>; head=<chars>'.

**Required tools**: `download_file`

**Allowed-minimal surface**: `list_user_spaces`, `list_files_recursively`, `download_file`

**Fixture**: `/ppam_2026_mcp_tests/d3/manifest.txt` (57 bytes,
content: `MANIFEST v1\nbuild=46-g14b5bda7\nspace=ppam_2026_mcp_tests\n`)

**Pass condition**: Two parsed values from the answer:
- `size=N` where N == byte length of fixture content (57)
- `head=<chars>` where chars == first 50 characters of fixture content

**Fail conditions**:
- Size off-by-one or off-by-many (Claude's K=1 trial computed 56,
  miscounting the literal length of `build=46-g14b5bda7` — real
  arithmetic mistake, not parser issue)
- `\n` rendered as the literal two-character string `\n` in `head=`
  — substring match against actual newlines fails (Qwen3.6 K=1)
- Size missing or not parseable as int

---

### D4 — List space's providers as table

**Band**: discovery   **Tier**: format   **Difficulty**: simple
(but originally measured cross-call resolution discipline)

**Prompt (verbatim):**
> List every provider supporting space `ppam_2026_mcp_tests`. For
> each provider, report (providerId, providerName) as a markdown
> table.

**Required tools**: `list_space_providers`

**Allowed-minimal surface**: `list_user_spaces`, `list_space_providers`

**Fixture**: none

**Pass condition**: Set-equality on provider names extracted from the
answer with `{cloud-pl, Cloud-SK}` (the two active SPICE providers
backing this space).

**Fail conditions**:
- Either provider name absent from the answer
  (`answer missing provider names: ['cloud-pl', 'Cloud-SK']`)
- Hex providerIds in answer but no human names — was a tool-affordance
  issue (the tool was strict about `space_id` accepting only hex IDs;
  agents that passed the name as `space_id` got 403, fell back to ID
  display). Fixed by M-3: tool now resolves space_id from name-or-id.

---

### D5 — Read JSON metadata

**Band**: discovery   **Tier**: format   **Difficulty**: simple

**Prompt (verbatim):**
> Get the JSON metadata attached to `/ppam_2026_mcp_tests/d5/sample.txt`.
> Report each key=value pair on its own line in 'key: value' form.

**Required tools**: `get_file_metadata`

**Allowed-minimal surface**: `list_user_spaces`, `get_file_metadata`

**Fixture**: `/ppam_2026_mcp_tests/d5/sample.txt` with metadata
`{pipeline_stage: raw, owner: agent, created: 2026-05-01}`

**Pass condition**: All three (key, value) pairs extracted from the
answer match the fixture's `json_metadata` exactly.

**Fail conditions**:
- Any key/value mismatched
- Format not parseable as `key: value` lines

**Notes**: D5's metadata is the source of the cross-scenario
pollution that broke the *original* (space-wide) A6 brief — D5's
`pipeline_stage=raw` matched A6's space-wide query for that key.
A6 has been re-authored to subtree-scope its query. See
`research/empirical-mcp-server-findings.md` M-2.

---

### D6 — List space names

**Band**: discovery   **Tier**: format   **Difficulty**: trivial

**Prompt (verbatim):**
> List the names of all spaces available to you, one per line.

**Required tools**: `list_user_spaces`

**Allowed-minimal surface**: `list_user_spaces`

**Fixture**: none

**Pass condition**: The string `ppam_2026_mcp_tests` appears as a name
in the answer (substring match, case-insensitive).

**Fail conditions**: Answer doesn't contain the benchmark space name.

**Notes**: D6 is the simplest scenario — single tool call, single
substring check. Effectively a smoke test for whether the agent can
use `list_user_spaces` at all.

---

## Access band — multi-step write workflows

### A1 — Tag every file in a subtree

**Band**: access   **Tier**: format   **Difficulty**: medium
(multi-step write loop)

**Prompt (verbatim):**
> For every file under `/ppam_2026_mcp_tests/a1/raw/`, set custom JSON
> metadata `{"reviewed_by": "agent", "reviewed_at": "2026-05-01"}`.
> After tagging, report the count of files you tagged in the form
> 'tagged=<N>'.

**Required tools**: `list_files_recursively`, `set_file_metadata`

**Allowed-minimal surface**: `list_user_spaces`, `list_files_recursively`,
`set_file_metadata`, `get_file_metadata`

**Fixture**:
- 5 files under `/a1/raw/sample00.txt` ... `sample04.txt` (4 bytes each)
- `/a1/outside.txt` (10 bytes; distractor outside the tagging target)

**Pass condition**: The answer contains `tagged=5` (parsed via
`extract_int`).

**Fail conditions**:
- `tagged=N` with N != 5 (over- or under-counting)
- Agent tagged the distractor `outside.txt` — its inclusion in count
  produces wrong N

**Notes**: Format-tier oracle: doesn't validate the federation
post-state, only the agent's reported count. The lighter check trades
strictness for noise tolerance — A2 has a stricter static-tier
counterpart.

---

### A2 — Set metadata on every file in subtree (verified)

**Band**: access   **Tier**: static   **Difficulty**: medium

**Prompt (verbatim):**
> Set custom JSON metadata `{"reviewed": false}` on every file under
> `/ppam_2026_mcp_tests/a2/inbox/`. When done, report 'done'.

**Required tools**: `list_files_recursively`, `set_file_metadata`

**Allowed-minimal surface**: `list_user_spaces`, `list_files_recursively`,
`set_file_metadata`

**Fixture**:
- `/a2/inbox/msg00.txt` ... `msg03.txt` (4 files, 2 bytes each)

**Pass condition**: REST side-channel cross-check — for each fixture
file, `get_file_metadata(json)` must contain `reviewed: false` (strict
bool equality, not truthiness). Plus the answer should contain the
word "done".

**Fail conditions**:
- Any fixture file missing the `reviewed` key
- Any file has `reviewed: true` or a different value
- Federation read fails on any fixture file (counts as fail)
- `metadata_type="custom"` was previously rejected by the API as 404
  (looked like file-not-found). Fixed by M-4: tool now validates
  the type to `{json, rdf, xattrs}` with `custom→json` alias.

---

### A3 — Rename + tag

**Band**: access   **Tier**: static   **Difficulty**: medium

**Prompt (verbatim):**
> Rename the file `/ppam_2026_mcp_tests/a3/staging/draft.txt` to
> `/ppam_2026_mcp_tests/a3/staging/published.txt`, then set its
> custom JSON metadata to `{"status": "published"}`. Report 'done'
> when finished.

**Required tools**: `move_file`, `set_file_metadata`

**Allowed-minimal surface**: `list_user_spaces`, `move_file`,
`set_file_metadata`, `get_file_id`

**Fixture**: `/a3/staging/draft.txt` (8 bytes)

**Pass condition**: REST side-channel cross-check:
- `/a3/staging/draft.txt` does NOT exist (`get_file_id` raises enoent)
- `/a3/staging/published.txt` DOES exist
- Its json metadata equals exactly `{"status": "published"}`

**Fail conditions**:
- `draft.txt` still exists (move didn't happen or wasn't a true rename)
- `published.txt` doesn't exist
- `published.txt` exists but lacks the metadata
- Metadata is set on the old `draft.txt` instead of the new path
  (race / stale fileId)

**Notes**: `move_file` uses the CDMI `Accept: application/cdmi-object`
header pattern — see `research/empirical-onedata-25.0-findings.md`
finding #11. This is one of the few scenarios that exercises a tool
without a public REST endpoint in Onedata 25.0.

---

### A4 — Copy preserving metadata

**Band**: access   **Tier**: static   **Difficulty**: medium-hard
(multi-step: read content + read metadata + create + set metadata)

**Prompt (verbatim):**
> Copy the file `/ppam_2026_mcp_tests/a4/source/data.csv` (preserving
> its custom JSON metadata) to `/ppam_2026_mcp_tests/a4/archive/data.csv`.
> The original file must remain in place. Report 'done' when finished.

**Required tools**: `download_file`, `create_file`, `get_file_metadata`,
`set_file_metadata`

**Allowed-minimal surface**: `list_user_spaces`, `download_file`,
`create_file`, `get_file_metadata`, `set_file_metadata`

**Fixture**: `/a4/source/data.csv` (19 bytes,
metadata `{format: csv, rows: '2'}`)

**Pass condition**: REST side-channel cross-check:
- Source file still exists with original content + metadata intact
- Archive copy exists with byte-identical content
- Archive copy's metadata equals exactly `{format: csv, rows: '2'}`

**Fail conditions**:
- Source missing (agent did move not copy)
- Source content/metadata altered
- Archive missing
- Archive content differs from source
- Archive metadata missing or different

---

### A5 — Create file + attach EU QoS rule

**Band**: access   **Tier**: static   **Difficulty**: hard
(requires understanding the QoS expression DSL + federation reality)

**Prompt (verbatim):**
> Create a new file at `/ppam_2026_mcp_tests/a5/important/checkpoint.bin`
> with content `placeholder` (utf-8). Then attach a QoS rule
> requiring it be replicated on at least 2 EU providers. Report
> 'done' when finished.

**Required tools**: `create_file`, `add_file_qos_requirement`

**Allowed-minimal surface**: `list_user_spaces`, `create_file`,
`add_file_qos_requirement`, `get_file_qos_summary`

**Fixture**: none (the agent creates the file)

**Pass condition**: REST side-channel cross-check:
- The created file exists at the target path
- Its size equals `len('placeholder')` = 11 bytes
- `get_file_qos_summary` returns at least one rule whose expression
  matches an EU constraint (regex over the rule expression: must
  contain one of `country=PL`, `country=SK`, `country=AT`, `geo=EU`
  or equivalent admin-attributed forms — see EU_TOKENS)
- AND that rule's `replicas_num >= 2`

**Fail conditions**:
- File not created
- Wrong content / size
- No QoS rule attached
- QoS rule expression uses a fictional operand (e.g. `cloud=EU`,
  `region=Europe`) that matches zero storages — was a docstring-
  guidance issue, addressed in M-8.
- Rule has `replicas_num=1`

**Notes**: SPICE has NO admin-set storage attributes (no `country=`,
`type=`, `geo=` tags pre-set on storages). The only operands that
work are `providerId=...` and `storageId=...`. An agent passing
`country=EU` gets a rule that materialises but matches zero
storages → permanent `impossible` status. The oracle accepts
country-based rules because the brief's wording suggests them, even
though they don't actually replicate on this federation. This is a
benchmark-design tension worth noting in the paper.

---

### A6 — Find files by metadata predicate

**Band**: access   **Tier**: format   **Difficulty**: medium

**Prompt (verbatim):**
> Find every file under `/ppam_2026_mcp_tests/a6/` whose JSON
> metadata key `pipeline_stage` equals `raw`. Report the full paths
> as a bullet list, one per line.

**Required tools**: `query_by_metadata`

**Allowed-minimal surface**: `list_user_spaces`, `query_by_metadata`

**Fixture**:
- `/a6/batch01/f1.txt` (metadata: `pipeline_stage=raw`)
- `/a6/batch01/f2.txt` (metadata: `pipeline_stage=raw`)
- `/a6/batch02/f3.txt` (metadata: `pipeline_stage=anonymised`; distractor)
- `/a6/no_meta.txt` (no metadata; distractor)

**Pass condition**: Set-equality of paths parsed from the answer
with `{f1.txt, f2.txt}` under `/a6/batch01/`.

**Fail conditions**:
- Missing either f1 or f2
- Including f3 (different stage; predicate violation)
- Including no_meta.txt
- Including paths from OTHER scenarios (was the cross-scenario
  pollution bug — fixed by re-authoring brief to subtree scope and
  by M-1's path-format normalisation in `query_by_metadata`)

**Notes**: Originally this scenario was space-wide ("every file in
space..."), which broke because D5's fixture also tags
`pipeline_stage=raw`. Subtree-scoped query avoids the collision.

---

## Placement band — distribution / QoS reasoning

### P1 — Per-provider distribution + EU replication assessment

**Band**: placement   **Tier**: static   **Difficulty**: hard
(multi-call: distribution → name resolution)

**Prompt (verbatim):**
> Report the per-provider distribution of `/ppam_2026_mcp_tests/p1/model_v1.pt`.
> For each supporting provider list (providerName, percentage of
> blocks held). Identify whether the file is fully replicated to any
> EU provider and name those providers.

**Required tools**: `get_file_distribution`, `list_space_providers`

**Allowed-minimal surface**: `list_user_spaces`, `get_file_distribution`,
`list_space_providers`

**Fixture**: `/p1/model_v1.pt` (4096 bytes, two QoS rules pinning it
to both providers — fully replicated steady state)

**Pass condition**: Both provider names (`cloud-pl`, `Cloud-SK`) must
appear in the answer; the agent must identify them as fully
replicated EU providers. REST side-channel verifies the file's
distribution actually shows 100% block coverage on both providers.

**Fail conditions**:
- Either provider name missing (e.g. only providerIds in answer)
- Wrong replication percentage reported
- Mis-identifying which providers are EU

**Notes**: Pre-fix, `get_file_distribution` returned only providerIds.
Smaller models often dropped name resolution and returned the hex
IDs in the answer → fail. Fixed by M-5: distribution response now
includes `providerName` per provider, no chained call required.

---

### P2 — Find QoS rules in `impossible` state

**Band**: placement   **Tier**: static   **Difficulty**: hard
(per-file QoS introspection across a subtree)

**Prompt (verbatim):**
> Find files under `/ppam_2026_mcp_tests/p2/critical/` whose QoS
> rule is in the `impossible` state (i.e. the federation cannot
> satisfy the requirement). For each such file, report
> (path, qosExpression, status).

**Required tools**: `list_files_recursively`, `get_file_qos_summary`

**Allowed-minimal surface**: `list_user_spaces`, `list_files_recursively`,
`get_file_qos_summary`

**Fixture**:
- `/p2/critical/violator.bin` (1024 bytes, QoS:
  `providerId=00000000nonexistent000000000000ch0000` — fictional ID,
  always `impossible`)
- `/p2/critical/ok.bin` (1024 bytes, QoS targets cloud-pl — should
  be `fulfilled`)

**Pass condition**: Cross-validate via REST side-channel — answer
must name `violator.bin` and report its expression + impossible
status; must NOT name `ok.bin`.

**Fail conditions**:
- Reporting `ok.bin` as impossible (it's the distractor; should be
  in `fulfilled` or `pending`)
- Missing `violator.bin`
- Reporting wrong expression for violator
- Status field missing or wrong

**Notes**: Onedata 25.0's QoS rule status doesn't always settle to
`fulfilled` even when data IS replicated (per finding #16). The
benchmark accepts `impossible` as a stable, observable signal — it
fires when no storage in the federation matches the expression.

---

### P3 — Add QoS rule + verify it materialised

**Band**: placement   **Tier**: dynamic   **Difficulty**: hard
(rule-add + polling within deadline)

**Prompt (verbatim):**
> Ensure `/ppam_2026_mcp_tests/p3/result.bin` is replicated on at
> least 2 EU providers. Add the appropriate QoS requirement, then
> verify by polling that EITHER (a) a replication transfer for this
> file appears in the space transfer log, OR (b) the QoS rule
> reaches `fulfilled` status. Wait up to 60 seconds. Report which
> condition was observed and the corresponding evidence (transferId
> or qosRequirementId).

**Required tools**: `add_file_qos_requirement`, `get_file_qos_summary`,
`list_space_transfers`

**Allowed-minimal surface**: `list_user_spaces`, `add_file_qos_requirement`,
`get_file_qos_summary`, `list_space_transfers`, `get_transfer`

**Fixture**: `/p3/result.bin` (8192 bytes, initial QoS pinning it to
cloud-pl only)

**Pass condition** (two-axis):
- `mcp_pass`: agent (a) added a QoS rule whose expression contains an
  EU operand and `replicas_num >= 2`, (b) called either
  `list_space_transfers` or `get_file_qos_summary` to poll, (c) the
  answer contains the words `transfer` or `fulfilled`.
- `federation_pass`: within 60s of the agent's rule add, the REST
  side-channel observed EITHER a transfer matching this file in the
  transfer log OR a rule for this file with status `fulfilled`.

**Fail conditions** (mcp axis):
- No `add_file_qos_requirement` with EU + replicas≥2
- No follow-up poll (neither `list_space_transfers` nor
  `get_file_qos_summary`)
- Final answer doesn't contain `transfer` or `fulfilled`

**Fail conditions** (federation axis):
- 60s elapsed; no transfer for this file appeared AND no rule
  reached `fulfilled` status

**Notes**: This is the only scenario where `mcp_pass` and
`federation_pass` can legitimately diverge — agent did the right
thing but the federation couldn't satisfy in time (eventual-
consistency reality on the live SPICE). For the paper's pass^k,
`mcp_pass` is the axis aggregated. `federation_pass=False` becomes
a §7 Threats narrative: "Onedata-side divergence rate".

---

### P4 — Most-recent transfer for a file

**Band**: placement   **Tier**: dynamic   **Difficulty**: medium
(requires fixture pre-stage of a real transfer)

**Prompt (verbatim):**
> Identify the most-recent transfer in space `ppam_2026_mcp_tests`
> involving the file `/ppam_2026_mcp_tests/p4/relocated.bin`. Report
> only the transferId.

**Required tools**: `list_space_transfers`, `get_transfer`

**Allowed-minimal surface**: `list_user_spaces`, `list_space_transfers`,
`get_transfer`

**Fixture**:
- `/p4/relocated.bin` (768 bytes)
- Pre-staged: a real `migration` transfer of this file to Cloud-SK,
  triggered by the harness via `POST /transfers` directly (not via
  QoS rule indirection per finding #17). The captured transferId
  is held in `RunContext.captured_transfer_id`.

**Pass condition**: The answer must contain the transferId captured
during fixture pre-staging (string substring match).

**Fail conditions**:
- TransferId missing from answer
- Different transferId (agent picked the wrong one if multiple
  transfers exist for the file)
- `list_space_transfers` rejected with 403 because agent passed the
  space name instead of ID — was the same M-3 issue as
  `list_space_providers`. Fixed: tool now resolves name-or-id.

---

### P5 — Multi-rule conflict detection

**Band**: placement   **Tier**: static   **Difficulty**: hard
(needs both rule listing and conflict reasoning)

**Prompt (verbatim):**
> Examine `/ppam_2026_mcp_tests/p5/conflicted.bin`. It carries
> multiple QoS requirements that may conflict. Report all rule
> expressions currently attached and identify whether any are
> mutually exclusive (e.g. country=PL alongside country=SK with
> replicas_num = 1 each). Format each rule as 'expr=<expression>;
> replicas=<N>; status=<status>'.

**Required tools**: `get_file_qos_summary`

**Allowed-minimal surface**: `list_user_spaces`, `get_file_qos_summary`,
`get_qos_requirement`

**Fixture**: `/p5/conflicted.bin` (8 bytes) with two QoS rules, each
with `replicas_num=1`, one pinning to cloud-pl, the other to Cloud-SK
(mutually exclusive at replicas=1).

**Pass condition**: REST side-channel cross-check — agent's answer
must:
- Report both rule expressions (cloud-pl and Cloud-SK pinning)
- Note the mutual-exclusion / conflict observation in prose

**Fail conditions**:
- Only one expression reported
- No conflict / mutual-exclusion phrasing
- Wrong expressions (made-up rules)

**Notes**: Pre-fix, `get_file_qos_summary` returned only
`{qosId: status}`, no expression detail. Agents had to chain
`get_qos_requirement` per rule to see expressions. Fixed by M-7:
summary now inlines `expression` and `replicas_num` per rule.

---

### P6 — Find single-replica files

**Band**: placement   **Tier**: static   **Difficulty**: hard
(needs the per-rule replicas_num field, which was missing pre-fix)

**Prompt (verbatim):**
> Find every file under `/ppam_2026_mcp_tests/p6/single-copy/` whose
> effective QoS requires only 1 replica. Report the full paths as a
> bullet list.

**Required tools**: `list_files_recursively`, `get_file_qos_summary`,
`get_qos_requirement`

**Allowed-minimal surface**: `list_user_spaces`, `list_files_recursively`,
`get_file_qos_summary`, `get_qos_requirement`

**Fixture**:
- `/p6/single-copy/lone1.bin` (4 bytes, QoS: pin to cloud-pl,
  replicas_num=1)
- `/p6/single-copy/lone2.bin` (4 bytes, QoS: pin to Cloud-SK,
  replicas_num=1)
- `/p6/single-copy/redundant.bin` (9 bytes, QoS:
  `providerId=cloud-pl | providerId=Cloud-SK`, replicas_num=2;
  distractor)

**Pass condition**: Set-equality of paths in the answer with
`{lone1.bin, lone2.bin}`. `redundant.bin` is the distractor (its
replicas_num=2 disqualifies it).

**Fail conditions**:
- Reporting `redundant.bin` (over-includes distractor)
- Missing either lone1 or lone2
- Reporting only `redundant.bin` (most common pre-fix failure mode —
  agents fell back on filename heuristics when they couldn't extract
  replicas_num from the summary response)

**Notes**: Pre-fix, ALL three K=1 LLMs converged on the same wrong
answer (only `redundant.bin`) because `get_file_qos_summary` lacked
`replicas_num` AND `get_qos_requirement` was excluded from the
HEADLINE allowlist. Two fixes applied: (a) HEADLINE expanded to 16
tools to include `get_qos_requirement`; (b) M-7 — summary now
inlines replicas_num so the chain isn't needed at all. After both,
all 3 LLMs flipped to PASS.

---

## Cross-band invariants

- **Per-trial federation reset** is subtree-scoped: only
  `/<space>/<scenario_id>/` is wiped between trials. Other scenarios'
  fixtures persist from prior trials. Scenarios that issue space-wide
  queries (originally A6) needed re-authoring to subtree-scope them
  to avoid pollution.
- **Distractors** in fixtures (non-target files within a queried
  subtree) test set-membership discipline. Most agents that fail by
  including distractors do so because they don't fully read the
  brief's predicate.
- **All briefs use the human-readable space name**
  (`ppam_2026_mcp_tests`). Tools that take `space_id` were initially
  strict to hex IDs and produced 403s when given names; M-3 made them
  polymorphic.
- **Path format**: every brief uses absolute paths
  (`/ppam_2026_mcp_tests/...`). Tool responses must echo absolute
  form for path-set comparison oracles to work. Several tools
  initially returned space-relative or root-relative paths; M-1 and
  M-6 normalised them.
- **The benchmark token has admin scope** — `list_user_spaces`
  returns ~27 spaces, not just the benchmark one. D1 / D6 oracles
  account for this by checking the benchmark space appears (D6) or
  validating against ground truth per visible space (D1).

## Cross-references

- Tool design issues surfaced by the K=1 sweep:
  `research/empirical-mcp-server-findings.md` (M-1 through M-9)
- Onedata-side empirical observations:
  `research/empirical-onedata-25.0-findings.md` (19+ findings)
- HEADLINE / ABLATION_EXTRAS allowlist:
  `benchmark/tool_allowlist.py`
- Scenario source:
  `benchmark/scenarios.py`
- Oracle source:
  `benchmark/oracles/{discovery, access, placement}.py`
