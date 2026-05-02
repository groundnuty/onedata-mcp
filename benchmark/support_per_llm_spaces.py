"""Idempotently attach storage support on both providers for each per-LLM space.

Reads the per-LLM space registry from `benchmark/_per_llm_spaces.py`,
checks the current Oneprovider support status for each space, and for
any (space, provider) pair that isn't already supported:

1. Generates a fresh space-support token via the Onezone REST.
2. POSTs to the provider's onepanel `support_space` endpoint with
   the token + size + storageId.
3. Verifies the support succeeded.

Storage backend selection: each provider exposes multiple storages.
We pick the `posix-local` storage by name (the canonical real-data
backend on the SPICE deployment); the `NetworkMonitoringStorage`
nulldevice is excluded because it's special-purpose.

Default support size: 100 MiB per provider per space (104857600 bytes).
The peak fixture footprint across all 18 scenarios is ~15 KiB; the
100 MiB cap is gross overkill chosen for safety.

Usage:
    uv run python -m benchmark.support_per_llm_spaces
    uv run python -m benchmark.support_per_llm_spaces --size-mib 50
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(REPO_ROOT / ".env")

from benchmark._per_llm_spaces import PER_LLM_SPACE, PER_LLM_SPACE_ID  # noqa: E402

ONEZONE_HOST = os.environ["ONEDATA_ONEZONE_HOST"].rstrip("/")
ONEZONE_TOKEN = os.environ["ONEDATA_ONEZONE_TOKEN"]
ALLOW_INSECURE = os.environ.get("ONEDATA_ALLOW_INSECURE_TLS", "").lower() in (
    "1",
    "true",
    "yes",
)

# Active SPICE providers (the two backing the existing
# `ppam_2026_mcp_tests` space). The other 3 declared providers are
# offline per task #24.
PROVIDERS = (
    ("cloud-pl", "https://cloud-pl.data.spice-platform.eu"),
    ("Cloud-SK", "https://cloud-sk.data.spice-platform.eu"),
)

# Storage backend name we want for support on each provider. Both
# providers happen to expose a backend with this exact name.
STORAGE_NAME = "posix-local"


# ---------------------------------------------------------------------------
# Onezone helpers
# ---------------------------------------------------------------------------


async def list_existing_spaces(client: httpx.AsyncClient) -> dict[str, str]:
    """{space_name: space_id} for spaces visible to current Onezone user."""
    r = await client.get(
        f"{ONEZONE_HOST}/api/v3/onezone/user/spaces",
        headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
    )
    r.raise_for_status()
    space_ids = r.json().get("spaces", [])
    out: dict[str, str] = {}
    for sid in space_ids:
        rr = await client.get(
            f"{ONEZONE_HOST}/api/v3/onezone/spaces/{sid}",
            headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
        )
        rr.raise_for_status()
        name = rr.json().get("name")
        if isinstance(name, str):
            out[name] = sid
    return out


async def list_space_supporting_providers(client: httpx.AsyncClient, space_id: str) -> set[str]:
    """Return the set of providerIds currently supporting `space_id`."""
    r = await client.get(
        f"{ONEZONE_HOST}/api/v3/onezone/spaces/{space_id}",
        headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
    )
    r.raise_for_status()
    providers = r.json().get("providers") or {}
    return set(providers) if isinstance(providers, dict) else set()


async def create_support_token(client: httpx.AsyncClient, space_id: str) -> str:
    """Mint a fresh space-support token. Single-use: one POST to the
    provider's support endpoint consumes it.
    """
    r = await client.post(
        f"{ONEZONE_HOST}/api/v3/onezone/spaces/{space_id}/providers/token",
        headers={
            "X-Auth-Token": ONEZONE_TOKEN,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json={},
    )
    r.raise_for_status()
    body = r.json()
    token = body.get("token")
    if not isinstance(token, str):
        raise RuntimeError(f"create_support_token: unexpected response: {body!r}")
    return token


# ---------------------------------------------------------------------------
# Onepanel helpers (per provider)
# ---------------------------------------------------------------------------


async def list_provider_storages(client: httpx.AsyncClient, provider_base: str) -> list[dict]:
    """Return [{id, name, type, ...}] for each storage backend on the
    provider. Used to find the `posix-local` storageId for support.
    """
    r = await client.get(
        f"{provider_base}/api/v3/onepanel/provider/storages",
        headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
    )
    r.raise_for_status()
    ids = r.json().get("ids", [])
    out: list[dict] = []
    for sid in ids:
        rr = await client.get(
            f"{provider_base}/api/v3/onepanel/provider/storages/{sid}",
            headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
        )
        rr.raise_for_status()
        d = rr.json()
        d["id"] = sid
        out.append(d)
    return out


async def list_provider_supported_spaces(client: httpx.AsyncClient, provider_base: str) -> set[str]:
    """Return the set of spaceIds supported by this provider."""
    r = await client.get(
        f"{provider_base}/api/v3/onepanel/provider/spaces",
        headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
    )
    r.raise_for_status()
    return set(r.json().get("ids") or [])


async def get_provider_id(client: httpx.AsyncClient, provider_base: str) -> str:
    """Return the providerId of this provider (for joining with onezone-side
    space-support relationships)."""
    r = await client.get(
        f"{provider_base}/api/v3/onepanel/provider",
        headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
    )
    r.raise_for_status()
    pid = r.json().get("id")
    if not isinstance(pid, str):
        raise RuntimeError(f"get_provider_id: unexpected response: {r.json()!r}")
    return pid


async def support_space_on_provider(
    client: httpx.AsyncClient,
    provider_base: str,
    *,
    token: str,
    size_bytes: int,
    storage_id: str,
) -> str:
    """POST to /api/v3/onepanel/provider/spaces. Returns the new local
    space-support id."""
    r = await client.post(
        f"{provider_base}/api/v3/onepanel/provider/spaces",
        headers={
            "X-Auth-Token": ONEZONE_TOKEN,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json={"token": token, "size": size_bytes, "storageId": storage_id},
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"support_space failed at {provider_base} (HTTP {r.status_code}): {r.text[:300]}"
        )
    body = r.json() if r.content else {}
    return body.get("id", "")


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> int:
    size_bytes = args.size_mib * 1024 * 1024
    print(
        f"[support] target support size per (space, provider): "
        f"{args.size_mib} MiB ({size_bytes} bytes)"
    )
    print(f"[support] storage backend name: {STORAGE_NAME!r}")
    print(f"[support] panel LLMs needing support: {sorted(PER_LLM_SPACE)}")
    print()

    verify = not ALLOW_INSECURE
    async with httpx.AsyncClient(verify=verify, timeout=30.0) as client:
        # Resolve names → spaceIds. The spaces were created via the admin
        # `/spaces` endpoint which does NOT add the calling user as a
        # member, so they don't appear in `/user/spaces`. Trust the
        # registry's recorded spaceId; verify each exists by direct GET.
        target: dict[str, str] = {}  # llm_name → spaceId
        for llm_name, space_name in PER_LLM_SPACE.items():
            sid = PER_LLM_SPACE_ID.get(llm_name)
            if not sid:
                print(
                    f"[support] WARN: no spaceId in registry for "
                    f"{llm_name!r} — run setup_per_llm_spaces first.",
                    file=sys.stderr,
                )
                continue
            r = await client.get(
                f"{ONEZONE_HOST}/api/v3/onezone/spaces/{sid}",
                headers={"X-Auth-Token": ONEZONE_TOKEN, "accept": "application/json"},
            )
            if r.status_code != 200:
                print(
                    f"[support] WARN: registered space {space_name!r} "
                    f"({sid}) not reachable (HTTP {r.status_code}). Skipping.",
                    file=sys.stderr,
                )
                continue
            target[llm_name] = sid

        # Per-provider preparation: discover providerId + storageId
        provider_state: dict[str, dict] = {}
        for pname, pbase in PROVIDERS:
            pid = await get_provider_id(client, pbase)
            storages = await list_provider_storages(client, pbase)
            storage = next((s for s in storages if s.get("name") == STORAGE_NAME), None)
            if not storage:
                print(
                    f"[support] WARN: provider {pname!r} has no storage named "
                    f"{STORAGE_NAME!r}. Available: "
                    f"{[s.get('name') for s in storages]}",
                    file=sys.stderr,
                )
                continue
            already = await list_provider_supported_spaces(client, pbase)
            provider_state[pname] = {
                "base": pbase,
                "providerId": pid,
                "storageId": storage["id"],
                "already_supported": already,
            }
            print(
                f"[support] {pname:10s}  providerId={pid}  "
                f"storageId={storage['id']}  already-supported={len(already)} space(s)"
            )
        print()

        # Drive the attach loop
        attached: list[tuple[str, str, str]] = []  # (llm, provider, space_id)
        skipped: list[tuple[str, str, str]] = []
        errors: list[tuple[str, str, str]] = []

        for llm_name, space_id in target.items():
            for pname, pstate in provider_state.items():
                if space_id in pstate["already_supported"]:
                    skipped.append((llm_name, pname, space_id))
                    print(f"[support] {llm_name:24s} on {pname:10s}  ALREADY supported")
                    continue
                try:
                    token = await create_support_token(client, space_id)
                    new_local = await support_space_on_provider(
                        client,
                        pstate["base"],
                        token=token,
                        size_bytes=size_bytes,
                        storage_id=pstate["storageId"],
                    )
                    attached.append((llm_name, pname, space_id))
                    print(
                        f"[support] {llm_name:24s} on {pname:10s}  ATTACHED  (local-id={new_local})"
                    )
                except Exception as e:  # noqa: BLE001
                    errors.append((llm_name, pname, str(e)[:200]))
                    print(f"[support] {llm_name:24s} on {pname:10s}  ERROR: {e}")

        # Verification pass
        print()
        print("[support] ====== verification ======")
        for llm_name, space_id in target.items():
            providers_now = await list_space_supporting_providers(client, space_id)
            need = {pstate["providerId"] for pstate in provider_state.values()}
            ok = need.issubset(providers_now)
            mark = "OK" if ok else "INCOMPLETE"
            print(
                f"  {llm_name:24s}  {mark:11s}  supported by {len(providers_now)}/{len(need)} "
                f"target provider(s)  ({sorted(providers_now)[:2]}...)"
            )

        print()
        print(
            f"[support] summary: attached={len(attached)} skipped={len(skipped)} "
            f"errors={len(errors)}"
        )
        return 0 if not errors else 1


def cli() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--size-mib",
        type=int,
        default=100,
        help="Support size per (space, provider) in MiB (default: 100).",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main(args)))


if __name__ == "__main__":
    cli()
