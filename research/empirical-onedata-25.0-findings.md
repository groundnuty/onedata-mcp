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

## Maintenance

This file accretes as we discover more empirical behaviour. New entries
go at the bottom with a short heading + concise observation + how it
was discovered + paper / code cross-references. Avoid restating things
covered by the swagger; only record gaps between spec / docs and live
behaviour, or non-obvious operational know-how.
