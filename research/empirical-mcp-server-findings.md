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
- `llm-output-stability-findings.md` — LLM-side output-emission
  quirks (model behaviour, not server design — e.g., Qwen empty
  final content after long tool chains).
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

## M-4. `set_file_metadata` accepts arbitrary `metadata_type` strings

**Surface:** `onedata_mcp/modules/files.py::mcp_set_file_metadata`

**Behaviour:** the parameter `metadata_type` is typed as `str` and
passed verbatim into the URL path: `PUT /data/<id>/metadata/<type>`.
Onedata only knows three metadata types (`json`, `rdf`, `xattrs`).
Any other value yields HTTP 404 with no helpful diagnostic — the
URL with the unknown type just doesn't exist.

**Why this matters:** the briefs use the word "custom" colloquially
("custom JSON metadata"). LLMs faithfully echo this and pass
`metadata_type="custom"`. Tool fires 404. Agent reads error as
"file not found" and gives up.

**Evidence:** Qwen-Coder A2 + A3 in run `20260502T112609`. Both
attempted `metadata_type="custom"` → 404 from
`PUT /data/<id>/metadata/custom`.

**Fix:** validate at tool-call time. Accept only `{json, rdf, xattrs}`.
Map alias `custom → json` (the natural reading). Anything else raises
`ValueError` immediately with a clear list of valid values.

---

## M-5. `get_file_distribution` returns provider IDs without names

**Surface:** `onedata_mcp/modules/files.py::mcp_get_file_distribution`

**Behaviour:** Oneprovider's `/data/<id>/distribution` returns
`distributionPerProvider` keyed by hex providerId only — no name. To
report "this file is on cloud-pl and Cloud-SK", an agent has to chain
`list_space_providers` and merge the lookups manually.

**Why this matters:** smaller / less capable models don't reliably
chain calls. Qwen-Coder P1 made the distribution call, saw
`736092c5...` and `27c0f483...`, and just included those hex IDs in
its answer. Oracle expected human names; FAIL.

**Evidence:** Qwen-Coder P1 in run `20260502T112609`. Final answer
listed providerIDs verbatim; no second call to resolve names.

**Fix:** the wrapper enriches the response inline by fetching
provider catalogues for each space the user has access to and
attaching `providerName` next to each `providerId` in
`distributionPerProvider`. Best-effort: if the lookup fails, the
distribution is still returned with all block-level info; only the
name field is missing.

---

## M-6. `list_files_recursively` returns paths relative to listed parent

**Surface:** `onedata_mcp/modules/files.py::mcp_list_files_recursively`

**Behaviour:** Onedata's recursive listing returns paths relative
to the listed root (`msg00.txt` when listing `/space/a2/inbox/`).
Same family as M-1 (`query_by_metadata`) but for a different tool;
inconsistent with the rest of the MCP surface, which uses absolute
paths everywhere else.

**Why this matters:** the agent listing a subtree expects its prompt's
absolute paths back. Without normalization, agents either drop the
context or hallucinate the prefix. Two failures observed:
- A2: Qwen-Coder used the relative paths in its follow-up
  `set_file_metadata` calls — calls succeeded against the wrong
  resolved fileId paths.
- The fix-cascade prevents quiet downstream cascading errors.

**Evidence:** A2 trace in run `20260502T112609` showing
`{"path": "msg00.txt"}` from `list_files_recursively`. Compare to the
already-fixed M-1 sibling.

**Fix:** when caller passed a path argument (starting with `/`), the
wrapper rewrites each entry's `path` to `<caller-path>/<relative>`.
When caller passed a fileId, paths are passed through unchanged
(caller chose the id form). Mirrors M-1's path-prefix-via-root
strategy exactly.

---

## M-7. `get_file_qos_summary` returns flat `{qosId: status}` only

**Surface:** `onedata_mcp/modules/qos.py::mcp_get_file_qos_summary`

**Behaviour:** Oneprovider returns `{requirements: {qosId: status}}`
— a status mapping with no rule-detail (replicas_num, expression).
Common questions like "which files require only 1 replica?" need
follow-up `get_qos_requirement` per rule.

**Why this matters:** P6 ("find files whose effective QoS requires
only 1 replica") needed two-call discipline. We promoted
`get_qos_requirement` to the HEADLINE allowlist (M-3 of scenario set,
documented in `tool_allowlist.py`) so agents *could* make the chain,
but agents that didn't recognise the chain pattern still failed.

**Why fix it:** the simpler-surface principle. Rule status is the
question 95% of askers want; rule detail is the other 5%. Inline
both so neither requires multiple calls.

**Evidence:** Qwen-Coder P6 in run `20260502T112609`. Got summary,
saw status only, guessed wrong about replica counts.

**Fix:** the wrapper fetches each rule's detail in parallel
(asyncio.gather) and merges `expression`, `replicas_num`,
`fulfilled` into each entry. Response shape becomes:

    {
      "status": "fulfilled",
      "requirements": {
        "<qosId>": {
          "status": "fulfilled",
          "expression": "providerId=...",
          "replicas_num": 2,
          "fulfilled": true
        }
      },
      "requirements_flat": { "<qosId>": "fulfilled" }
    }

Original flat shape preserved as `requirements_flat` for any caller
that depends on it.

---

## M-8. `add_file_qos_requirement` operands are federation-specific

**Surface:** `onedata_mcp/modules/qos.py::mcp_add_file_qos_requirement`

**Behaviour:** Onedata accepts arbitrary `key=value` operands in QoS
expressions. Two operand families exist:

1. **Implicit (always work)**: `providerId=<id>`, `storageId=<id>`,
   `anyStorage`.
2. **Admin-attributed (work only if federation admins tagged
   storages)**: e.g. `country=PL`, `type=ssd`, `geo=EU`. Whether
   these work is per-federation policy.

LLMs trained on generic Onedata docs assume the admin-attributed
forms always work and confidently emit expressions like `cloud=EU`,
`region=Europe`, `zone=public`. None of these are real attribute
names; SPICE in particular has no admin-set tags at all.

**Why this matters:** A QoS rule that no storage matches enters
`impossible` status — never fulfils. The agent considers the call
"successful" (rule was created), but downstream verification fails.

**Evidence:** Qwen3.6 A5 in run `20260502T112609`. Used
`expression="cloud=EU"`, got a `qosRequirementId` back, declared
done. Oracle checks the ACTUAL rule expression — it's not an
EU-replicas≥2 rule by any sensible reading.

**Fix:** strengthen the docstring with explicit guidance:
- name the always-safe implicit operands (providerId, storageId,
  anyStorage)
- flag the admin-attributed family as federation-specific
- explicitly call out common fictional operand names to AVOID
  (`cloud=...`, `region=...`, `zone=...`)
- point at the empirical-findings doc for the SPICE specifics

This is purely a docstring tightening — no behavioural change. The
hope is that better in-tool documentation reduces the fictional-
operand class of error.

---

## M-9 (lower urgency). Inactive Forge models still appear in
        `/v1/models`

Out of scope for the MCP server itself but documented in the
Cyfronet-facing report at
`artefacts/20260502T112609/REPORT_cyfronet.md`. Listed here as a
cross-reference: the benchmark surfaced 4 distinct Forge-API gaps
(tags-not-exposed, max_tokens-caps-undiscoverable, inactive-models-
still-in-catalogue, undifferentiated-400-errors) which are
recommended as a Cyfronet operations-side write-up.

---

## Pattern: smaller-model accommodation

M-4 through M-8 share a design heuristic that's worth naming:
**bake the chained-call result into the primary response so weak-
model agents don't have to chain**.

The OpenAI tool-use protocol forces the agent to run the tool→
read result→call next tool loop. Every "should be one call" pattern
that requires N calls in practice multiplies the failure rate by
roughly N: each chain step is an opportunity to give up, hallucinate,
or mis-specify.

Bigger models (Claude Sonnet 4.5 in our panel) execute the chains
reliably and don't need the help. Smaller / open-source models
(Qwen3-Coder-30B, GLM-4.7-Flash, QwQ-32B, Qwen3.6-35B) have a
markedly higher success rate when the response payload pre-joins the
data they're going to need.

This isn't a paper observation; it's a substrate observation. The
PPAM 2026 paper measures *which* models pass; the tool-design choices
in this MCP server affect *how* the panel scores. The fixes M-4
through M-8 are a deliberate choice to optimise the tool surface
toward weaker-model usability — paper §7 should note this.

---

## M-10. `download_file` returns size out-of-band

**Surface:** `onedata_mcp/modules/files.py::mcp_download_file`

**Behaviour:** the wrapper returned the raw file content as bytes /
string. Agents asked for "byte count of file X" then called
`len(content)` on the returned string — which counts characters after
UTF-8 decode, not bytes. Multi-byte text gives a wrong answer; even
ASCII content gives a wrong answer when the content has trailing
whitespace the model truncates.

**Why this matters:** D3 ("get exact byte size of /d3/file.txt") is
specifically a size-arithmetic scenario. Three of four panel LLMs
(Sonnet 4.5, V3, GLM-4.7) returned wrong byte counts in run
`20260502T145805` despite reading the right file. Only Qwen3.6
got it right (independent `get_file_attributes` call to read `size`).
Fleet-wide pass rate 1/4. The tool returned the data; the data shape
forced the failure.

**Evidence:** D3 across panel in run `20260502T145805`. Diagnostic on
DeepSeek-V3's K=1 failures (13/18) showed D3 as the
arithmetic-on-content-string failure mode that was preventing fleet-
wide convergence.

**Root cause:** `download_file` returned a single value (bytes /
string). Counting bytes from the string requires the agent to remember
to encode-then-len, and OSS panel models don't reliably do this. The
authoritative size is server-side; surfacing it inline removes the
arithmetic-on-content-string trap.

**Fix:** the wrapper now returns
`{"content": str, "size_bytes": int, "content_type": str | None}`.
`size_bytes` is the authoritative byte count (`len(raw_bytes)`).
`content_type` is the upstream HTTP `Content-Type` header verbatim if
known, else `None`. Backwards-compat: agents that destructure
`.content` keep working; new agents read `.size_bytes` directly.
Implementation: `onedata_mcp/api/files.py::download_file_with_meta`
(new) returns the tuple form; the legacy `download_file` thin-wraps
it for `grep_file_content` callers. `onedata_mcp/modules/files.py:240`
surfaces the dict envelope.

**Test:** `test/unit/api/test_files.py::test_download_file_with_meta_*`
(2 tests) + `test/unit/modules/test_files_wrapper.py::test_download_file_wrapper_*`
(2 tests, including a UTF-8 multi-byte fixture asserting
`size_bytes != len(content)`).

**Status:** applied — see commit at the bottom of this file.

---

## M-11. `create_parents` default of False + no explicit `create_directory`

**Surface:** `onedata_mcp/modules/files.py::mcp_create_file` plus the
absence of a directory-creation tool.

**Behaviour:** A4 (cross-directory file move + verify) requires the
agent to set up a target directory before placing a file inside it.
With no explicit `create_directory` tool, V3 and GLM both invented:

    create_file(path="archive", content="", create_parents=True)

This silently creates a REGULAR FILE at `archive`. Every subsequent
`archive/<x>` op then fails with `enotdir`, and the agent — having
gotten a `fileId` back — believes the directory exists. Multi-step
cascade: file-move "fails" not because of the move but because the
target "directory" is actually a file.

**Why this matters:** V3 and GLM both fell into this on A4 in run
`20260502T145805` — fleet-wide 2/4 affected. The trap is structural:
without `create_directory`, the agent has no clean way to express
"I want a directory here." Setting `create_parents=True` and writing
empty content is a reasonable inference from the available surface
that produces the wrong result.

**Evidence:** A4 traces in run `20260502T145805` — V3 and GLM both
attempted `create_file(path="archive", content="", create_parents=True)`
followed by file-create ops under `archive/` that 400'd with
`enotdir`. Sonnet+Qwen3.6 used different patterns and avoided the
trap.

**Root cause:** the wrapper exposed `create_file` with
`create_parents=False` as the default — agents had to remember to
opt in to the parent-creation behavior. Combined with no
`create_directory` tool, agents who DID enable `create_parents`
ended up using it on the wrong shape.

**Fix:** three coordinated changes:

1. **Default flip:** `create_parents` now defaults to `True`. Missing
   intermediate directories along the path are auto-created without
   the agent needing to know about the flag.

2. **New tool `create_directory`** added at
   `onedata_mcp/modules/files.py:343`. Returns `{fileId, path}`. Maps
   to `PUT /data/{space_id}/path/{rel}?type=DIR&create_parents=...`
   — see `oneprovider-swagger:paths/data/id/path.yaml` (operationId
   `create_file_at_path`, the canonical type-DIR primitive).
   Implementation at `onedata_mcp/api/files.py::create_directory`.
   Added to ABLATION_EXTRAS (not HEADLINE — the headline 16-tool
   contract is preserved; the defensive error in `create_file` is
   what catches the trap on the headline surface).

3. **Defensive error in `create_file`:** when content is empty AND
   the basename has no recognizable file extension, raise ValueError
   pointing at `create_directory`. This is belt-and-braces — the
   default-True flip + the new tool together solve the trap, but the
   defensive error catches any agent that doesn't read the new
   docstring carefully. Heuristic at
   `onedata_mcp/api/files.py::_looks_like_directory_intent`. Hidden
   files (`.gitignore`) are explicitly allowed empty.

**Test:** `test/unit/api/test_files.py::test_create_directory_*` (3) +
`test/unit/api/test_files.py::test_looks_like_directory_intent_*` (2) +
`test/unit/modules/test_files_wrapper.py::test_create_file_*` (3) +
`test_create_directory_*` (2). The `test_create_directory_then_create_file_under_it`
test exercises the full A4 shape (mkdir → file-under-mkdir).

**Status:** applied — see commit at the bottom of this file.

---

## M-12. `list_files_recursively` `prefix` ambiguity (relative-only)

**Surface:** `onedata_mcp/modules/files.py::mcp_list_files_recursively`

**Behaviour:** the `prefix` parameter on `list_files_recursively`
matches Onedata's *relative* path scheme — when listing
`/space/d2/datasets/`, valid prefixes are `alpha` (matches
`alpha.txt`), not `/space/d2/datasets/alpha`. The original docstring
read "Only files with paths starting with this value are listed,"
which doesn't say WHICH path scheme. V3 (and likely future agents)
reasonably inferred the absolute form, passed
`prefix="/space/d2/datasets/alpha"`, and got back `{"files": []}` —
silent zero-match.

**Why this matters:** D2 ("find files matching prefix `/d2/datasets/alpha`")
intermittently failed for V3 in run `20260502T145805` because the
absolute prefix returned no matches and the agent had no signal that
the prefix shape was wrong (vs the brief expecting different
matches). Silent-zero-match is a worse failure mode than a 400 — no
error to backtrack from.

**Evidence:** V3 D2 trace in run `20260502T145805`. Tool call has
`prefix=/d2/datasets/alpha` (absolute), tool response is
`{files: []}`. Compare to runs where V3 used a relative prefix and
got the correct match set.

**Root cause:** Onedata's REST `prefix` is server-side relative —
that's a known Onedata-25.0 quirk (sibling to the M-1 / M-6
relative-path family). The wrapper didn't translate; the docstring
didn't disambiguate.

**Fix:** docstring tightening at
`onedata_mcp/modules/files.py:170`. New text:

> Filter by relative path prefix under parent_id_or_path. Onedata's
> recursive listing returns RELATIVE paths (e.g. 'alpha.txt' under
> '/space/d2/datasets/'), so the prefix MUST be relative too — pass
> 'alpha' to match 'alpha.txt'. Absolute paths are NOT supported and
> silently return zero matches.

Server-side stripping (the optional improvement) is not implemented —
the wrapper-side parse ambiguity (the path under-which the prefix
should match could be either the listed parent or its space-relative
form) makes that more invasive than the docstring-only fix.

**Test:** `test/unit/modules/test_files_wrapper.py::test_list_files_recursively_prefix_param_doc_relative_only`
asserts the schema-level description includes both "relative" and
"absolute". Plus a functional test that a relative prefix
filters correctly.

**Status:** applied — see commit at the bottom of this file.

---

---

## M-13. HTTP transport accepts arbitrary Host/Origin headers (DNS-rebinding vulnerability)

**Surface:** `onedata-mcp` HTTP transport (added 2026-05-03 to support
the conformance + Inspector tooling). FastMCP-default behaviour;
exposed by running the modelcontextprotocol/conformance suite v0.1.16
`dns-rebinding-protection` scenario (suite reference:
[MCP-DNS-Rebinding-Protection spec](https://modelcontextprotocol.io/specification/2025-11-25/basic/security_best_practices#local-mcp-server-compromise)).

**Behaviour:** when launched with `MCP_TRANSPORT=http MCP_PORT=3037`,
the server accepts requests with `Host: evil.example.com` and `Origin:
http://evil.example.com` headers and responds with HTTP 200, including
on `tools/call` requests. The MCP-2025-11-25 security spec mandates
that local servers MUST reject non-localhost Host/Origin headers to
prevent DNS-rebinding attacks (where a malicious website resolves a
domain to 127.0.0.1, then issues fetch() requests to it).

Specifically:

```
- localhost-host-rebinding-rejected: FAILURE
  Expected HTTP 4xx for invalid Host/Origin headers, got 200
  hostHeader: evil.example.com
  originHeader: http://evil.example.com
- localhost-host-valid-accepted: SUCCESS
  Server correctly accepts valid 127.0.0.1 origins
```

The valid-localhost case passes; only the malicious-Host case fails.

**Why this matters:** the stdio-transport mode (used by the benchmark
+ claude-agent-sdk) is unaffected — that path doesn't accept HTTP
headers at all. But operators running onedata-mcp via HTTP for
Inspector/conformance/external-client integration are vulnerable: a
user with a browser tab open to a malicious site can have their local
MCP server enumerated and arbitrarily invoked from JavaScript.

The risk profile depends on how operators deploy:

- Pure stdio (benchmark-style, claude-agent-sdk subprocess): NOT affected
- HTTP on `127.0.0.1` only, no public port: low risk (still
  vulnerable to DNS-rebinding from the user's own browser)
- HTTP on `0.0.0.0` or public IP: HIGH risk

**Fix (proposed, not yet applied 2026-05-03):**

FastMCP 3.x's HTTP transport accepts a `host_validation` middleware
or equivalent. The right fix is to register middleware that:

1. Validates `Host` header is exactly `127.0.0.1:<port>`,
   `localhost:<port>`, or empty (server-relative).
2. Validates `Origin` header is `http://127.0.0.1:<port>` or
   `http://localhost:<port>` when present.
3. Rejects with HTTP 403 otherwise.

Pseudocode for the middleware (concrete impl TBD when applied):

```python
ALLOWED_HOSTS = {f"127.0.0.1:{port}", f"localhost:{port}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}

async def reject_dns_rebinding(request, call_next):
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if host not in ALLOWED_HOSTS:
        return Response(status_code=403, content="Invalid Host header")
    if origin and origin not in ALLOWED_ORIGINS:
        return Response(status_code=403, content="Invalid Origin header")
    return await call_next(request)
```

**Status:** documented, fix deferred. Tracked via:

- `conformance-baseline.yaml` — `dns-rebinding-protection` listed in
  the baseline's `server:` array as expected-fail (so `make
  conformance` exits 0 with the issue documented).
- This M-13 entry — when fix lands, remove from baseline.

**Test after fix:** `make conformance` — `dns-rebinding-protection`
moves from FAILURE to SUCCESS in the suite output. Remove from
`conformance-baseline.yaml`.

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
