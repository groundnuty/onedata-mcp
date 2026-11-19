# Design decision: `move_file` strategy

**Status:** **decided 2026-05-01 — option (a): CDMI move, intra-space only.**
**Tool:** `move_file(src_path, dst_path)` in `onedata_mcp/api/files.py`.
**Implemented at:** `onedata_mcp/api/files.py::move_file` (mocked unit tests pass; live smoke pending user write-gate).

## Context

Onedata 25.0 has **no public REST endpoint for move/rename**. Verified against `oneprovider-swagger@25.0` (commit `39da981`):

```
$ grep -RliE "rename|relocate|move" paths/ definitions/
# only matches are *remove* operationIds — no move endpoint exists
```

The original GitLab Onedata MCP server (`gitlab.spice-platform.eu/work-packages/wp6/onedata-mcp`) wraps `OnedataFileRESTClient.move()`. Tracking that down (`onedatafilerestclient` submodule of `onedatarestfsspec`, commit `6887661`, `onedata_file_rest_client.py:527-555`), it turns out **the "private endpoint" is actually a CDMI (Cloud Data Management Interface) move** — a documented standard, just not in Onedata's `/api/v3/oneprovider/` swagger:

```
PUT https://{oneprovider}/cdmi/{dst_space}/{dst_path}
Headers: X-Auth-Token, X-CDMI-Specification-Version: 1.1.1,
         Content-Type: application/cdmi-object
Body:    {"move": "<src_space>/<src_path>"}
```

The Python client raises an explicit error if `src_space != dst_space` — **CDMI move is intra-space only**. The PPAM paper §7 *Threats to Validity, Infrastructure* already flags the absence of a public swagger contract:

> "The `rename_file` tool wraps Onedata's private `move` endpoint, which has no public REST contract. We mitigate by pinning the Onedata image SHA in the federation snapshot artefact and running an `mcp_smoke.py` reachability check against every tool before each daily benchmark batch."

The §7 caveat stands — CDMI is documented but the *integration* with Onedata's auth/space namespacing is not in the swagger.

## Options considered

| | Pros | Cons |
|---|---|---|
| **(a) CDMI PUT — same shape the official Python client uses** | atomic; matches paper's claim; one HTTP call; same auth (X-Auth-Token) as the rest of the surface | not in `/api/v3/oneprovider/` swagger; intra-space only |
| **(b) Non-atomic `download → create_at_destination → delete_source`** | uses only `/api/v3/oneprovider/` endpoints; survives any future CDMI churn; supports cross-space | loses atomicity (partial failure → orphan); read-size limits (the base `download_file` caps at 5 MB); 3× HTTP cost |
| **(c) Wait for upstream public endpoint** | future-proof | no ETA; blocks benchmark |

## Decision: option (a)

CDMI is a standard, the upstream Python client has been using it for years, the auth is identical to the rest of our surface, and option (b) introduces atomicity loss that would ripple into the paper's threats-to-validity discussion *more* than CDMI's "not-in-the-public-swagger" status does. We absorb the small risk that the CDMI integration shape changes between Onedata releases as part of the paper's existing §7 infrastructure caveat.

## Constraint surfaced to the agent

The MCP tool's input schema documents intra-space only. Cross-space moves raise a `ValueError` with a message directing the agent to compose `download_file` + `create_file` + `delete_file` (option (b)) — accepting the loss of atomicity for that specific case. None of the 18 PPAM scenarios attempts a cross-space move, so option (a) covers the headline.

## Implementation

`onedata_mcp/api/files.py::move_file` performs:

1. Parse src and dst paths into `(space, inner_path)` tuples; reject if either isn't `/<space>/<path>` form.
2. Reject cross-space moves with a clear `ValueError`.
3. Compute the CDMI URL by stripping `/api/v3/oneprovider` from the configured base URL and appending `/cdmi/{dst_space}/{dst_inner}` (URL-encoded).
4. PUT with X-Auth-Token + CDMI headers + JSON body `{"move": "<src_space>/<src_inner>"}`.
5. On non-2xx, raise `OnedataApiError` carrying the response status + body.
6. On success, call `get_file_id(dst_path)` to return the moved entity's fileId.

Five unit tests cover happy path, nested/encoded paths, cross-space rejection, malformed input, and server error surfacing — see `test/unit/api/test_files.py::test_move_file_*`.

## Live verification (pending)

The CDMI move has not been smoked against the live federation yet. That's a write op against `ppam_2026_mcp_tests`; gated until user clears the `--write` smoke phase. When cleared, the smoke will:

1. Create `_smoke/<UTC>/before.txt` in the benchmark space
2. `move_file('/ppam_2026_mcp_tests/_smoke/<UTC>/before.txt', '/ppam_2026_mcp_tests/_smoke/<UTC>/after.txt')`
3. Verify the destination exists and the source doesn't (via `get_file_attributes`)
4. Cleanup both via `delete_file`

If CDMI fails at step 2 — fall back to option (b) and revisit this doc.

## Cross-references

- Paper spec: `papers/ppam-2026/research/22-mcp-implementation-spec.md` §3.9.1
- Paper draft: `papers/ppam-2026/paper.tex` §7 *Threats to Validity, Infrastructure*
- Implementation notes: `IMPLEMENTATION_NOTES.md` §"Move file: CDMI implementation"
- Source snippet: `OnedataFileRESTClient.move()` in `onedatafilerestclient/onedata_file_rest_client.py:527-555` (commit `6887661`)
