"""Idempotently create one Onedata space per panel LLM.

Background: per `research/empirical-mcp-server-findings.md` M-2, the
benchmark harness's per-trial federation reset is subtree-scoped, so
running multiple LLMs concurrently against the same scenario subtree
would corrupt each other's writes. To unlock LLM-level parallelism,
each LLM gets its own dedicated space. Within an LLM's space, scenarios
still use the existing disjoint subtrees by design.

Why a separate script (not a one-shot in run_panel):
- Space creation requires Onezone admin privilege.
- Provider-side support requires SSH access to the provider nodes (not
  doable from this machine programmatically).
- Spaces are persistent — once created, they live across runs.

What this script does:
1. Reads the panel definition from `benchmark/panel.py`.
2. For each LLM in the panel, computes a sanitised space name like
   `ppam_2026_mcp_tests_<llm>`.
3. Lists the current Onezone spaces via the user's token.
4. For any panel LLM whose space doesn't exist:
   a. POST /api/v3/onezone/spaces with the new name (admin endpoint).
   b. POST /api/v3/onezone/spaces/<sid>/providers/token to generate a
      space-support token for each currently-supporting provider.
   c. Print the token + the manual provider-side command the operator
      must run on the provider node (we can't do this leg from here).
5. Reports a summary of what was created vs reused.

Usage:
    uv run python -m benchmark.setup_per_llm_spaces

The script is safe to re-run: existing spaces are detected by name and
reused. Only missing spaces trigger creation + support-token output.

For a future panel addition, add the LLM to `benchmark/panel.py` and
re-run this script.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

# Imports below must come AFTER load_dotenv (config reads env eagerly).
import httpx  # noqa: E402

from benchmark.panel import build_panel  # noqa: E402

ONEZONE_HOST = os.environ["ONEDATA_ONEZONE_HOST"].rstrip("/")
ONEZONE_TOKEN = os.environ["ONEDATA_ONEZONE_TOKEN"]
ALLOW_INSECURE = os.environ.get("ONEDATA_ALLOW_INSECURE_TLS", "").lower() in (
    "1",
    "true",
    "yes",
)


def _sanitise(llm_name: str) -> str:
    """Convert an LLM name into a space-name-friendly suffix.

    Onedata space names accept letters/digits/'.'/'-'/'_' but for
    cleanliness we collapse non-alphanumeric to '_'.
    """
    safe = re.sub(r"[^a-zA-Z0-9]+", "_", llm_name).strip("_")
    return safe


def _space_name_for(llm_name: str) -> str:
    """Canonical space-name convention for per-LLM spaces."""
    return f"ppam_2026_mcp_tests_{_sanitise(llm_name)}"


async def _onezone_get(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.get(
        f"{ONEZONE_HOST}/api/v3/onezone{path}",
        headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
    )
    r.raise_for_status()
    return r.json()


async def _onezone_post(
    client: httpx.AsyncClient,
    path: str,
    *,
    json_body: dict | None = None,
) -> httpx.Response:
    r = await client.post(
        f"{ONEZONE_HOST}/api/v3/onezone{path}",
        headers={
            "X-Auth-Token": ONEZONE_TOKEN,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json=json_body or {},
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Onezone POST {path} failed (status={r.status_code}): {r.text[:300]}")
    return r


async def list_existing_spaces(client: httpx.AsyncClient) -> dict[str, str]:
    """Return {space_name: space_id} for spaces the current user has access to."""
    body = await _onezone_get(client, "/user/spaces")
    space_ids = body.get("spaces", [])
    out: dict[str, str] = {}
    for sid in space_ids:
        detail = await _onezone_get(client, f"/spaces/{sid}")
        name = detail.get("name")
        if isinstance(name, str):
            out[name] = sid
    return out


async def create_space(client: httpx.AsyncClient, name: str) -> str:
    """Create a new Onezone space with the given name. Returns the new spaceId.

    Uses the admin endpoint POST /spaces — requires `oz_spaces_create` priv
    on the calling token. The user-scoped equivalent
    POST /user/spaces also works for non-admin tokens.
    """
    # Try admin endpoint first; fall back to user endpoint on 403.
    for path in ("/spaces", "/user/spaces"):
        try:
            r = await _onezone_post(client, path, json_body={"name": name})
            location = r.headers.get("Location") or r.headers.get("location")
            if location:
                # Location: .../spaces/<sid>
                return location.rstrip("/").rsplit("/", 1)[-1]
            body = r.json() if r.content else {}
            sid = body.get("spaceId") or body.get("id")
            if isinstance(sid, str):
                return sid
            raise RuntimeError(f"create_space succeeded but no spaceId in response: {r.text[:200]}")
        except RuntimeError as e:
            if "403" in str(e) and path == "/spaces":
                continue  # try /user/spaces
            raise
    raise RuntimeError("create_space: both admin and user endpoints rejected the request")


async def create_support_token(client: httpx.AsyncClient, space_id: str) -> str:
    """Generate a space-support token. Provider admins use this to attach
    storage support to the space.
    """
    r = await _onezone_post(client, f"/spaces/{space_id}/providers/token")
    body = r.json() if r.content else {}
    token = body.get("token")
    if not isinstance(token, str):
        raise RuntimeError(f"create_support_token: no token in response: {r.text[:200]}")
    return token


async def get_current_user_id(client: httpx.AsyncClient) -> str:
    """Return the userId of the user the calling token represents."""
    body = await _onezone_get(client, "/user")
    uid = body.get("userId")
    if not isinstance(uid, str):
        raise RuntimeError(f"get_current_user_id: unexpected response: {body!r}")
    return uid


async def add_user_to_space(
    client: httpx.AsyncClient, space_id: str, user_id: str
) -> None:
    """PUT the user as a member of the space (admin endpoint).

    Spaces created via the admin POST /spaces endpoint do NOT auto-add
    the calling user as a member, which means the user can't see the
    space in /user/spaces and (more importantly) can't access files
    inside it. This call adds membership with default privileges.
    Idempotent: HTTP 201 on first add, 204 on already-member.
    """
    r = await client.put(
        f"{ONEZONE_HOST}/api/v3/onezone/spaces/{space_id}/users/{user_id}",
        headers={
            "X-Auth-Token": ONEZONE_TOKEN,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json={},
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"add_user_to_space failed (HTTP {r.status_code}): {r.text[:300]}"
        )


async def main() -> int:
    panel, skipped = build_panel()
    if skipped:
        print("[panel] note: some legs are skipped (creds-related):")
        for r in skipped:
            print(f"  - {r}")
    if not panel:
        print("[panel] empty — nothing to do.", file=sys.stderr)
        return 2

    print(f"[setup] Onezone: {ONEZONE_HOST}")
    print(f"[setup] panel size: {len(panel)}")
    for entry in panel:
        target = _space_name_for(entry.name)
        print(f"  - {entry.name}  →  space `{target}`")
    print()

    verify = not ALLOW_INSECURE
    async with httpx.AsyncClient(verify=verify, timeout=30.0) as client:
        existing = await list_existing_spaces(client)
        user_id = await get_current_user_id(client)
        print(f"[setup] {len(existing)} existing space(s) visible to the current user")
        print(f"[setup] current user id: {user_id}")

        created: list[tuple[str, str, str]] = []  # (llm, space_name, space_id)
        reused: list[tuple[str, str, str]] = []
        support_tokens: list[tuple[str, str]] = []  # (space_name, token)

        for entry in panel:
            target = _space_name_for(entry.name)
            sid = existing.get(target)
            if sid:
                reused.append((entry.name, target, sid))
                continue
            print(f"[setup] creating space `{target}` for LLM `{entry.name}`...")
            new_sid = await create_space(client, target)
            created.append((entry.name, target, new_sid))
            # Admin POST /spaces does NOT auto-add the user as member.
            # Without membership, the user can't access files inside the
            # space — even with admin token. PUT them in explicitly.
            try:
                await add_user_to_space(client, new_sid, user_id)
                print(f"  added user as space member ({user_id[:12]}...)")
            except RuntimeError as e:
                print(f"  WARN: could not add user to space: {e}")
            try:
                token = await create_support_token(client, new_sid)
                support_tokens.append((target, token))
            except RuntimeError as e:
                print(f"  WARN: could not generate support token: {e}")

        # Summary
        print()
        print("[setup] ====== summary ======")
        for llm, name, sid in reused:
            print(f"  REUSED   {llm:24s}  {name}  ({sid})")
        for llm, name, sid in created:
            print(f"  CREATED  {llm:24s}  {name}  ({sid})")

        if support_tokens:
            print()
            print("[setup] ====== ACTION REQUIRED ON PROVIDER NODES ======")
            print(
                "Each new space needs storage support from at least one provider. "
                "The current SPICE federation has cloud-pl + Cloud-SK as the "
                "active providers backing the existing `ppam_2026_mcp_tests` "
                "space. Run the support command on EACH provider node "
                "(via SSH or onepanel UI) for EACH new space.\n"
            )
            print("Per-space support tokens (use within the next ~24h before they expire):\n")
            for name, token in support_tokens:
                print(f"  Space: {name}")
                print(f"    Token: {token}")
                print()
            curl_body = '{"token":"<TOKEN>","size":104857600,"storageId":"<STORAGE_ID>"}'
            print(
                "On each provider's onepanel:\n"
                "  https://<provider-host>/onepanel  →  Spaces  →  Support space\n"
                "    paste token, set size (e.g. 100 MiB = 104857600 bytes),\n"
                "    select an existing storage backend, submit.\n\n"
                "Or via the onepanel REST CLI (admin SSH on the provider):\n"
                "  curl -u admin:<onepanel-pw> -X POST \\\n"
                "    -H 'content-type: application/json' \\\n"
                f"    -d '{curl_body}' \\\n"
                "    https://<provider-host>:9443/api/v3/onepanel/provider/spaces\n"
            )

        if not created:
            print("[setup] All panel-LLM spaces already exist. No action needed.")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
