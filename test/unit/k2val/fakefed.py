"""A stateful fake of the local Compose federation (Onedata 25.0 shapes) for the panel's tests.

One zone, two providers (owner and local) behind pytest-httpx, answering by host. Files hold
blocks per provider; a write through a provider puts blocks at that provider; a replication
(a transfer, or a QoS rule `providerId=<local>`) copies the owner's blocks to the local
provider after `replication_ticks` calls of `tick()`. `FakeClock.sleep` ticks, so every wait
in the panel's code moves the fake world on without real time.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from urllib.parse import unquote

import httpx

OWNER_HOST = "https://owner.k2val.datahub.test"
LOCAL_HOST = "https://local.k2val.datahub.test"
ZONE_HOST = "https://zone.k2val.datahub.test"
OWNER_ID = "pOwner"
LOCAL_ID = "pLocal"
API = "/api/v3/oneprovider"


def error(status: int, errno: str = "enoent") -> httpx.Response:
    return httpx.Response(
        status, json={"error": {"id": "posix", "details": {"errno": errno}, "description": errno}}
    )


@dataclass
class Node:
    fid: str
    name: str
    parent: str | None
    kind: str  # REG or DIR
    size: int = 0
    blocks: dict[str, list[list[int]]] = field(default_factory=dict)
    local_lag: int = 0  # lookups at the local provider that still fail
    # the file-tool scenarios (design/13-15)
    mode: str = "664"
    data: bytes = b""


class FakeClock:
    def __init__(self, fed: FakeFed | None = None) -> None:
        self.now = 1_000_000.0
        self.fed = fed
        self.sleeps = 0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.fed:
            self.fed.tick()
        await asyncio.sleep(0)


class FakeFed:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.roots: dict[str, str] = {}  # space name -> root node id
        self.rules: dict[str, dict] = {}
        self.transfers: dict[str, dict] = {}
        self.puts: list[dict] = []  # every PUT content: file, provider, offset (None), length
        self.requests: list[tuple[str, str, str]] = []  # (method, host, path)
        self.size_field = "logicalSize"
        self.replication_ticks = 1
        self.local_lag = 0  # lag given to every new node at the local provider
        self.unexpected: list[str] = []
        self.fail_owner_distribution = False
        self.offset_with_token = 0  # listings that sent both (25.0 rejects them)
        # the whole federation is stopped: every request is refused, as a stopped container does
        self.down = False
        self._seq = 0

    # --- helpers --------------------------------------------------------

    def new_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}{self._seq:06d}"

    def add_space(self, name: str) -> str:
        rid = self.new_id("root")
        self.nodes[rid] = Node(rid, name, None, "DIR")
        self.roots[name] = rid
        return rid

    def path_of(self, node: Node) -> str:
        parts = []
        cur: Node | None = node
        while cur is not None:
            parts.append(cur.name)
            cur = self.nodes.get(cur.parent) if cur.parent else None
        return "/" + "/".join(reversed(parts))

    def find(self, path: str) -> Node | None:
        for n in self.nodes.values():
            if self.path_of(n) == path:
                return n
        return None

    def kids(self, fid: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.parent == fid]

    def by_path(self, path: str) -> Node:
        node = self.find(path)
        assert node is not None, path
        return node

    @staticmethod
    def covered(blocks: list[list[int]], size: int) -> int:
        reach = total = 0
        for off, length in sorted(blocks):
            start, end = max(off, reach), min(off + length, size)
            if end > start:
                total += end - start
                reach = end
        return total

    def tick(self) -> None:
        """Advance pending replications and rules by one step."""
        for obj in [*self.transfers.values(), *self.rules.values()]:
            if obj["status"] in ("completed", "fulfilled"):
                continue
            obj["ticks"] += 1
            if obj["ticks"] >= self.replication_ticks:
                node = self.nodes.get(obj["fileId"])
                is_transfer = obj in self.transfers.values()
                # A transfer copies to its target. A copy to the owner, which already holds the
                # data, changes nothing; rules here are always `providerId=<local>`.
                target = obj.get("replicatingProviderId", LOCAL_ID) if is_transfer else LOCAL_ID
                if node is not None and target == LOCAL_ID:
                    node.blocks[LOCAL_ID] = [list(b) for b in node.blocks.get(OWNER_ID, [])]
                obj["status"] = "completed" if is_transfer else "fulfilled"

    # --- routing --------------------------------------------------------

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = f"{request.url.scheme}://{request.url.host}"
        self.requests.append((request.method, host, request.url.path))
        if self.down:
            raise httpx.ConnectError("connection refused")
        if host == ZONE_HOST and (request.method, request.url.path) == (
            "GET",
            "/api/v3/onezone/configuration",
        ):
            return httpx.Response(200, json={"name": "zone"})
        provider = {OWNER_HOST: OWNER_ID, LOCAL_HOST: LOCAL_ID}.get(host)
        path = request.url.path
        if provider is None or not path.startswith(API):
            self.unexpected.append(f"{request.method} {request.url}")
            return error(404)
        rest = path[len(API) :]
        body = (
            json.loads(request.content)
            if request.headers.get("content-type", "").startswith("application/json")
            and request.content
            else {}
        )
        q = dict(request.url.params)
        m = request.method
        await asyncio.sleep(0)
        # the local provider does not know a new file yet: by id, as by path, it answers enoent
        if provider == LOCAL_ID and (fid := self._file_id_of(rest, body)) in self.nodes:
            node = self.nodes[fid]
            if node.local_lag > 0:
                node.local_lag -= 1
                return error(400)

        if m == "GET" and rest == "/configuration":
            return httpx.Response(200, json={"providerId": provider, "name": provider})
        if m == "GET" and rest == "/spaces":
            # get_file_distribution enriches provider names from the user's spaces: none here
            return httpx.Response(200, json=[])
        if m == "POST" and rest.startswith("/lookup-file-id/"):
            return self._lookup(provider, unquote(rest[len("/lookup-file-id/") :]))
        raw_rest = request.url.raw_path.decode().split("?", 1)[0][len(API) :]
        if m == "PUT" and (mm := re.fullmatch(r"/data/([^/]+)/path/(.+)", raw_rest)):
            return self._create_at_path(provider, unquote(mm[1]), mm[2], q, request.content)
        if mm := re.fullmatch(r"/data/([^/]+)/children", rest):
            if m == "POST":
                return self._create(mm[1], q, request.content)
            return self._children(mm[1], body)
        if mm := re.fullmatch(r"/data/([^/]+)/content", rest):
            if m == "GET":
                return self._content(mm[1])
            return self._put(provider, mm[1], q, request.content)
        if m == "GET" and (mm := re.fullmatch(r"/data/([^/]+)/files", rest)):
            return self._files(mm[1], body)
        if mm := re.fullmatch(r"/data/([^/]+)/distribution", rest):
            return self._distribution(provider, mm[1])
        if mm := re.fullmatch(r"/data/([^/]+)/qos/summary", rest):
            return self._qos_summary(mm[1])
        if mm := re.fullmatch(r"/data/([^/]+)", rest):
            if m == "DELETE":
                return self._delete(mm[1])
            if m == "PUT":
                return self._set_attrs(mm[1], body)
            return self._attrs(mm[1], body)
        if m == "POST" and rest == "/qos_requirements":
            return self._qos_post(body)
        if mm := re.fullmatch(r"/qos_requirements/([^/]+)", rest):
            return self._qos_one(m, mm[1])
        if m == "POST" and rest == "/transfers":
            return self._transfer_post(body)
        if mm := re.fullmatch(r"/transfers/([^/]+)", rest):
            return self._transfer_get(mm[1])
        if m == "GET" and (mm := re.fullmatch(r"/spaces/([^/]+)", rest)) and mm[1] in self.roots:
            return self._space(mm[1])
        self.unexpected.append(f"{m} {request.url}")
        return error(404)

    @staticmethod
    def _file_id_of(rest: str, body: dict) -> str | None:
        """The file id a request is about (in the path or the body), if any."""
        if mm := re.match(r"/data/([^/]+)", rest):
            return mm[1]
        if rest in ("/qos_requirements", "/transfers"):
            return body.get("fileId")
        return None

    # --- handlers -------------------------------------------------------

    def _lookup(self, provider: str, path: str) -> httpx.Response:
        node = self.find(path)
        if node is None:
            return error(400)
        if provider == LOCAL_ID and node.local_lag > 0:
            node.local_lag -= 1
            return error(400)
        return httpx.Response(200, json={"fileId": node.fid})

    def _create(self, parent: str, q: dict, data: bytes = b"") -> httpx.Response:
        if parent not in self.nodes:
            return error(400)
        if any(k.name == q["name"] for k in self.kids(parent)):
            return error(400, "eexist")
        kind = q.get("type", "REG")
        fid = self.new_id("dir" if kind == "DIR" else "file")
        self.nodes[fid] = Node(fid, q["name"], parent, kind, local_lag=self.local_lag)
        if kind == "REG" and data:
            self.nodes[fid].data = data
            self.nodes[fid].size = len(data)
        return httpx.Response(201, json={"fileId": fid})

    def _children(self, fid: str, body: dict) -> httpx.Response:
        """One page, in name order, as 25.0 documents it: `limit`, then `token` (from the
        previous page) or `offset`, never both. A regular file lists itself as its only entry,
        not an error (measured on 25.0)."""
        node = self.nodes.get(fid)
        if node is None:
            return error(400)
        if node.kind != "DIR":
            attributes = body.get("attributes") or ["fileId", "name", "type"]
            return httpx.Response(
                200,
                json={
                    "children": [{a: self._attr(node, a) for a in attributes}],
                    "isLast": True,
                    "nextPageToken": None,
                },
            )
        if "token" in body and "offset" in body:
            self.offset_with_token += 1
            return httpx.Response(
                400, json={"error": {"id": "badValue", "details": {"key": "offset"}}}
            )
        kids = sorted(self.kids(fid), key=lambda k: k.name)
        start = (
            int(body["token"].removeprefix("tok-")) if "token" in body else body.get("offset", 0)
        )
        limit = body.get("limit", 1000)
        page = kids[start : start + limit]
        attributes = body.get("attributes") or ["fileId", "name", "type"]
        children = [{a: self._attr(k, a) for a in attributes} for k in page]
        last = start + limit >= len(kids)
        token = None if last else f"tok-{start + limit}"
        return httpx.Response(
            200, json={"children": children, "isLast": last, "nextPageToken": token}
        )

    def _put(self, provider: str, fid: str, q: dict, data: bytes) -> httpx.Response:
        node = self.nodes.get(fid)
        if node is None or node.kind != "REG":
            return error(400)
        offset = int(q["offset"]) if "offset" in q else None
        self.puts.append(
            {
                "file": fid,
                "name": node.name,
                "provider": provider,
                "offset": offset,
                "length": len(data),
            }
        )
        if offset is None:  # no offset: truncate to 0, then write
            node.size = len(data)
            node.data = data
            node.blocks = {provider: [[0, len(data)]] if data else []}
        elif not data:  # empty write at an offset: sets the size, writes no block
            node.size = max(node.size, offset)
        else:
            node.size = max(node.size, offset + len(data))
            node.blocks.setdefault(provider, []).append([offset, len(data)])
        return httpx.Response(204)

    def _distribution(self, provider: str, fid: str) -> httpx.Response:
        node = self.nodes.get(fid)
        if node is None:
            return error(400)
        if self.fail_owner_distribution and provider == OWNER_ID:
            return error(500, "internalServerError")
        per = {}
        for pid in (OWNER_ID, LOCAL_ID):
            blocks = node.blocks.get(pid, [])
            per[pid] = {
                "success": True,
                self.size_field: node.size,
                "distributionPerStorageBackend": {
                    f"st-{pid}": {
                        "success": True,
                        "blocks": blocks,
                        "physicalSize": sum(b[1] for b in blocks),
                    }
                },
            }
        return httpx.Response(200, json={"type": "REG", "distributionPerProvider": per})

    def _attr(self, node: Node, name: str):
        full = {
            "fileId": node.fid,
            "name": node.name,
            "type": node.kind,
            "size": node.size if node.kind == "REG" else None,
            "path": self.path_of(node),
            "posixPermissions": node.mode,
            "atime": 1_700_000_000,
            "mtime": 1_700_000_000,
            "isFullyReplicatedLocally": self.covered(node.blocks.get(LOCAL_ID, []), node.size)
            >= node.size,
        }
        return full.get(name)

    def _attrs(self, fid: str, body: dict) -> httpx.Response:
        node = self.nodes.get(fid)
        if node is None:
            return error(400)
        names = body.get("attributes") or ["fileId", "name", "type", "size", "path"]
        return httpx.Response(200, json={a: self._attr(node, a) for a in names})

    # --- the file-tool scenarios (design/13-15) ---------------------------

    def _files(self, fid: str, body: dict) -> httpx.Response:
        """`GET /data/{id}/files`: every regular file under the directory, at any depth, with
        its path relative to it (one page; no directories, as on 25.0)."""
        root = self.nodes.get(fid)
        if root is None or root.kind != "DIR":
            return error(400)
        prefix = self.path_of(root) + "/"
        found = [
            n for n in self.nodes.values() if n.kind == "REG" and self.path_of(n).startswith(prefix)
        ]
        attributes = body.get("attributes") or ["path", "name", "type", "size"]
        files = []
        for node in sorted(found, key=self.path_of):
            entry = {a: self._attr(node, a) for a in attributes}
            entry["path"] = self.path_of(node).removeprefix(prefix)
            files.append(entry)
        return httpx.Response(200, json={"files": files, "isLast": True, "nextPageToken": None})

    def _set_attrs(self, fid: str, body: dict) -> httpx.Response:
        node = self.nodes.get(fid)
        if node is None:
            return error(400)
        if set(body) != {"posixPermissions"}:
            return httpx.Response(400, json={"error": {"id": "badValue"}})
        node.mode = body["posixPermissions"]
        return httpx.Response(204)

    def _content(self, fid: str) -> httpx.Response:
        node = self.nodes.get(fid)
        if node is None or node.kind != "REG":
            return error(400)
        return httpx.Response(
            200, content=node.data, headers={"Content-Type": "application/octet-stream"}
        )

    def _create_at_path(
        self, provider: str, base: str, raw: str, q: dict, data: bytes
    ) -> httpx.Response:
        """`PUT /data/{base}/path/{rel}?type=..&create_parents=..` (swagger
        create_file_at_path): parents made when asked, eexist on any existing last segment,
        eacces when a parent on the path is a regular file (measured on 25.0, which does not
        send enotdir)."""
        if base not in self.nodes:
            return error(400)
        names = [unquote(part) for part in raw.split("/")]
        if any(n in ("", ".", "..") for n in names):
            return httpx.Response(
                400, json={"error": {"id": "badValue", "details": {"key": "path"}}}
            )
        current = base
        for name in names[:-1]:
            found = next((k for k in self.kids(current) if k.name == name), None)
            if found is None:
                if q.get("create_parents") != "true":
                    return error(400, "enoent")
                found = Node(self.new_id("dir"), name, current, "DIR")
                self.nodes[found.fid] = found
            elif found.kind != "DIR":
                return error(400, "eacces")
            current = found.fid
        if any(k.name == names[-1] for k in self.kids(current)):
            return error(400, "eexist")
        kind = q.get("type", "REG")
        fid = self.new_id("dir" if kind == "DIR" else "file")
        node = Node(fid, names[-1], current, kind, local_lag=self.local_lag)
        if kind == "REG":
            node.data = data
            node.size = len(data)
            node.blocks = {provider: [[0, len(data)]] if data else []}
        self.nodes[fid] = node
        return httpx.Response(201, json={"fileId": fid})

    def _delete(self, fid: str) -> httpx.Response:
        if fid not in self.nodes:
            return error(400)
        stack = [fid]
        while stack:
            cur = stack.pop()
            stack.extend(k.fid for k in self.kids(cur))
            self.nodes.pop(cur, None)
        return httpx.Response(204)

    def lineage(self, fid: str) -> set[str]:
        """The node and its ancestors, up to the space root."""
        out: set[str] = set()
        cur = self.nodes.get(fid)
        while cur is not None and cur.fid not in out:
            out.add(cur.fid)
            cur = self.nodes.get(cur.parent) if cur.parent else None
        return out

    def _qos_summary(self, fid: str) -> httpx.Response:
        if fid not in self.nodes:
            return error(400)
        # effective, as Onedata's: the node's own rules and those inherited from its ancestors
        lineage = self.lineage(fid)
        reqs = {q: r["status"] for q, r in self.rules.items() if r["fileId"] in lineage}
        return httpx.Response(200, json={"requirements": reqs, "status": "fulfilled"})

    def _qos_post(self, body: dict) -> httpx.Response:
        qid = self.new_id("q")
        self.rules[qid] = {**body, "status": "pending", "ticks": 0}
        return httpx.Response(201, json={"qosRequirementId": qid})

    def _qos_one(self, method: str, qid: str) -> httpx.Response:
        rule = self.rules.get(qid)
        if rule is None:
            return error(404, "enoent")
        if method == "DELETE":
            del self.rules[qid]
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "qosRequirementId": qid,
                "fileId": rule["fileId"],
                "expression": rule["expression"],  # 25.0's name, not the swagger's
                "replicasNum": rule["replicasNum"],
                "status": rule["status"],
            },
        )

    # Provider names, as the space's providers list them (`GET /spaces/{id}`); here a space's
    # id is its name.
    PROVIDER_NAMES = {LOCAL_ID: "vistula-dc", OWNER_ID: "nordic-marine-dc"}

    def space_of(self, fid: str) -> str | None:
        root = next((n for n in map(self.nodes.get, self.lineage(fid)) if n and not n.parent), None)
        return root.name if root is not None else None

    def _space(self, space: str) -> httpx.Response:
        providers = [{"providerId": p, "providerName": n} for p, n in self.PROVIDER_NAMES.items()]
        return httpx.Response(200, json={"spaceId": space, "name": space, "providers": providers})

    def _transfer_post(self, body: dict) -> httpx.Response:
        """As 25.0 is expected to answer (unverified; the live test prints the real bodies): an
        unknown file is a 404, a target outside the space's providers a 400 naming the space."""
        if body.get("fileId") not in self.nodes:
            return error(404)
        target = body.get("replicatingProviderId")
        if target not in self.PROVIDER_NAMES:
            details = {"spaceId": self.space_of(body["fileId"]), "providerId": target}
            return httpx.Response(
                400,
                json={
                    "error": {
                        "id": "spaceNotSupportedBy",
                        "details": details,
                        "description": f"Space is not supported by provider {target}.",
                    }
                },
            )
        tid = self.new_id("t")
        self.transfers[tid] = {**body, "status": "pending", "ticks": 0}
        return httpx.Response(201, json={"transferId": tid})

    def _transfer_get(self, tid: str) -> httpx.Response:
        t = self.transfers.get(tid)
        if t is None:
            return error(404)
        return httpx.Response(
            200,
            json={
                "fileId": t["fileId"],
                "replicationStatus": t["status"],
                "replicatingProviderId": t.get("replicatingProviderId"),
            },
        )
