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

import asyncio
import re
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
# `posixPermissions`: "POSIX file permissions as string expressing octal notation" (swagger;
# a shared file in public mode shows the last digit only)
_MODE_RE = re.compile(r"[0-7]{1,4}")
# A listing of more pages than this is malformed (at 1000 entries a page, no trial comes near).
MAX_LISTING_PAGES = 20
# One probe request of `FedApi.ping`: a federation that does not answer in this long is down.
PING_TIMEOUT_S = 15.0


class FixtureError(RuntimeError):
    """The federation did not reach a state the fixture needs. Counts as a deployment
    artefact (RESET_FAIL), never as a model failure."""


class FederationUnreachable(RuntimeError):
    """A service of the federation did not answer (a refused or timed out connection, or a
    5xx). The message names the service and the kind, never a host."""


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


def zone_config(host: str, token: str, *, insecure_tls: bool) -> ProviderConfig:
    """The Onezone, for `FedApi.ping` only (the panel's fixtures and oracles never use it)."""
    return ProviderConfig(
        base_url=f"{host.rstrip('/')}/api/v3/onezone",
        auth_headers={
            "X-Auth-Token": token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        verify_ssl=not insecure_tls,
        service="onezone",
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
    """One provider's answer to `GET /data/{id}/distribution`, reduced to what the panel needs.

    Each provider entry is read on its own: its own size (`sizes`), the size field it carried,
    and its blocks as validated intervals. A provider whose entry failed, has no size, has an
    empty or missing storage map, one storage failed, or an interval that is not
    `[offset >= 0, length > 0]` inside its size is in `failed`: its bytes are unknown, never 0.
    """

    sizes: dict[str, int] = field(default_factory=dict)
    size_fields: dict[str, str] = field(default_factory=dict)
    # validated, merged `(offset, length)` intervals of each readable provider
    intervals: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    failed: frozenset[str] = frozenset()

    def readable(self, provider_id: str) -> bool:
        return provider_id in self.intervals and provider_id not in self.failed

    def size_of(self, provider_id: str) -> int:
        return self.sizes.get(provider_id, 0)

    @property
    def size(self) -> int:
        """The largest size any readable entry gives (fixture waits on the owner's own view
        use `size_of`)."""
        return max((self.sizes[p] for p in self.intervals), default=0)

    @property
    def size_known(self) -> bool:
        return any(p in self.size_fields for p in self.intervals)

    @property
    def size_field(self) -> str:
        return next((self.size_fields[p] for p in self.intervals if p in self.size_fields), "")

    @property
    def covered(self) -> dict[str, int]:
        return {p: self.bytes_of(p) for p in self.intervals}

    def covers(self, provider_id: str) -> bool:
        """The provider's own blocks cover its own size, and that size is above 0 (every file
        the panel judges has data: "0 of 0 bytes" is a reading to distrust, not a copy)."""
        size = self.size_of(provider_id)
        return self.readable(provider_id) and size > 0 and self.bytes_of(provider_id) >= size

    def bytes_of(self, provider_id: str) -> int:
        return sum(n for _, n in self.intervals.get(provider_id, ()))

    def observed(self) -> dict[str, dict]:
        """The validated source observations, kept in a snapshot for offline rescoring."""
        out: dict[str, dict] = {
            p: {
                "size": self.sizes.get(p),
                "size_field": self.size_fields.get(p, ""),
                "intervals": [list(b) for b in self.intervals[p]],
            }
            for p in self.intervals
        }
        for p in self.failed:
            out[p] = {"failed": True}
        return out


def _interval(block: Any) -> tuple[int, int] | None:
    """`[offset, length]`: two non-negative ints (not bools), length above 0."""
    if not isinstance(block, list | tuple) or len(block) != 2:
        return None
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in block):
        return None
    return (block[0], block[1]) if block[1] > 0 else None


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for start, length in sorted(intervals):
        end = start + length
        if out and start <= out[-1][1]:
            out[-1][1] = max(out[-1][1], end)
        else:
            out.append([start, end])
    return [(a, b - a) for a, b in out]


def parse_dist(body: Any) -> DistView:
    per_provider = body.get("distributionPerProvider") if isinstance(body, dict) else None
    if not isinstance(per_provider, dict):
        return DistView()
    view = DistView()
    failed: set[str] = set()
    for pid, entry in per_provider.items():
        if not isinstance(entry, dict) or entry.get("success") is False:
            failed.add(pid)
            continue
        for key in ("logicalSize", "virtualSize"):
            value = entry.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                view.sizes[pid], view.size_fields[pid] = value, key
                break
        storages = entry.get("distributionPerStorageBackend")
        if pid not in view.sizes or not isinstance(storages, dict) or not storages:
            failed.add(pid)
            continue
        intervals: list[tuple[int, int]] = []
        for storage in storages.values():
            raw = storage.get("blocks") if isinstance(storage, dict) else None
            if not isinstance(storage, dict) or storage.get("success") is False:
                failed.add(pid)
                break
            parsed = [_interval(b) for b in raw] if isinstance(raw, list) else [None]
            if any(b is None or b[0] + b[1] > view.sizes[pid] for b in parsed):
                failed.add(pid)
                break
            intervals.extend(b for b in parsed if b is not None)
        if pid not in failed:
            view.intervals[pid] = _merge(intervals)
    view.failed = frozenset(failed)
    return view


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
        zone: ProviderConfig | None = None,
    ) -> None:
        self._cfg = {"local": local, "owner": owner}
        self._zone = zone  # only `ping` reads it
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
            zone=zone_config(
                fed.onezone_host,
                secrets.onezone_token.reveal(),
                insecure_tls=fed.allow_insecure_tls,
            ),
            **kw,
        )

    # --- reachability ---------------------------------------------------

    async def ping(self, *, timeout_s: float = PING_TIMEOUT_S) -> None:
        """Does the federation answer? One cheap request (`GET /configuration`) to the local
        provider, the owner provider and the zone (when one is configured), none of it cached
        (`provider_id` is). Raises FederationUnreachable for the first that gives no answer in
        `timeout_s` or answers with a 5xx; any other answer, a 4xx included, is an answer: the
        service is up, and whatever is wrong with the request is a trial's own matter."""
        targets = [("local provider", self._cfg["local"]), ("owner provider", self._cfg["owner"])]
        if self._zone is not None:
            targets.append(("zone", self._zone))
        for name, cfg in targets:
            try:
                await asyncio.wait_for(request(cfg, "GET", "/configuration"), timeout_s)
            except TimeoutError:
                raise FederationUnreachable(f"{name}: timeout") from None
            except OnedataApiError as e:
                status = (e.response or {}).get("status_code")
                if status is None or status >= 500:
                    raise FederationUnreachable(f"{name}: {e.kind or f'http_{status}'}") from None

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
        """Every entry of a directory (sweep and teardown): the same bounded, strict listing as
        the snapshots' (`_listing`), so a repeated token cannot hang a cleanup."""
        return await self._listing(role, dir_id, ("fileId", "name", "type"))

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
        # 200 only: a redirect (3xx, not followed) or a partial answer is not the file's bytes
        if response.status_code != 200:
            raise OnedataApiError(
                f"GET /data/<id>/content answered status {response.status_code}, not 200",
                response={"status_code": response.status_code, "body": None},
            )
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) != len(response.content):
            raise OnedataApiError(
                f"GET /data/<id>/content: {len(response.content)} bytes, {declared} declared",
                response={"status_code": response.status_code, "body": None},
            )
        return response.content

    async def _page(
        self,
        role: Role,
        dir_id: str,
        limit: int,
        token: str | None,
        attributes: tuple[str, ...] = TREE_ATTRIBUTES,
    ) -> tuple[list[dict[str, Any]], bool, str | None, dict]:
        body: dict[str, Any] = {"attributes": list(attributes), "limit": limit}
        if token:
            body["token"] = token
        r = await request(self._cfg[role], "GET", f"/data/{dir_id}/children", json_body=body)
        page = r["body"]
        if not isinstance(page, dict) or not isinstance(page.get("children"), list):
            raise OnedataApiError(f"listing of {dir_id} has no 'children' list", response=r)
        next_token = page.get("nextPageToken")
        return page["children"], page.get("isLast") is True, next_token or None, r

    async def _listing(
        self, role: Role, dir_id: str, attributes: tuple[str, ...] = TREE_ATTRIBUTES
    ) -> list[dict[str, Any]]:
        """Every entry of a directory, page by page. Strict: an answer without a `children`
        list raises, so an unreadable listing is never taken for an empty directory; so do a
        non-final page without a token, a token seen before (a loop), and more than
        MAX_LISTING_PAGES pages (a fixture or snapshot error, never a hang)."""
        out: list[dict[str, Any]] = []
        token: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_LISTING_PAGES):
            children, last, token, r = await self._page(role, dir_id, 1000, token, attributes)
            out.extend(children)
            if last:
                return out
            if not token:
                raise OnedataApiError(f"listing of {dir_id}: not last, but no token", response=r)
            if token in seen:
                raise OnedataApiError(f"listing of {dir_id}: a page token repeats", response=r)
            seen.add(token)
        raise OnedataApiError(f"listing of {dir_id}: more than {MAX_LISTING_PAGES} pages")

    async def first_page_names(self, role: Role, dir_id: str, limit: int) -> tuple[list[str], bool]:
        """The names on a directory's first page at `limit`, and whether it is the last page
        (LCp's check that its answer needs a second page)."""
        children, last, _, _ = await self._page(role, dir_id, limit, None)
        return [c.get("name") for c in children if isinstance(c, dict)], last

    async def tree(self, role: Role, dir_id: str, *, max_depth: int = 4) -> dict[str, dict]:
        """A directory's tree as `role` shows it: relative path -> {fileId, type, size, mode},
        down to `max_depth` levels. Raises if any listing fails (never an empty tree)."""
        out: dict[str, dict] = {}
        stack: list[tuple[str, str, int]] = [("", dir_id, 1)]
        while stack:
            prefix, current, depth = stack.pop()
            for entry in await self._listing(role, current):
                name, fid, kind = entry.get("name"), entry.get("fileId"), entry.get("type")
                if not isinstance(name, str) or not name or "/" in name:
                    raise OnedataApiError(f"listing of {current}: an entry has no usable name")
                if not isinstance(fid, str) or not fid or kind not in ("REG", "DIR", "SYMLNK"):
                    raise OnedataApiError(f"listing of {current}: {name!r} has no id or type")
                size = entry.get("size")
                if kind == "REG" and (not isinstance(size, int) or isinstance(size, bool)):
                    raise OnedataApiError(f"listing of {current}: file {name!r} has no size")
                if kind == "REG" and size < 0:
                    raise OnedataApiError(f"listing of {current}: file {name!r} has size {size}")
                mode = entry.get("posixPermissions")
                if kind == "REG" and not (isinstance(mode, str) and _MODE_RE.fullmatch(mode)):
                    # a file's mode is compared with the start state: none is not "unchanged"
                    raise OnedataApiError(f"listing of {current}: file {name!r} has no mode")
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
