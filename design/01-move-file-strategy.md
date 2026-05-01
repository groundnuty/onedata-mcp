# Design decision: `move_file` strategy

**Status:** open. Decision deferred to first live-federation smoke pass.
**Tool:** `move_file(src_file_id_or_path, dst_path)` in `onedata_mcp/api/files.py`.
**Currently:** `NotImplementedError` with a structured message the agent can surface.

## Context

Onedata 25.0 has **no public REST endpoint for move/rename**. Verified against `oneprovider-swagger@25.0` (commit `39da981`):

```
$ grep -RliE "rename|relocate|move" paths/ definitions/
# only matches are *remove* operationIds — no move endpoint exists
```

The original GitLab Onedata MCP server (`gitlab.spice-platform.eu/work-packages/wp6/onedata-mcp`) wraps `OnedataFileRESTClient.move()`, which posts to a private undocumented endpoint. The PPAM 2026 paper §7 *Threats to Validity, Infrastructure* already calls this out:

> "The `rename_file` tool wraps Onedata's private `move` endpoint, which has no public REST contract. We mitigate by pinning the Onedata image SHA in the federation snapshot artefact and running an `mcp_smoke.py` reachability check against every tool before each daily benchmark batch."

So the paper assumes the private endpoint works. The question is whether to commit to the private hack or step around it.

## Options

| | Pros | Cons |
|---|---|---|
| **(a) Port the private-endpoint POST** from `OnedataFileRESTClient.move()` source | atomic; matches the paper's claim; smallest change | breaks if Onedata rotates the private endpoint shape; no public contract to anchor against |
| **(b) Non-atomic `download → create_at_destination → delete_source`** | uses only public endpoints; survives upstream private-API churn | loses atomicity — partial failure leaves the source consumed without a destination, or both deleted; not faithful to the paper's tool description; may run afoul of read-size limits for large files |
| **(c) Wait for upstream public endpoint** | future-proof | no ETA; blocks benchmark; not viable for PPAM camera-ready |

## Decision

**Defer to first live smoke pass.**

Rationale: we can't characterise the failure modes of (a) without trying it on the live federation. If the private endpoint behaves stably under our token, (a) wins (paper-faithful, atomic). If it doesn't — the smoke fails or the response shape has drifted — we fall back to (b) and update the paper §7 prose to acknowledge non-atomicity.

## Open questions

- The token scope minted on azure-interway: does it carry the privileges the private `move` endpoint requires? Some private endpoints check op-worker-internal permissions not exposed to user tokens.
- For cross-space moves: does the private endpoint actually support that, or only intra-space? The paper's `rename_file` tool definition allows `(src_space, dst_space)` differing.
- Is there a Onedata internal Erlang RPC we could surface instead, behind a thin REST proxy in the MCP server itself? (Likely overkill, but worth knowing it exists.)

## Cross-references

- Paper spec: `papers/ppam-2026/research/22-mcp-implementation-spec.md` §3.9.1
- Paper draft: `papers/ppam-2026/paper.tex` §7 *Threats to Validity, Infrastructure*
- Implementation notes: `IMPLEMENTATION_NOTES.md` §"Move file: known gap"
