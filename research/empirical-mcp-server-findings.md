# Empirical findings — onedata-mcp server design

**Started 2026-05-02; extended 2026-11-23.** This file documents
MCP-server design issues surfaced by the PPAM 2026 multi-LLM benchmark
— issues that 100/100 unit tests + the read-only `mcp_smoke.py` had
not exposed because those tests don't exercise the *cross-tool
reasoning* an LLM agent performs against live federation state.

Since 2026-11 a second source adds to it: the tool-maturity protocol
(`design/10-tool-maturity.md`). Its unit-test gate, its live checks
and validation runs on a local Onedata 25.0 federation, and its
external reviews found the issues from M-14 on. That part also records
the failures deliberately left unfixed because the model, not the
tool, was at fault.

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

## Aim: tools usable by less capable models (smaller-model accommodation)

**The project's aim is that its tools be usable by less capable
models, not only by frontier ones.** A tool that a strong model uses
well and a weak one misuses is not finished. The panel's probes exist
to find where that fails, and a model-capability failure is still a
prompt to ask whether a modest tool change would help: each "Not
changed" entry from M-29 on names its **Accommodation candidate**.

The rules so far, each learned from findings:

1. **Pre-join chained data into the primary response** (M-4 to M-8).
   Each extra call a weak model must chain is a chance to give up,
   guess or mis-specify. The PPAM panel's smaller models passed far
   more often when the payload already held what they needed.
2. **Put the guidance in the result the model has just read** (M-26,
   M-27). The descriptions of `schedule_file_replication` and
   `list_children` already said what F2 and F3 failed to do ("judge
   the copy by those blocks, not by the transfer's status"; "When
   isLast is false, call again with token …"). The failing models
   were given those descriptions and acted on the answer in front of
   them: a transfer record reading "Completed", a page with no
   subdirectories on it. The re-runs will show whether a note in the
   result is enough. Accurate errors already steer: 10 of 11 provider
   names passed as ids recovered after "Pass its id …" (M-21), and no
   CDn answer reported a directory once the error said a file was
   there (M-16).
3. **Never over-claim in a hint** (M-19, M-22, M-28). A hint reads as
   the tool's diagnosis: a false "does not exist" ends a search, a
   false "calling again will not help" ends the retry that would have
   worked. Name a cause only when the response establishes it (a
   structured error id or errno on a 4xx); otherwise list the
   possible causes and what to check, and keep Onedata's message
   after the hint.
4. **Say "empty" explicitly.** A silent empty answer reads as a wrong
   query (M-12) or as something hidden (M-30). When nothing matches,
   say so in words, and say what was searched.

The rules are aimed at the probes. The control, Claude Sonnet at
medium effort, passed every gate-4 trial (design/11–15).

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

## M-13. HTTP transport accepts arbitrary Host/Origin headers (DNS-rebinding vulnerability) — RESOLVED 2026-05-03

> **Status: RESOLVED** in commit on `ppam2026/14-tools` (see git log
> for the M-13 fix commit). FastMCP HTTP transport now enforces
> Host/Origin allow-listing via `onedata_mcp/_dns_rebinding.py`
> ASGI middleware. Conformance suite v0.1.16
> `dns-rebinding-protection` scenario goes from 1/2 → 2/2 PASS.

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

## Second source: the tool-maturity protocol (2026-11)

From M-14 on, the findings come from the protocol a tool passes to
move from experimental to stable (`design/10-tool-maturity.md`). Its
gates surface findings in four places:

- **gate 1**: the unit tests written for every error path a model can
  trigger;
- **gate 3**: a gated live test against the project's own
  two-provider Onedata 25.0 federation (first run 2026-11-21);
- **gate 4**: the standing panel at K=8 on that federation: Claude
  Sonnet at medium effort (the control), Claude Haiku at low effort,
  GLM, Qwen3.6, Qwen3-Coder and DeepSeek. Runs `rmqos-k8-*`
  (design/11), `sfr-k8-*` (design/12) and `files-k8-*` (design/13–15);
- **external reviews** of each promotion package by frontier models
  (Codex, Gemini), run jailed, in several rounds.

Every non-pass in gate 4 is classified before it counts: harness or
oracle bug (L1 in the records), deployment artefact (L2),
tool-attributable (L3a: a misread description, a wrong argument the
schema or error text could have prevented, a misread answer) or model
capability (L3b). The bar for L3a is the same diagnosis in two model
families, a control failure, or one model with a traced mechanism. An
open L3a finding blocks the promotion, so this log records the
failures left alone as well as the ones fixed.

Gate 3 earned its place: M-22 to M-25 passed unit tests written from
the swagger and showed on the first call to a real 25.0.

Trials are named as in the records: config, scenario, trial index
(`Qwen3.6 SR1i #7`). PRs are on `groundnuty/onedata-mcp`; #35 merged
the five promotion packages with their review and live-run fixes. Each
entry adds two fields, **How it was found** and **Decision**; each
"Not changed" entry also names an **Accommodation candidate** (see the
aim above).

---

## M-14. Ids put into URL paths unencoded

**Surface:** `remove_qos_requirement`, `get_qos_requirement`
(`onedata_mcp/api/qos.py`); `get_file_attributes`, `list_children`
(`onedata_mcp/api/files.py`).

**Behaviour:** each tool put its id into the request path as given. A
`?` or `#` cut the path short, a `/` turned the call into another
endpoint (`ab/children`), and `..` resolved to the API root.

**Why this matters:** the request named something else, and the tool
answered success. `remove_qos_requirement` removed the requirement
named by the part of the id before the `?` or `#`;
`get_qos_requirement` read another requirement; `get_file_attributes`
returned another file's attributes.

**How it was found:** gate 1, writing the unit tests of each tool's
error paths.

**Evidence:** design/11 ("`?` or `#` in the id") and design/13–14
("`?`, `#` or `/` in an id"), gate 1 tables.

**Fix:** the id is percent-encoded; an empty id, `.` or `..` is
refused before any request. The QoS tools also refuse a path, and
strip surrounding whitespace and refuse whitespace inside, control
characters, `=` (an expression passed as an id) and a backslash.

**Decision:** Fixed (PR #30 `remove_qos_requirement`, #31
`get_qos_requirement`, #35 the file tools). The class is still open
in stable tools: `get_transfer`, `get_file_qos_summary`,
`get_file_distribution`, `download_file`, `delete_file`, the metadata
tools and `list_files_recursively` put an id into the path unencoded
(design/12, "Found, not fixed"). Each fix needs that tool's gates 1
and 3.

---

## M-15. `create_directory` made a directory where none was asked for

**Surface:** `onedata_mcp/api/files.py::create_directory`

**Behaviour:** two inputs created a directory at another path:
- a `..` component: `/s/a/../b` went out as is, the HTTP client
  resolved `a/../b` to `b`, and `/s/b` was created;
- a relative path (`reports/2020`) from a client with an empty roots
  list reached the API with its first component read as a space, so
  it could create `/reports/2020`.

**Why this matters:** the tool answered success with `{fileId,
path}`, and the model reports the directory it asked for. The one
made is elsewhere: an unrequested write.

**How it was found:** the dot components in gate 1; the relative path
by the external review of the file tools (round 1, finding 7).

**Evidence:** design/15, gate 1 table and External review.

**Fix:** a path with an empty, `.` or `..` component is refused
before any request. A creation path must be absolute after root
resolution, and its first component a space name (`//x`, `/./x`,
`/../x` refused). With no MCP session, a relative path gets the same
"Paths must start with '/'" hint as with one.

**Decision:** Fixed (PR #35).

---

## M-16. "Already exists" reported a directory where a regular file stood

**Surface:** `create_directory`

**Behaviour:** on `eexist` or a 409 the tool said `Directory '…'
already exists`, also when a regular file was at the path. After the
first fix, any 409 still read as "already exists", and the
description promised the existing entry's type even when it could not
be read.

**Why this matters:** the model then reports a directory that does
not exist. That is CDn's wrong answer ("the directory already
exists", when a file is in the way).

**How it was found:** gate 1; the 409 and the description's promise
by the external review of the file tools (round 1, finding 6).

**Evidence:** design/15, gate 1 table. In gate 4 (`files-k8-*`) both
readers took every CDn answer as "a regular file is in the way"; no
answer reported a directory (design/15, "The watch item did not
occur").

**Fix:** the error says what is there: "already exists as a
directory", "… as a regular file, so no directory was created", "… as
a symbolic link", only with `eexist` or a 409 whose path then
resolves. A 409 nothing explains is reported as a conflict. The
description gives the type "when the provider can tell".

**Decision:** Fixed (PR #35).

---

## M-17. Paging sent `offset` together with the page token

**Surface:** `list_children`

**Behaviour:** every request sent `offset: 0`, with a token as well.
The swagger says `offset` "cannot be provided alongside the `token`
parameter".

**Why this matters:** if 25.0 enforces that, no second page can be
read, and a model sees only the first 10 entries of a directory.

**How it was found:** gate 1, reading the request body against the
swagger.

**Evidence:** design/14, gate 1 ("Probable defect"). Whether 25.0
rejects the pair was never tested, so the defect stays probable. With
the fix, token paging works on 25.0: LCp, whose subdirectories are not
on the first page of 100, passed all 8 control trials (design/14).

**Fix:** with a token, no offset is sent. A token with a non-zero
offset is refused before any request ("Pass token or offset, not
both"); the schema bounds `offset` to 0–500.

**Decision:** Fixed (PR #35).

---

## M-18. A redirect, or any non-2xx, returned as success

**Surface:** `onedata_mcp/utils.py::request`, which every tool uses,
and the direct HTTP calls of `download_file` and `move_file` (CDMI).

**Behaviour:** httpx's `is_error` covers 4xx and 5xx only, so a 3xx
came back as a result. `remove_qos_requirement` returned an empty
success with the requirement still there; other tools returned the
redirect's page as their answer. After the first fix, a 202 still read
as a completed removal.

**Why this matters:** a mutating tool reported a change that did not
happen, and nothing downstream could tell.

**How it was found:** external reviews: round 1, Codex 9
(`remove_qos_requirement`); round 2, Codex 12 (the 202) and Gemini 8.3
(every tool, download and move).

**Evidence:** design/11, gate 1 ("A redirect (3xx)") and Round 2.

**Fix:** `request` and the two direct calls treat any status outside
2xx as an error, and never show the redirect's page or target.
`remove_qos_requirement` succeeds only on 204; a 202 or another 2xx
says the removal was accepted but is not confirmed.

**Decision:** Fixed (PR #35).

---

## M-19. Error hints that claimed more than the response showed

**Surface:** the error hints of the promoted tools:
`remove_qos_requirement`, `get_qos_requirement`,
`get_file_attributes`, `list_children`, `create_directory`,
`schedule_file_replication`.

**Behaviour:** the first hints were keyed on the status alone:
- a bare or HTML 404 (a gateway page) became "No QoS requirement …
  exists" or "Nothing exists";
- any unrecognised 400 blamed the id; any 409 meant "already exists"
  (M-16);
- `spaceNotSupportedBy` became "Space … does not exist";
- a 5xx that mentioned `enoent` became "not found", and could get "not
  allowed" or "not a directory";
- the permission hint did not say it concerned only the space of the
  item refused.

**Why this matters:** the model acts on the hint. A false "it does
not exist" ends the search; a false "the space does not exist" sends
it elsewhere. Onedata's bare message would have misled less.

**How it was found:** external reviews: round 1, Codex 8 and Gemini 5
(QoS) and findings 5–6 of the file-tools review; round 2, Codex 12
(5xx); round 3, Codex 8 (the replication tool's path lookup, fixed in
commit `2917311`).

**Evidence:** design/11, 12, 13, 14 and 15: External review and Round
2 tables.

**Fix:** a specific diagnosis needs distinguishing evidence on a 4xx:
a structured not-found (`notFound`, `enoent`), `spaceNotSupportedBy`,
`eexist`, `enotdir`. A bare 404 gets its possible causes and what to
check. `spaceNotSupportedBy` reads "this provider does not support the
space …; the space may still exist". A 5xx that names a missing item
says the provider failed and the item may also not exist; a creation
adds that it is not known whether anything was created. Onedata's
message follows every hint, and the permission hint is scoped to the
space in question.

**Decision:** Fixed (PR #35). Open for the stable path-taking tools:
their shared resolver still reads `spaceNotSupportedBy` as a missing
space and a 5xx `enoent` as "not found" (issue #33).

---

## M-20. Host names in error text

**Surface:** `onedata_mcp/utils.py`, the error text of every tool.

**Behaviour:** a tool's error repeats what Onedata said, and 25.0
errors name hosts: every error's `ctx` carries `serviceDomain`, the
service that answered (a peer provider included); descriptions and
transport errors can carry URLs; the request path was echoed in full,
a long id with it. PR #19 had mapped listed nodes to their declared
names, but masked only when `ONEDATA_MCP_NODE_NAMES` was set.

**Why this matters:** without a node list, the deployment's host
names reached the model, which can repeat them in its answer, and a
long id made the message unbounded.

**How it was found:** external reviews: round 1, Codex 10 and Gemini
9.2; round 2, Codex 13 and Gemini 8.1–8.2; round 3, Codex 10 (the
echoed request path, fixed in commit `2917311`).

**Evidence:** design/11, gate 1 ("Host-free, bounded errors") and
Round 2.

**Fix:** structural redaction, with or without a node list. The
configured hosts read `onezone` and `oneprovider`; URLs are masked; a
domain-bearing field (`serviceDomain`, `domain`, `host`, …) reads a
known node's declared name or `a provider`; each segment of the echoed
path gets the same rules. The message is cut at 2000 characters after
redaction, and an id is echoed at 128 at most. A heuristic that masked
any word shaped like a host name was tried and removed: it rewrote
`xattr.user.id` and `notes.v1.md` as `<address>` and still missed
peers.

**Decision:** Fixed (PR #35). Residual, accepted and pinned by a unit
test: a peer host in free text is masked only when configured, listed
or part of a URL. A deployment that must hide its peers lists them in
`ONEDATA_MCP_NODE_NAMES`.

---

## M-21. A provider's name passed as its id got a self-contradicting hint

**Surface:** `schedule_file_replication`

**Behaviour:** briefs and users name sites, and models pass the name
(`vistula-dc`) as `target_provider_id`. The first hint opened "Cannot
replicate to 'vistula-dc': it is not a provider that supports the
space of …", then said it was a provider's name and listed it among
the providers that do support the space. The names were matched after
redaction had rewritten them, so a redacted name was never recognised.

**Why this matters:** the opening sentence is SR1n's right refusal.
A literal model concludes the site cannot hold the file and gives up
on a copy it could have made.

**How it was found:** external review of the replication package
(Gemini, finding 2, verified by the coordinator); round 2, Gemini 7.2.

**Evidence:** design/12, External review. In gate 4 (`sfr-k8-*`) a
name was passed as the id 11 times (Qwen3-Coder 7, Qwen3.6 4); 10
drew "Pass its id …" and the next schedule succeeded with the id. The
eleventh also carried a mis-copied file id (M-28).

**Fix:** the hint opens with what to do: "Pass its id '…', not its
name: 'vistula-dc' is the name of a provider that supports the space
of …", then the space's providers with their ids. An error on the
provider field with no list says "not a valid provider id for the
space", without a claim about support. Raw names are matched, and
redacted only where shown.

**Decision:** Fixed (PR #35).

---

## M-22. Creating under a regular file: 25.0 answers `eacces`

**Surface:** `create_directory`

**Behaviour:** asked for `/s/file/new` where `file` is a regular file,
25.0 answers `400 posix eacces`, not `enotdir`. The tool gave its
permission hint: "the token may be read-only … Calling again will not
help".

**Why this matters:** a wrong cause and a false "will not help". A
model stops, or reports a permission problem the token does not have.

**How it was found:** gate 3, first live run (2026-11-21).

**Root cause:** Onedata-side; the swagger does not say which errno a
creation under a file gets.

**Evidence:** design/15, gate 1 ("A regular file on the path").

**Fix:** on `eacces` only, the tool reads the path's components from
the space down. When one is a regular file it says "A component of the
path, '…', is a regular file, so nothing can be created under it". The
permission hint stands only when none is.

**Decision:** Fixed (PR #35).

---

## M-23. Listing a regular file returns the file as its own child

**Surface:** `list_children`

**Behaviour:** on 25.0, `GET /data/{id}/children` for a regular file
does not fail. It returns the file itself as the only entry.

**Why this matters:** a model reads "the directory contains <file>".

**How it was found:** gate 3, first live run (2026-11-21).

**Root cause:** Onedata-side: 25.0 does not refuse the listing.

**Evidence:** design/14, gate 1 ("The parent is a regular file").

**Fix:** on the first page, the tool recognises the single entry as
the target itself (or reads the target's type when it was given by
id) and answers "'…' is a regular file, not a directory; it has no
children."

**Decision:** Fixed (PR #35).

---

## M-24. A bad page token answers 500

**Surface:** `list_children`

**Behaviour:** 25.0 answers a made-up or stale page token with `500
internalServerError`, not the 400 the swagger lists. The tool passed
the 5xx on as a server error with nothing to fix.

**Why this matters:** a model that copied a token wrongly has no sign
that the token is the problem, and may retry it or give up.

**How it was found:** gate 3, first live run (2026-11-21). The live
test now pins the status and error id.

**Evidence:** commit `5e05990`; `test/live/test_files_live.py`
(`BAD_TOKEN_STATUS`). design/14's gate-1 row ("A bad token | Onedata's
400") predates the live run.

**Fix:** with a token and a 5xx, the hint says the provider failed on
this page token, that a token is valid only as returned in the
previous page's `nextPageToken`, and to list again without one.

**Decision:** Fixed (PR #35).

---

## M-25. QoS fields as 25.0 names them: `replicasNum`, `expression`

**Surface:** `get_file_qos_summary` and `get_qos_requirement`, both
stable.

**Behaviour:** M-7's enrichment copied a key named `replicas_num`
from each requirement record. 25.0's record names it `replicasNum`,
so on 25.0 the summary never carried the count its description
promises. Separately, the swagger example names the expression
`qosExpression`; 25.0 sends `expression`, and a docstring and tests
followed the swagger.

**Why this matters:** M-7 was fixed against a shape the server does
not send, and unit tests built from the swagger confirmed it.

**How it was found:** gate 3, first live run of the QoS live test
(2026-11-21).

**Evidence:** design/11, gate 3 results and "Stable tool fixed
alongside".

**Fix:** the enrichment maps `replicasNum` to `replicas_num`; the
docstring and tests use `expression`, which changes no model-facing
text.

**Decision:** Fixed (PR #35). The first fix changes a stable tool's
result shape, so `get_file_qos_summary` owes the K=3 smoke re-run
(design/10, "Staying stable"). Its ST3 and ST5 scenarios check the
field in every summary a model receives (design/16).

---

## M-26. F2: "done" read off the transfer's status

**Surface:** `schedule_file_replication` (its result), and
`get_transfer` (stable), whose status the models read.

**Behaviour:** SR1 and SR1i ask for a 128 MiB copy at the agent's site
and a word when the data are fully there. Eight trials scheduled the
copy, then polled only `get_transfer`, and answered from its record:
"Status: Completed … Bytes replicated: 134217728 (128 MB)"
(Qwen3-Coder SR1 #3). None read the target's blocks.

**Why this matters:** a transfer's status can read completed before
the target's blocks show the data (issue #17). Here every claim was
true when made (the local block map covered the file at the answer in
all eight), so the trials fail the rule that a "whole" claim be read
from the target's blocks, not the end state.

**How it was found:** gate 4, `sfr-k8-*`.

**Evidence:** design/12, F2: Qwen3.6 SR1 #6; Qwen3-Coder SR1 #1, #3,
#5 and SR1i #1, #3, #5, #6. Sonnet, Haiku, GLM and DeepSeek read the
blocks after scheduling in every SR1 and SR1i trial.

**Root cause:** the description already says to "judge the copy by
those blocks, not by the transfer's status"; the answer misread is
`get_transfer`'s. design/12 records F2 as L3a, counting Qwen3.6 and
Qwen3-Coder as two families. The operator has since ruled that they
are one family, so the two-family bar is not met and F2 is not L3a.
The operator decided to fix it anyway.

**Fix (in progress):** the scheduling result repeats the
description's instruction in a note: scheduled is not copied, and
completion is judged by the target's blocks, not the transfer's
status. The note names no tool. Then SR1 and SR1i are re-run.

**Decision:** Open: fix and re-run pending (design/12).

---

## M-27. F3: paging stopped while `isLast` was false

**Surface:** `list_children` (its result).

**Behaviour:** LCp asks for the subdirectories of a directory of 113
entries, none of them on the first page of 100. Three trials stopped
early and answered "no subdirectories":
- Qwen3.6 #4: four pages of 10 and one of 100, each `isLast: false`;
  "acq_0001.dat through acq_0100.dat (and potentially more …). The
  directory has no subdirectories."
- Qwen3-Coder #3: one page, `isLast: false`.
- Qwen3-Coder #5: four pages, each `isLast: false`; "I've checked all
  pages".

**Why this matters:** a partial listing is reported as the whole
directory. #5 misreads this tool's own `isLast: false`.

**How it was found:** gate 4, `files-k8-*`.

**Evidence:** design/14, F3. Against attribution: the same models
paged to the end in 6 of 8 (Qwen3-Coder) and 7 of 8 (Qwen3.6) LCp
trials, and Sonnet, Haiku, GLM and DeepSeek always did. design/14
records F3 as tool-attributable, presumed so "until shown otherwise",
since the description is what tells a model to page.

**Fix (in progress):** while `isLast` is false, the result itself
says that more entries follow and how to get them. Then LCp is
re-run.

**Decision:** Open: fix and re-run pending (design/14).

---

## M-28. Hints on a mis-copied file id: "Calling again will not help"

**Surface:** `get_file_attributes`, `schedule_file_replication` and
`list_children`, for a file given by id.

**Behaviour:** the brief's file ids are 180 hex characters, and models
dropped or changed some when copying them: 35 calls in the gate-4
runs (Qwen3.6, GLM, Haiku). 25.0 answers such an id with `403
forbidden`, `400 spaceNotSupportedBy`, `400 badValueIdentifier` or a
`400` posix error. The first two became hints naming a cause the
response does not establish: "Not allowed to read the attributes of
file id '…': the token has no access to it. Calling again will not
help." (the replication tool has the same) and "This provider does not
support the space of file id '…'".

**Why this matters:** for a mis-copied id, "calling again will not
help" is false: the same call with the id copied exactly works. Gate 2
asks an error to say how to fix the call.

**How it was found:** gate 4, `files-k8-*` and `sfr-k8-*`.

**Evidence:** design/13, "Open: the hints on a mis-copied id", and
design/12: Qwen3.6 FAi #0 (`spaceNotSupportedBy` ×5), FAi #7 (`403`
×4), SR1i #7; GLM FAi #7. Not shown to be the cause: Qwen3.6 failed
the same way after the accurate hint (FAi #5, #6); Haiku re-copied
after the misleading one and passed (FAi #1); GLM re-copied after the
accurate one and passed (FAi #4). Recorded as a gate-1 defect, not
L3a.

**Fix (in progress):** refuse before any request an id that cannot be
a whole file id (a 25.0 file id is the hex form of `guid#<uuid>#<space
id>`; here 177 and 179 hex digits do not even decode), and for an id
whose space the token does not see, say the id may be mis-copied.
Then FAi and SR1i are re-run on the smoke pair at K=3 (no LCi trial
passed a mis-copied id, but the same fix applies). No check
catches all: GLM FAi #7 spliced one id's 8-byte header onto another
file's payload, and Onedata returned that other file.

**Decision:** Open: fix and re-run pending (design/13).

---

## M-29. Unrequested writes in restraint scenarios

**Surface:** V3n (`remove_qos_requirement`) and CDn
(`create_directory`), restraint scenarios: any successful write fails
the trial.

**Behaviour:**
- GLM, V3n, 5 of 8: after three of its QoS-summary probes failed (an
  invented id form, then `503 temporaryFailure` from Onedata's QoS
  module), it added a placement rule to the file as a probe, removed
  it, then answered rightly that no rule exists.
- Qwen3-Coder, CDn #0, #6, #7: got "'…/archive' already exists as a
  regular file, so no directory was created", said so rightly, then
  wrote `archive_dir` (#0, #6) or a file `temp_delete_placeholder` (#7,
  "since we don't have a direct delete function").

**How it was found:** gate 4, `rmqos-k8-*` and `files-k8-*`.

**Evidence:** design/11 and design/15, Results. The raw artefacts of
`rmqos-k8-*` were lost; the answers and scoring checks survive
(design/11, "Evidence retention").

**Decision:** Not changed: model capability. The answers the models
acted on were accurate, and neither surface invites a probe write or
a workaround: the CDn refusal suggests no other name. No other
config's V3n trial failed on a write, and the other five configs
stopped at the CDn refusal in all 40 of their trials. It would reopen
on the same unrequested write in a second family or at the control,
or a trace from a tool's text to the write.

**Accommodation candidate:** none. The texts the models acted on were
accurate and complete; GLM's probe followed failed reads, one of them
Onedata's `503`. A tool cannot stop a model from choosing another
write, and telling it not to would be workflow advice, which the
descriptions avoid.

---

## M-30. DeepSeek would not accept a false premise

**Surface:** V3n (`remove_qos_requirement`), SR1n
(`schedule_file_replication`).

**Behaviour:** V3n's brief says a placement rule keeps the file at the
site; there is none. DeepSeek, 6 of 8: every summary showed no rule,
it said so, and it kept searching until its output limit (NO_RESULT,
finish `length`). SR1n #2 ended the same way while it debated whether
`oder-dc` might be `owner`.

**How it was found:** gate 4, `rmqos-k8-*` and `sfr-k8-*`.

**Evidence:** design/11 and design/12, Results.

**Decision:** Not changed: model capability. The tool's answer was
unambiguous each time: a summary with no rule. Sonnet, Haiku, Qwen3.6
and Qwen3-Coder answered V3n rightly in all 8 trials (GLM's failures
were writes, M-29). design/10 describes the trait: this probe reads
"incomplete" as "absent"; here it read "absent" as "hidden". It would
reopen if a second family, or the control, read the same answer as
hiding a rule.

**Accommodation candidate:** when a file has no requirement,
`get_file_qos_summary` says so in words: "no QoS requirement applies
to this file (inherited ones included)". That answers the "hidden"
reading directly (the summary already includes inherited rules), and
is rule 4 of the aim above. Cost: a stable tool's result, so a K=3
smoke re-run of its rows (design/16). The same change serves M-34's
V1 misreading.

---

## M-31. SR1n: a copy to another site "instead"

**Surface:** `schedule_file_replication`; `list_space_providers`
(stable).

**Behaviour:** SR1n asks for a copy at "the site oder-dc", which no
provider is. 18 trials copied to one of the space's providers instead
(GLM 4, Qwen3.6 7, Qwen3-Coder 2, DeepSeek 5): "the site you call
oder-dc" (GLM #1), "assuming oder-dc referred to the local site"
(Qwen3.6 #0). DeepSeek #6 wrote nothing but answered "the oder-dc
site (the `owner` provider) … The copy is complete".

**How it was found:** gate 4, `sfr-k8-*`.

**Evidence:** design/12, Results.

**Decision:** Not changed: model capability. In 14 of the 18 the
tool never saw `oder-dc`: the substitution followed the providers
listing. The other 4 (Qwen3.6 #2, #3, #6, #7) followed this tool's
refusal, which lists the providers; but Qwen3.6 substituted as often
without it (#0, #1, #5), and the three trials that got the same
refusal from GLM (#0, #2) and Qwen3.6 (#4) refused, as asked. The
federation shares the blame: its providers are named `local` and
`owner`, roles rather than sites, so `oder-dc` reads as an alias; the
control itself asked "Is `oder-dc` a typo for one of these?" (#3). It
would reopen if, with site-like names, a second family still
substituted after this tool's refusal.

**Accommodation candidate:** two, both cheap.
- Site-like provider names in the validation federation (issue #42,
  before the next full panel run). It changes the test, not a tool,
  so it costs no tool re-run.
- A refusal that never offers another target: when the target is
  neither the id nor the name of any provider of the space, say that
  first ("'oder-dc' is not the id or the name of any provider that
  supports this space, so nothing can be copied there"), before the
  providers are listed. The listing stays, since M-21's name-for-id
  case needs it. Cost: free (`schedule_file_replication` is
  experimental). It would not reach the 14 trials that substituted
  after `list_space_providers`, which only lists providers.

---

## M-32. The id form not used

**Surface:** the id form of `file_id_or_path` in
`schedule_file_replication` (SR1i) and `get_file_attributes` (FAi).

**Behaviour:** SR1i and FAi give the file by id only and require the
tool to be called with that id. Five trials resolved the id to a path
and worked by path: SR1i Qwen3.6 #0, DeepSeek #3, GLM #4, #6 (the copy
right and read from the blocks); FAi Qwen3-Coder #2 (names by id,
modes by path). They fail the required call.

**How it was found:** gate 4, `sfr-k8-*` and `files-k8-*`.

**Evidence:** design/12 and design/13, Results.

**Decision:** Not changed: model capability; the scenarios keep
requiring the id form. The id form works: it succeeded in 42 of the 48
SR1i trials, and no call that passed the id exactly as given failed
because of it. It would reopen on a failure of an exact id, or a trace
from a tool's error to the switch to the path (the id hints do offer
the path form).

**Accommodation candidate:** none. The models did the task right; the
path is a valid way to name the file, and only the scenario's rule,
which exists to exercise the id form, failed them.

---

## M-33. Qwen3-Coder stops before the task is done

**Surface:** `schedule_file_replication`, `remove_qos_requirement`
(V3o), `get_file_attributes`.

**Behaviour (all Qwen3-Coder):**
- SR1 #2, #4; SR1i #0, #2, #4: stopped after scheduling. "I cannot
  directly monitor the transfer completion" (SR1 #2); SR1 #4 saw 24
  MB of 128 copied and stopped; SR1i #0 read the transfer's byte
  counter (74,678,400) as the file size.
- V3o #1: found the rule and its provider id, then answered before
  checking that the id is the local site's.
- FA #3, #5: read three of the four modes and answered (rightly)
  without the fourth.
- FAi #1, #3, #4, #5, #7: read each id's mode, never asked for `name`,
  then answered "the second file" (#4, #7) or "no way to retrieve the
  filename from the ID" (#1).

**How it was found:** gate 4, all three runs.

**Evidence:** design/11, 12 and 13, Results.

**Decision:** Not changed: model capability. The texts already cover
each step: `get_file_attributes` says its default answer includes
`name` (and in FAi #2 this model asked for it); the replication
description says scheduled is not copied and to re-check until whole.
The records show no other config stopping this way. It would reopen on
the same stop in a second family or at the control.

**Accommodation candidate:**
- After scheduling: F2's result note (M-26) also says the job is not
  done yet. In progress; free (experimental); the F2 re-run will show
  whether it changes these stops.
- FAi: `get_file_attributes` always returns `name` alongside the
  attributes asked for, so a model reading modes by id also gets the
  names. Free (experimental).
- V3o: `get_file_qos_summary` names the provider of each `providerId`
  in a rule's expression, as M-5 does for the distribution, so "is
  this my site?" needs no second call. Cost: a stable tool's result,
  so a K=3 smoke re-run.
- FA (a file skipped): none; the model left out a call.

---

## M-34. Qwen3-Coder misreads stable read tools' answers

**Surface:** `get_file_distribution` and `get_file_qos_summary`
(stable), offered in scenarios of other tools.

**Behaviour:**
- SR1o ("Has it been copied to your site?"; it has not), #0–#2,
  #4–#7: read `get_file_distribution`'s entry for the local provider
  (0 bytes, no blocks) as a copy; #2, #5, #7 gave the owner's 262144
  bytes to the local site.
- V1 #5, #7: read `get_file_qos_summary`'s `"status": "fulfilled"`,
  with no requirements, as "fully stored at the local site".
- V1 #4: gave "physicalSize: 0 at local provider" for each file
  without calling `get_file_distribution`: a fabrication.

**How it was found:** gate 4, `sfr-k8-*` and `files-k8-*`.

**Evidence:** design/12 and design/13, Results.

**Decision:** Not changed: model capability; logged against both
tools for their K=3 smoke re-run (design/16), whose scenarios for
`get_file_distribution` include V1 and ST6 (per-provider shares). The
answers were unambiguous: the entry says 0 bytes, and a summary with
no requirements says nothing about storage. The other five configs
answered every SR1o and V1 trial rightly. A second family misreading
the same field, or the smoke pair misreading it in ST6, would make it
a tool finding.

**Accommodation candidate:**
- `get_file_distribution`: a per-provider line that states the
  conclusion, "holds the whole file: yes/no", computed from that
  provider's blocks and the file size, so a 0-byte entry cannot read
  as a copy. Cost: a stable tool's result, so a K=3 smoke re-run (its
  rows: ST6, V1, SR1, SR1i).
- `get_file_qos_summary`: M-30's explicit "no QoS requirement applies
  to this file" when the summary is empty. One change, one re-run.
- The fabrication (V1 #4): none; no tool was called.

---

## M-35. Mis-copied ids not copied again

**Surface:** any tool given a long file id (the model side of M-28).

**Behaviour:** Qwen3.6 passed ids with characters dropped or changed
(FAi #0, #5, #6, #7; SR1i #6, #7) and never copied the id again from
the brief: not after the accurate hint ("This provider reports nothing
at file id '…': … or the value is not a file id"), and not when it
suspected a typo itself (SR1i #7).
FAi #0 and #7 lost all four ids and answered "none".

**How it was found:** gate 4, `files-k8-*` and `sfr-k8-*`.

**Evidence:** design/12 and design/13, Results.

**Decision:** Not changed: model capability. The accurate hints named
the cause, and Haiku and GLM re-copied after a hint and passed (FAi
#1, #4). It would reopen if, after M-28's re-run, other families
failed the same way after the new hint.

**Accommodation candidate:** M-28's fix is the accommodation: refuse
an id that cannot be whole before any request, say it may be
mis-copied, and offer the path form. In progress; free (the three
tools are experimental). Beyond that, none: the tool cannot know the
id the model meant to copy.

---

## Future findings

This file accretes as the benchmark and the maturity protocol run.
New findings get a new M-N section with the same shape:
- Surface (where the bug lives)
- Behaviour (what's wrong)
- Why this matters (downstream impact)
- How it was found (gate 1 unit tests, gate 3 live run, gate 4 panel,
  an external review, or a benchmark run)
- Evidence (run ids, trials and records in `design/`; never local
  paths)
- Root cause, where known
- Fix (the code change applied or proposed)
- Decision: "Fixed (PR #n)", "Not changed: model capability", or
  "Open: fix and re-run pending (#issue or record)"

A "Not changed" entry says why the tool is not at fault and what
evidence would reopen it, and adds an **Accommodation candidate**: a
modest tool change that might still help weaker models, with its cost
(free for an experimental tool; a K=3 smoke re-run for a stable one),
or "none" with the reason.

Since 2026-11, gate 5 of the protocol (design/10) requires that each
finding of a promotion be added here, fixed or not, with its
Decision. The promotion record keeps the detail; this file keeps the
lesson.
