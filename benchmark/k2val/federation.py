"""The panel's own REST client for the local Compose federation.

Fixtures and oracles talk to the OWNER provider (`nordic-marine-dc`, where the data are
written) and to the LOCAL provider (`vistula-dc`, the agent's site). Each call mirrors the
request shape of the existing helper it stands for (`onedata_mcp/api/files.py`, `qos.py`,
`transfers.py`; Onedata 25.0, oneprovider-swagger) and goes through `onedata_mcp.utils.request`,
but takes its config as an argument: the helpers read the process environment, which holds
only the local provider.

Everything is async; waiting goes through an injectable `Clock`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import httpx

from benchmark.k2val.clock import REAL_CLOCK, Clock
from benchmark.k2val.config import FederationConf, FederationSecrets
from onedata_mcp.utils import OnedataApiError, request

Role = Literal["owner", "local"]
POLL_S = 2.0
# what `FedApi.tree` asks of each entry (the file-tool scenarios, design/13-15)
TREE_ATTRIBUTES = ("fileId", "name", "type", "size", "posixPermissions")


class FixtureError(RuntimeError):
    """The federation did not reach a state the fixture needs. Counts as a deployment
    artefact (RESET_FAIL), never as a model failure."""


@dataclass(frozen=True)
class ProviderConfig:
    """What `onedata_mcp.utils.request` reads from a service config (duck-typed, so importing
    this module never imports `onedata_mcp.config`, which reads a `.env`). The headers hold the
    token: they stay out of the repr."""

    base_url: str
    auth_headers: dict[str, str] = field(repr=False)
    verify_ssl: bool = True
    service: str = "oneprovider"
    node_names: tuple[tuple[str, str], ...] = ()


def provider_config(host: str, token: str, *, insecure_tls: bool) -> ProviderConfig:
    return ProviderConfig(
        base_url=f"{host.rstrip('/')}/api/v3/oneprovider",
        auth_headers={
            "X-Auth-Token": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        verify_ssl=not insecure_tls,
    )


def covered_bytes(blocks: list[Any], size: int) -> int:
    """Bytes of [0, size) the `[offset, length]` blocks cover (their union)."""
    spans: list[tuple[int, int]] = []
    for b in blocks:
        if (
            isinstance(b, list | tuple)
            and len(b) == 2
            and all(isinstance(x, int) and not isinstance(x, bool) for x in b)
            and b[1] > 0
        ):
            start, end = max(b[0], 0), min(b[0] + b[1], size)
            if end > start:
                spans.append((start, end))
    total = 0
    reach = 0
    for start, end in sorted(spans):
        start = max(start, reach)
        if end > start:
            total += end - start
            reach = end
    return total


@dataclass
class DistView:
    """One provider's answer to `GET /data/{id}/distribution`, reduced to what the panel needs."""

    size: int = 0
    # bytes of [0, size) each provider holds, from the blocks it reports
    covered: dict[str, int] = field(default_factory=dict)
    failed: frozenset[str] = frozenset()
    # which size field the answer carried: PPAM saw `virtualSize` on 25.0, the swagger says
    # `logicalSize`. Recorded so a live run shows which one this federation answers.
    size_field: str = ""

    def covers(self, provider_id: str) -> bool:
        return self.size >= 0 and self.covered.get(provider_id, 0) >= self.size and self.size_known

    @property
    def size_known(self) -> bool:
        return bool(self.size_field)

    def bytes_of(self, provider_id: str) -> int:
        return self.covered.get(provider_id, 0)


def parse_dist(body: Any) -> DistView:
    per_provider = body.get("distributionPerProvider") if isinstance(body, dict) else None
    if not isinstance(per_provider, dict):
        return DistView()
    size = 0
    size_field = ""
    failed: set[str] = set()
    blocks_of: dict[str, list[Any]] = {}
    for pid, entry in per_provider.items():
        if not isinstance(entry, dict) or entry.get("success") is False:
            failed.add(pid)
            continue
        for key in ("logicalSize", "virtualSize"):
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                if value >= size:
                    size, size_field = value, key
                break
        blocks: list[Any] = []
        storages = entry.get("distributionPerStorageBackend")
        for storage in storages.values() if isinstance(storages, dict) else ():
            if isinstance(storage, dict) and storage.get("success") is not False:
                blocks.extend(storage.get("blocks") or [])
        blocks_of[pid] = blocks
    covered = {pid: covered_bytes(blocks, size) for pid, blocks in blocks_of.items()}
    return DistView(size, covered, frozenset(failed), size_field)


def _is_absent(e: OnedataApiError) -> bool:
    status = (e.response or {}).get("status_code")
    return e.errno == "enoent" or status == 404


class FedApi:
    def __init__(
        self,
        local: ProviderConfig,
        owner: ProviderConfig,
        *,
        clock: Clock = REAL_CLOCK,
        poll_s: float = POLL_S,
    ) -> None:
        self._cfg = {"local": local, "owner": owner}
        self.clock = clock
        self.poll_s = poll_s
        self._ids: dict[str, str] = {}

    @classmethod
    def from_conf(cls, fed: FederationConf, secrets: FederationSecrets, **kw: Any) -> FedApi:
        return cls(
            provider_config(
                fed.local.host, secrets.local_token.reveal(), insecure_tls=fed.allow_insecure_tls
            ),
            provider_config(
                fed.owner.host, secrets.owner_token.reveal(), insecure_tls=fed.allow_insecure_tls
            ),
            **kw,
        )

    # --- identity -------------------------------------------------------

    async def provider_id(self, role: Role) -> str:
        if role not in self._ids:
            r = await request(self._cfg[role], "GET", "/configuration")
            self._ids[role] = r["body"]["providerId"]
        return self._ids[role]

    # --- namespace ------------------------------------------------------

    async def lookup(self, role: Role, path: str) -> str | None:
        """File id of an absolute path (`POST /lookup-file-id/<path>`); None if absent."""
        normalized = path if path.startswith("/") else f"/{path}"
        try:
            r = await request(
                self._cfg[role], "POST", f"/lookup-file-id/{quote(normalized, safe='')}"
            )
        except OnedataApiError as e:
            if _is_absent(e):
                return None
            raise
        return r["body"]["fileId"]

    async def mkdir(self, role: Role, parent_id: str, name: str) -> str:
        r = await request(
            self._cfg[role],
            "POST",
            f"/data/{parent_id}/children",
            params={"name": name, "type": "DIR"},
        )
        return r["body"]["fileId"]

    async def create_reg(self, role: Role, parent_id: str, name: str) -> str:
        """`POST /data/{parent}/children?name=...&type=REG`, no body: an empty file."""
        r = await request(
            self._cfg[role],
            "POST",
            f"/data/{parent_id}/children",
            params={"name": name, "type": "REG"},
            additional_headers={"Content-Type": "application/octet-stream"},
        )
        return r["body"]["fileId"]

    async def put(self, role: Role, file_id: str, data: bytes, offset: int | None) -> None:
        """`PUT /data/{id}/content`. With `offset=None` Onedata truncates to 0 first; an
        explicit offset (0 included) keeps the rest of the file."""
        params = None if offset is None else {"offset": offset}
        await request(
            self._cfg[role],
            "PUT",
            f"/data/{file_id}/content",
            params=params,
            body=data,
            additional_headers={"Content-Type": "application/octet-stream"},
        )

    async def children(self, role: Role, dir_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            body: dict[str, Any] = {"attributes": ["fileId", "name", "type"], "limit": 1000}
            if token:
                body["token"] = token
            r = await request(self._cfg[role], "GET", f"/data/{dir_id}/children", json_body=body)
            out.extend(r["body"].get("children", []))
            token = r["body"].get("nextPageToken")
            if r["body"].get("isLast", True) or not token:
                return out

    # --- the file-tool scenarios (design/13-15) --------------------------

    async def set_mode(self, role: Role, file_id: str, mode: str) -> None:
        """`PUT /data/{id}` with `posixPermissions` (swagger `set_attr`), in octal (`"600"`)."""
        await request(
            self._cfg[role], "PUT", f"/data/{file_id}", json_body={"posixPermissions": mode}
        )

    async def content(self, role: Role, file_id: str) -> bytes:
        """`GET /data/{id}/content`: a small file's bytes (CDo's end-state check)."""
        cfg = self._cfg[role]
        headers = {**cfg.auth_headers, "Accept": "*/*"}
        headers.pop("Content-Type", None)
        async with httpx.AsyncClient(
            base_url=cfg.base_url, headers=headers, verify=cfg.verify_ssl
        ) as client:
            response = await client.get(f"/data/{file_id}/content")
        if response.is_error:
            raise OnedataApiError(
                f"GET /data/<id>/content failed (status={response.status_code})",
                response={"status_code": response.status_code, "body": None},
            )
        return response.content

    async def _listing(self, role: Role, dir_id: str) -> list[dict[str, Any]]:
        """Every entry of a directory, page by page. Strict: an answer without a `children`
        list raises, so an unreadable listing is never taken for an empty directory."""
        out: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            body: dict[str, Any] = {"attributes": list(TREE_ATTRIBUTES), "limit": 1000}
            if token:
                body["token"] = token
            r = await request(self._cfg[role], "GET", f"/data/{dir_id}/children", json_body=body)
            page = r["body"]
            if not isinstance(page, dict) or not isinstance(page.get("children"), list):
                raise OnedataApiError(f"listing of {dir_id} has no 'children' list", response=r)
            out.extend(page["children"])
            token = page.get("nextPageToken")
            if page.get("isLast") is True:
                return out
            if not token:
                raise OnedataApiError(f"listing of {dir_id}: not last, but no token", response=r)

    async def tree(self, role: Role, dir_id: str, *, max_depth: int = 4) -> dict[str, dict]:
        """A directory's tree as `role` shows it: relative path -> {fileId, type, size, mode},
        down to `max_depth` levels. Raises if any listing fails (never an empty tree)."""
        out: dict[str, dict] = {}
        stack: list[tuple[str, str, int]] = [("", dir_id, 1)]
        while stack:
            prefix, current, depth = stack.pop()
            for entry in await self._listing(role, current):
                name = entry.get("name")
                if not isinstance(name, str) or not name:
                    raise OnedataApiError(f"listing of {current}: an entry has no name")
                path = f"{prefix}{name}"
                out[path] = {
                    "fileId": entry.get("fileId"),
                    "type": entry.get("type"),
                    "size": entry.get("size"),
                    "mode": entry.get("posixPermissions"),
                }
                if entry.get("type") == "DIR" and depth < max_depth:
                    stack.append((f"{path}/", entry.get("fileId"), depth + 1))
        return out

    async def attrs(self, role: Role, file_id: str, attributes: list[str]) -> dict[str, Any] | None:
        try:
            r = await request(
                self._cfg[role], "GET", f"/data/{file_id}", json_body={"attributes": attributes}
            )
        except OnedataApiError as e:
            if _is_absent(e):
                return None
            raise
        return r["body"]

    async def delete(self, role: Role, file_id: str) -> None:
        try:
            await request(self._cfg[role], "DELETE", f"/data/{file_id}")
        except OnedataApiError as e:
            if not _is_absent(e):
                raise

    async def distribution(self, role: Role, file_id: str) -> DistView:
        r = await request(self._cfg[role], "GET", f"/data/{file_id}/distribution")
        return parse_dist(r["body"])

    # --- QoS and transfers ---------------------------------------------

    async def qos_ids(
        self, role: Role, file_id: str, *, missing_ok: bool = True
    ) -> tuple[str, ...]:
        """The requirement ids of a file's EFFECTIVE QoS summary (its own and those inherited
        from its ancestors). `missing_ok=False` (the snapshots): a 404 or an answer without a
        `requirements` map raises, so an unreadable summary is never read as "none"."""
        try:
            r = await request(self._cfg[role], "GET", f"/data/{file_id}/qos/summary")
        except OnedataApiError as e:
            if missing_ok and _is_absent(e):
                return ()
            raise
        body = r["body"]
        requirements = body.get("requirements") if isinstance(body, dict) else None
        if not missing_ok and not isinstance(requirements, dict):
            raise OnedataApiError("QoS summary without a 'requirements' map", response=r)
        return tuple(sorted((requirements or {}).keys()))

    async def qos_file_id(self, role: Role, qos_id: str) -> str | None:
        """The file a requirement is set on (its own `fileId`, not a file that inherits it);
        None if the requirement is gone."""
        try:
            r = await request(self._cfg[role], "GET", f"/qos_requirements/{quote(qos_id, safe='')}")
        except OnedataApiError as e:
            if _is_absent(e):
                return None
            raise
        file_id = r["body"].get("fileId") if isinstance(r["body"], dict) else None
        return file_id if isinstance(file_id, str) else None

    async def add_qos(self, role: Role, file_id: str, expression: str) -> str:
        r = await request(
            self._cfg[role],
            "POST",
            "/qos_requirements",
            json_body={"expression": expression, "replicasNum": 1, "fileId": file_id},
        )
        return r["body"]["qosRequirementId"]

    async def qos_status(self, role: Role, qos_id: str) -> str:
        r = await request(self._cfg[role], "GET", f"/qos_requirements/{qos_id}")
        return r["body"].get("status", "")

    async def delete_qos(self, role: Role, qos_id: str) -> None:
        try:
            await request(self._cfg[role], "DELETE", f"/qos_requirements/{qos_id}")
        except OnedataApiError as e:
            if not _is_absent(e):
                raise

    async def replicate(self, role: Role, file_id: str, target_provider_id: str) -> str:
        r = await request(
            self._cfg[role],
            "POST",
            "/transfers",
            json_body={
                "type": "replication",
                "dataSourceType": "file",
                "fileId": file_id,
                "replicatingProviderId": target_provider_id,
            },
        )
        return r["body"]["transferId"]

    # --- waiting --------------------------------------------------------

    async def wait_until(
        self, check: Callable[[], Awaitable[bool]], *, timeout_s: float, what: str
    ) -> None:
        """Poll `check` until it is true; FixtureError(what) after `timeout_s`."""
        deadline = self.clock.time() + timeout_s
        while True:
            if await check():
                return
            if self.clock.time() >= deadline:
                raise FixtureError(f"timed out after {timeout_s:.0f}s waiting for: {what}")
            await self.clock.sleep(self.poll_s)
