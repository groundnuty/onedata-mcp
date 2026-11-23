# Empirical findings — Onedata 25.0 + SPICE deployment

**Maintained 2026-04-30 onwards.** Implementation-side observations that
came out of building the PPAM 2026 fork against
`data.spice-platform.eu`. Distinct from `papers/ppam-2026/research/` which
hosts paper-relevant context; this file is for future MCP-server
developers and is referenced from `IMPLEMENTATION_NOTES.md`.

## 1. Three corrections to paper §3 spec endpoint paths

The original implementation spec (`papers/ppam-2026/research/22-mcp-implementation-spec.md`)
gave indicative endpoint paths and explicitly said "verify against the
live `https://data.spice-platform.eu/api/v3/` REST documentation before
coding". Three of the spec paths were wrong; verified against
`oneprovider-swagger@25.0` (commit `407940e` of `25.0` tag, no drift vs
`develop` tip on these paths):

| Spec §3 said | Reality (25.0 swagger) | operationId |
|---|---|---|
| `GET /data/{file_id}/qos_summary` | `GET /data/{file_id}/qos/summary` *(slash, not underscore)* | `get_file_qos_summary` |
| `POST /data/{file_id}/qos_requirements` *(per-file URL)* | `POST /qos_requirements` *(top-level; fileId in JSON body)* | `add_qos_requirement` |
| `state ∈ {ongoing, completed, failed, all}` for `list_space_transfers` | `state ∈ {waiting, ongoing, ended}` | `get_all_transfers` |

Caught this at task #7 (verify endpoints) before any code touched the
wrong paths. Each is documented as a paper-text edit the writing agent
will need to make in `IMPLEMENTATION_NOTES.md` and surfaces again in
`design/03-tool-allowlist-curation.md`.

## 2. The "private move endpoint" is actually CDMI

The GitLab Onedata MCP server's `OnedataFileRESTClient.move()` was
described as posting to a "private undocumented endpoint" (paper §7
Threats to Validity, Infrastructure). Tracking down the source
(`onedatafilerestclient` submodule of `onedatarestfsspec`, commit
`6887661`, `onedata_file_rest_client.py:527-555`):

```
PUT https://{oneprovider}/cdmi/{dst_space}/{dst_path}
Headers: X-Auth-Token, X-CDMI-Specification-Version: 1.1.1,
         Content-Type: application/cdmi-object
Body:    {"move": "<src_space>/<src_path>"}
```

It's **CDMI** — Cloud Data Management Interface, an SNIA-standardised
protocol. Not in Onedata's `/api/v3/oneprovider/` swagger, so the §7
caveat about "no public REST contract" stands; but CDMI itself is a
documented standard, just integrated with Onedata's auth/space
namespacing in undocumented ways. The Python client raises an explicit
error if `src_space != dst_space` — **CDMI move is intra-space only**.

Decision recorded in `design/01-move-file-strategy.md`.

## 3. Onepanel auth quirk: `admin` vs `onepanel` users

The SPICE deployment helm values (`enviroments/*/values/*.yaml`) declare
two admin accounts on every OnePanel:

```yaml
onepanel_emergency_account:
  name: onepanel
  password: SP!CEPLATF@RM
main_onezone_admin:        # only on the onezone helm values
  name: admin
  password: SP!CEPLATF@RM
```

In practice:

- **Onezone OnePanel** (port via `data.spice-platform.eu/api/v3/onepanel/`): both `admin` and `onepanel` work.
- **Per-provider OnePanels** (`cloud-pl.../onepanel/`, `cloud-sk.../onepanel/`, `uibk.../onepanel/`): **only `onepanel` works**. `admin:SP!CEPLATF@RM` returns `unauthorized / badBasicCredentials`.

Discovered while listing storages on cloud-pl + Cloud-SK before
supporting `ppam_2026_mcp_tests`. Probably an artefact of how the
deployment provisions accounts — the `main_onezone_admin` clause only
appears in the onezone helm values, not the per-provider values.

**Operational implication:** all per-provider OnePanel curls must use
`onepanel:SP!CEPLATF@RM`. The mcp-side code only talks to `/api/v3/oneprovider/`,
not `/api/v3/onepanel/`, so this quirk doesn't affect the MCP server
itself — but it matters for any space-support / federation-state
maintenance scripts.

## 4. POST `/api/v3/onezone/user/spaces` returns 201 + empty body

Tripped the first space-create attempt: my response parser tried to
extract `spaceId` from the JSON body, but the response was empty. The
spaceId is in the `Location` header:

```
HTTP/2 201
location: https://data.spice-platform.eu/api/v3/onezone/user/spaces/<sid>
content-length: 0
```

Created a duplicate space silently before realising the first call had
succeeded. Cleaned up by deleting the duplicate. **Always parse the
Location header on Onedata 201s, not the body.**

## 5. `list_space_transfers` returns transferIds only

The `/api/v3/oneprovider/spaces/{sid}/transfers` endpoint returns:

```json
{
  "transfers": ["<tid>", "<tid>", ...],
  "nextPageToken": "<token>" | null
}
```

No metadata — no source / destination / state / bytes / timing per
transfer. Agents needing detail must follow up with `GET /transfers/{tid}`
per id (`get_transfer` tool).

This drove the inclusion of `get_transfer` in the headline 15-tool
allowlist (`design/03`). Without it, scenario P4 ("most-recent migration
of file F") is unsolvable from IDs alone.

## 6. The federation that runs vs the federation Onezone advertises

Onezone reports 5 registered providers (`/api/v3/onezone/user/effective_providers`):

| Name | Country | Online (2026-04-30) | Notes |
|---|---|---|---|
| cloud-pl | PL (Cyfronet) | ✅ | bound to `ppam_2026_mcp_tests` |
| Cloud-SK | SK (IISAS) | ✅ | bound to `ppam_2026_mcp_tests` |
| uibk | AT (UIBK) | ✅ | available, not bound |
| Cloud | DE (azure-interway) | ❌ | namespace exists, no pods |
| Edge | DE (azure-interway) | ❌ | namespace exists, no pods |

Querying the cluster directly:

```
$ ssh azureuser@data-spice... 'kubectl get pods -A | grep -iE "onezone|oneprovider"'
onezone-spice  onezone-0  Running  1/1  58d
# (no oneprovider-cloud or oneprovider-edge pods despite namespaces)
```

So onezone has 5 providers in its registry but only 3 of them have a
running data plane. `getProviderDetails(...).online` is the authoritative
reachability signal; the registry alone over-states federation health.

The paper's headline claim of "5 OPs / 4 countries / 75 GiB" is currently
inaccurate against reality (3 OPs reachable / 3 countries / X GiB
support actually attached to the benchmark space). Tracked for the
paper-writing agent as a §4.1 edit.

## 7. 25.0 vs develop tip vs 25.1: zero drift on the endpoints we use

Verified at task #7. After pinning all three swagger repos to tag `25.0`
(matching what the live federation reports as `version: 25.0`):

```
diff 25.0 25.1 on each of:
  paths/data/id/distribution.yaml
  paths/data/id/qos/summary.yaml
  paths/qos_requirements.yaml
  paths/spaces/sid/transfers.yaml
  paths/spaces/sid.yaml
  paths/data/id.yaml
  definitions/data/data_distribution.yaml
  definitions/qos/qos_summary.yaml
  definitions/qos/qos_create_request.yaml
  definitions/qos/qos_requirement.yaml
→ 0 lines diff on each
```

So the verification we did against develop tip earlier in the session
also holds against the deployed 25.0. Useful precedent: for stable
endpoint surfaces, swagger develop tip is a reasonable proxy when the
deployed version is 1-2 minor releases behind.

## 8. The `-spice-v1` onezone patch is still unverified

The deployed onezone runs `onedata/onezone:ID-ba7a778696-spice-v1`. The
`-spice-v1` suffix is a SPICE-specific patch on the 25.0 base. We
haven't yet hit any onezone-side endpoint where the patch's behaviour
would differ from upstream 25.0 (the MCP server's `list_space_providers`
calls *oneprovider*'s `/spaces/{sid}`, not onezone's). Tracked as
conversation task #23.

If we later need richer per-provider attributes (geo, storage class)
via onezone's `/providers/{id}`, we should compare the response shape
against upstream 25.0 swagger before assuming.

## 9. Token caveats are unbounded

The benchmark token (`ppam2026-mcp-bench-2026-04-30`) was minted
without any caveats — no time limit, no service restriction. To be
tightened pre-camera-ready (planned: 30-day time caveat, service caveat
scoping to oneprovider-only). Onedata supports caveat composition via
the `caveats` field on `POST /api/v3/onezone/user/tokens/named`. The
schema is in `onezone-swagger@25.0/definitions/token/named_token_create_request.yaml`.

Documented in `papers/ppam-2026/research/27-benchmark-space-snapshot.md`
under the Token section.

## 10. dirStatsServiceEnabled defaults to true on space support

When supporting `ppam_2026_mcp_tests` from cloud-pl + Cloud-SK, OnePanel's
default for `dirStatsServiceEnabled` was `true` on both providers. We
didn't request it; it was on by default. This means directory-level size
statistics are tracked, which P1 / P6 oracles may benefit from. Worth
knowing if a future deployment opts out.

## 11. CDMI move requires explicit `Accept: application/cdmi-object`

Discovered live 2026-05-01 first `--write` smoke. The CDMI PUT for move
returns **HTTP 406 Not Acceptable** if the request omits an `Accept`
header (or uses `*/*`):

```
v1 (no Accept):                               → 406
v2 (Accept: application/cdmi-object):         → 201 ✓
v3 (Accept: */*):                             → 400 (file already moved by v2)
```

Onedata's CDMI implementation strictly requires the Accept header to
match the request body media type. The Python `OnedataFileRESTClient`
that we ported from happens to set this — but the snippet quoted in
`design/01-move-file-strategy.md` glossed over it. Fixed in
`onedata_mcp/api/files.py::move_file` (commit `999b987`, 2026-05-01).

This is the most important "spec is incomplete" gotcha we've found:
the CDMI 1.1.1 standard *recommends* matching Accept but doesn't
mandate it; Onedata's enforcement of "match exactly" is stricter than
the standard. Worth flagging in the paper §7 *Threats / Infrastructure*.

## 12. Convergence is fast (~6-7s) for small fixtures, well under the soft cap

The first live fixture_runner exercise (D5 = 1 file with metadata; A2
= 4 files with metadata) converged in **6.0s and 6.8s** respectively on
the cloud-pl + Cloud-SK pair. Soft cap is 60s, hard cap is 120s. So
the conservative caps from paper §4¶5 leave 10× headroom on small
fixtures.

This is encouraging for the full sweep timing budget but doesn't
generalise to scenarios with many files (P1 puts an 8-file fixture
under QoS) or to the bigger scenarios with `replicas_num=2` constraints
that need both providers to settle. The dbsync calibration sweep
(paper §4¶5 TODO) should still happen before the camera-ready run; the
6-7s number is a lower bound, not the worst case.

## 13. Four-cell oracle matrix verified live

The two-axis OracleResult (design/06) survives contact with reality.
Smoke run 2026-05-01 explicitly exercised three cells:

- D5 with correct answer: `mcp_pass=True, federation_pass=None` (format-tier).
- A2 with correct writes: `mcp_pass=True, federation_pass=True`.
- A2 with empty agent trace: `mcp_pass=False, federation_pass=False`.

The fourth cell (`mcp_pass=True, federation_pass=False` — Onedata-side
divergence) hasn't naturally occurred yet. We'll see it the first time
dbsync lags or a transient 5xx hits during a real benchmark sweep.

**Update 2026-05-01 evening**: the fourth cell (mcp=T, federation=F)
showed up *naturally* in the placement-band smoke after the QoS-rule
reauthoring (entry #14). See entry #16.

## 14. SPICE federation has NO admin-set QoS attributes

Discovered live 2026-05-01 placement-band smoke. Every QoS rule the
benchmark fixture-runner issued ended up in `status=impossible` despite
identical cluster, identical token, identical providers being online.
Probed via the `evaluate_qos_expression` endpoint:

```
expr='anyStorage'                           → matches 2 (both POSIX)
expr='country=PL'                           → matches 0
expr='country=SK'                           → matches 0
expr='geo=PL' / 'geo=SK' / 'geo=EU'         → matches 0
expr='type=posix'                           → matches 0
expr='providerId=27c0...d591'   (cloud-pl)  → matches 1
expr='providerId=736092c5...1411' (Cloud-SK) → matches 1
expr='storageId=1f72...2e5d'                → matches 1
```

Conclusion: the SPICE deployment's POSIX storage backends carry **no
admin-configured QoS tags**. Only implicit operands work
(`providerId=`, `storageId=`, `anyStorage`). User-attribute tokens
(`country=`, `geo=`, `type=`) compile but match no storages, leaving
any rule using them pinned in `impossible` status.

This is the most consequential finding so far for paper §4 / §6: any
scenario brief naming `country=` / `geo=` / `type=` will fail not on
agent capability but on missing federation configuration. Two paths:

- **Short term** (this paper): re-author scenarios to use `providerId=`
  expressions. Captured in `benchmark/_federation_constants.py`.
- **Medium term**: ask Cyfronet to configure storage QoS tags
  (`country`, `geo`, `type`) on the SPICE providers. They're admin-set
  per-storage-backend, so it's a 3-line config change per provider.
  This would let scenarios use the paper-canonical syntax, matching
  Onedata's user-facing QoS docs and the curation argument the paper
  makes about agent-friendly DSLs.

Verified by commit `68ac2ee` (paths in scenarios.py + oracles/EU_TOKENS).

## 15. Field-name corrections discovered live

Two cases where the Onedata 25.0 swagger's *example body* uses one
field name but the actual JSON returned from the live federation uses
another. Both bit our oracles before the live smoke.

| Endpoint | Swagger example shows | Live response uses |
|---|---|---|
| `GET /qos_requirements/{qid}` | `qosExpression` | **`expression`** |
| `GET /data/{id}/distribution` | `logicalSize` (older docs) | **`virtualSize`** |

Permissive fallback (`detail.get("expression") or detail.get("qosExpression")`,
similarly for size) is the cheapest fix. Onedata may eventually align
the swagger; until then, both names are accepted.

These count as paper §3 / §7 textual corrections — the writing agent's
handoff doc (`papers/ppam-2026/research/28-empirical-spec-corrections.md`)
should be updated to add these two to the existing 3 corrections.

## 16. QoS rule status doesn't settle even when data IS replicated

Discovered live 2026-05-01 P1 fixture: with two separate single-replica
QoS rules (one per provider), the data DOES replicate to both providers
within ~10 seconds, but one of the two rule statuses stays `pending`
indefinitely. Direct timeline observation on the live federation:

```
 t+ 5s  qos=pending  req_statuses=[pending, pending]   dist={SK:4096, PL:0}
 t+10s  qos=pending  req_statuses=[pending, fulfilled] dist={SK:4096, PL:4096}  ← data fully present
 t+15s  qos=pending  req_statuses=[pending, fulfilled] dist={SK:4096, PL:4096}
 t+20s  qos=pending  req_statuses=[pending, fulfilled] dist={SK:4096, PL:4096}
 t+25s  qos=pending  req_statuses=[pending, fulfilled] dist={SK:4096, PL:4096}
 t+30s  qos=pending  req_statuses=[pending, fulfilled] dist={SK:4096, PL:4096}
```

After 30s polling, one rule still reads `pending` while its data
requirement is fully satisfied. Implication: the convergence-wait
strategy in `fixture_runner._check_convergence` must inspect **actual
data placement** (via `get_file_distribution`), not just QoS rule
status. The two-axis OracleResult also needs a "data-presence" path
for `federation_pass`, otherwise scenarios that legitimately set up
multi-rule QoS will spuriously time out.

Fix planned for the immediate follow-up commit: convergence wait checks
"each fixture file's distributionPerProvider matches the expected
provider set" instead of "no rule pending".

This may also explain entry #17 (P4 pre-stage timeout): if the
pre-stage QoS rule we add to trigger a migration similarly stays
`pending` after the migration completes, our pre-stage poll loop never
exits because we wait for transfer-log entries that may have completed
and aged out before we noticed.

## 17. P4 pre-stage — fixed by switching to direct `POST /transfers`

Original strategy (temp `providerId=<id>` QoS rule whose side-effect
was a migration, with the runner polling for the resulting transfer
log entry) timed out reliably at 120s. Root cause: entry #16 — the
temp QoS rule itself stayed `pending` indefinitely, so the migration
was never reliably scheduled.

**Fix landed 2026-05-01 (commit `55669ac`)**: pre-stage uses
`POST /api/v3/oneprovider/transfers` directly with
`{type: "migration", replicatingProviderId, evictingProviderId, fileId}`.
Returns the transferId immediately. P4 pre-stage now completes in
~6s on the live federation (was 120s timeout).

Lesson: when a high-level Onedata abstraction (QoS rules) doesn't
behave as documented under load, look for the lower-level direct API
that bypasses the abstraction entirely. POST /transfers is the
canonical migration scheduler; the QoS-rule indirection was always
"side effect of higher-level intent" rather than "atomic primitive".

## 18. Snapshot-vs-re-query: the oracle race window

Discovered live 2026-05-01 D-band smoke. D1's oracle re-queries
`list_user_spaces` at oracle time to derive ground truth. The
federation has 27+ spaces visible to the admin user, including some
with duplicate names (`TestData` × 2, `StefansSpace` × 2 — Onedata
permits non-unique space names). Between the agent's
`list_user_spaces` call and the oracle's, the federation can churn
(entries appear/disappear, provider counts shift). The agent's
honest answer fails verification not because of agent error but
because ground truth has shifted under the oracle's feet.

**Partial fix this turn**: `RunContext` now carries a
`spaces_snapshot` field populated at fixture-prepare time. D1's
oracle reads from the snapshot, not from a fresh re-query.

**The deeper problem**: the synthetic smoke harness still
regenerates the agent's answer from a *new* `list_user_spaces` call,
which can disagree with the snapshot. In a real LLM-driven benchmark
this won't happen — the agent's actual `tools/list` response is
captured once and used both for the answer and for verification.
The smoke's residual D1 failure is a smoke-harness limitation, not
a substrate or oracle bug.

**Implication for the paper**: §4 oracle-design discussion should
acknowledge that ground truth needs to be *snapshotted at trial
boundaries*, not re-derived at oracle time, when the substrate is
visibly mutable. Not novel (τ-bench's ground truth is also
snapshotted) but worth surfacing as a federation-specific concern.

## 19. POST /transfers requires both `replicatingProviderId` AND `evictingProviderId` for `migration`

Discovered when implementing the entry #17 fix. Onedata's `migration`
transfer type semantically equals "replicate then evict"; the API
requires both the destination provider (`replicatingProviderId`) AND
the source provider (`evictingProviderId`) to be specified. The
swagger documents this but the inline cURL example only shows
`replication` (single provider), which can mislead.

The fixture runner picks the OTHER bound provider as the eviction
source — works on a 2-provider space; 3+ providers would need an
explicit choice from the test fixture.

## 20. Creating a directory under a regular file answers `400 posix eacces`, not `enotdir`

Entries 20-26 come from the first live runs against the project's own
validation federation (a local Compose federation of two Oneproviders,
Onedata 25.0, 2026-11-21), not from `data.spice-platform.eu`. The MCP-side
consequences are M-22 to M-25 in `research/empirical-mcp-server-findings.md`.

`create_directory` for `<dir>/seed.txt/sub`, where `seed.txt` is a
regular file, got `400` with the `posix` error and errno `eacces`. The
POSIX answer for "a path component is not a directory" is `enotdir`; the
tool had a branch for it (design/15, gate 1), and 25.0 did not take it
here. The swagger lists 400, 403, 404 and 500 for the endpoint with the
generic `Error` body and does not say which errno a creation under a file
gets. The probe left `create_parents` at its default (true), and the same
token had just created the probe directory and `seed.txt`.

Evidence: design/15, gate 1 (row "A regular file on the path") and gate 3
results; findings log M-22.

**For a client:** `eacces` on a creation is not proof of a permission
problem. Before reporting one, or saying that a retry will not help, read
the types of the path's components from the space down: a regular file
among them is the cause. `create_directory` does this on `eacces` only and
keeps the permission hint for the case where no component is a regular
file. It still maps `enotdir`, should another provider send it.

## 21. Listing a regular file's children returns the file itself

`GET /data/{id}/children` on a regular file does not fail. It returns a
normal listing whose only entry is the file itself. The swagger lists 400,
403, 404 and 500 for the endpoint, so a client written from it may expect
an error here; the tool's branch for `enotdir` on the listing is not
reached for a file.

Evidence: design/14, gate 1 (row "The parent is a regular file"); the live
test `test_listing_errors_and_an_empty_directory_as_the_model_reads_them`
in `test/live/test_files_live.py`; findings log M-23.

**For a client:** an error-free listing does not show that the target is a
directory, and a client that reads the answer as it comes reports "the
directory contains `<file>`". Check the target's type first, or compare the
single entry with the target: a directory is never its own child, so an
entry that is the target itself means a regular file. `list_children` does
this on the first page and answers "'…' is a regular file, not a
directory; it has no children."

## 22. A made-up page token answers `500 internalServerError`

`GET /data/{id}/children` with a made-up `token` (`not-a-token`), on a
directory that lists fine without one, answers `500` with the error id
`internalServerError`. The design had assumed a 400: the swagger lists 400
for the endpoint but does not say which failures get it. The status and id
are those of any server failure, so the response alone does not show that
the token is the cause. The live test shows it by a control: the same
directory lists, unchanged, without a token right before and right after.
The tool's code comment says a stale token behaves the same; the live test
pins only a made-up one.

Evidence: findings log M-24; commit `5e05990`; `test/live/test_files_live.py`
(`BAD_TOKEN_STATUS`, `BAD_TOKEN_ERROR_ID`). design/14 (gate 1, row "A bad
token"; gate 3; Round 2) records it now; it said 400 before the change that
added this entry.

**For a client:** a 5xx on a page read that carried a token may be the
token, not an outage. A token is good only as returned in the previous
page's `nextPageToken`, and there is no point retrying one that was not;
start the listing again without a token. `list_children` says this when it
gets a 5xx with a token.

## 23. A QoS requirement's record names its fields `expression` and `replicasNum`

`GET /qos_requirements/{qid}` returns `qosRequirementId`, `fileId`,
`expression` and `replicasNum`; the live test asserts all four and that
`qosExpression` is absent. Two other names circulated in the code and the
docs. The swagger's cURL example for the endpoint shows `qosExpression`
(entry #15), although its schema definition says `expression`, as the live
record does. And `replicas_num`, the key the MCP tools use, is not an
Onedata name: the count is `replicasNum` (also in the swagger). The
summary endpoint itself returns only ids and statuses (M-7), so the
expression and the count come from one `GET` per requirement. M-7's
enrichment copied a key named `replicas_num`, so on 25.0 a file's QoS
summary never carried the count its description promises; the first live
run of the QoS test found it.

Evidence: design/11, gate 3 results and "Stable tool fixed alongside";
findings log M-25.

**For a client:** read `expression` and `replicasNum` from the record. A
client written from the swagger's example reads `qosExpression` and finds
nothing, and one that reads `replicas_num` finds nothing either; neither
is an error, only an absent field. Check a live record before trusting an
example body.

## 24. A new QoS requirement shows in the file's summary almost at once

After `add_file_qos_requirement` (`anyStorage`, one replica) on a probe
file, the file's QoS summary listed the new requirement on its first read,
0.3 s after the add. After `remove_qos_requirement` it stopped listing it
on its first read, 0.1 s after the removal. The live test polls every 1 s
and both times are under one interval, so no wait was needed; each time
includes the read's own latency. One probe file, one provider; the load
on the federation was not recorded.

Evidence: design/11, gate 3 (item 1 and results); `test/live/test_qos_live.py`.

**For a client:** a requirement can be read back right after it is added,
and a removal shows at once, so a short poll is enough and the 60 s settle
window of the validation scenarios is ample. This is about the listing
only. Whether the requirement is `fulfilled` is a separate matter (entry
#16: a rule stayed `pending` after the data were there), and what another
provider sees depends on dbsync convergence (entry #12).

## 25. A 128 MiB replication between two local providers is whole about 2 s after scheduling

`schedule_file_replication` of a 128 MiB file, written through the
federation's other provider, to the local provider: the local provider's
block map covered the whole file 2.06 s after the schedule, about 62 MiB/s
counting the scheduling. Polled every 0.5 s, the file was seen partly copied
on two polls (from 0.82 s to 1.49 s, 30803309 bytes both times). The
transfer's status read `enqueued`, then `completed`. The first poll that
read `completed` was the one at which the blocks were whole (2.06 s), and
no poll read `completed` while the blocks, read right after it on the same
poll, left part of the file out. A 256 KiB file was whole 0.12 s after the
schedule and was never seen partly copied. One run, an unloaded
federation; timing under the panel's parallel load was not measured.

Evidence: design/12, gate 3 (results, and the decision to keep 128 MiB);
`test/live/test_transfer_live.py`.

**For a client:** the stretch in which the copy is partly done is about a
second at this size, so a check after scheduling finds the copy either
already whole or partway, and a model rarely sees "in transfer". The
tool's description warns that a transfer's status can read completed
before the copied blocks show at the target. That was not observed here,
but it is one unloaded run, so the test of "the data are there" stays the
target provider's block map covering the file, not the status (M-26: eight
trials answered from the status alone; each claim was true when made, but
unread). The idle figure is a lower bound for a loaded federation
(design/12), so do not derive a timeout from it.

## 26. A second removal, an unknown id and a file id all answer 404 with a structured not-found

`DELETE /qos_requirements/{qid}` was tried with three ids that name no
requirement: one just removed, one that never existed (32 random hex
digits) and a real file id. Each answered `404` with a structured
not-found body (`notFound` or `enoent`; the record does not say which).
`GET /qos_requirements/{qid}` for an unknown id also answers 404. The
swagger lists 400, 403, 404 and 500 for the delete with the generic body,
and the live test accepted a 400 or a 404; the 400 did not occur.

Evidence: design/11, gate 3 (the list of cases and results) and gate 1
(rows "Unknown id" and "A 404 that does not say the requirement is
missing"); `test/live/test_qos_live.py`.

**For a client:** the status and body do not tell the three cases apart,
and a file id is not rejected as a malformed id (400); it reads as
not-found. So a 404 on a removal means only that no requirement has this
id: it may have been removed already, or the value is not a requirement
id. The current ids are the keys of `requirements` in the file's QoS
summary. Only a 404 whose body is a structured not-found says the
requirement is missing; a bare or HTML 404 does not, and
`remove_qos_requirement` words it more carefully. A 403 was not exercised
(it needs a token without `space_manage_qos`), so what 25.0 answers there
is unverified.

## Maintenance

This file accretes as we discover more empirical behaviour. New entries
go at the bottom with a short heading + concise observation + how it
was discovered + paper / code cross-references. Avoid restating things
covered by the swagger; only record gaps between spec / docs and live
behaviour, or non-obvious operational know-how.
