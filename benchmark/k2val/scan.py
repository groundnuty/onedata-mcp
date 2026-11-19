"""`k2val scan`: the pre-flight scan for the CLI legs (wave 2).

The CLI agents send everything they see to a vendor. Before one may run, the scan collects what
it will see, through the same server it will use:

- the tool definitions, and the prompts (system prompt and every brief for the config's space);
- sample tool answers on a fixture: `list_space_providers`, `get_file_distribution`,
  `check_readiness`;
- the provider ids, provider names, space name and fixture paths.

and checks each against a token list file (one token per line, matched case-insensitively as a
substring) and an optional regex patterns file. It prints and records only match COUNTS per
category, never the matched text. A CLI leg refuses to start unless the latest scan for its
config is clean and current (`latest_scan_is_clean`).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from benchmark.k2val.config import (
    ConfigError,
    K2valConfig,
    ModelConf,
    load_config,
    read_federation_secrets,
)
from benchmark.k2val.scenarios import SCENARIOS, SYSTEM_PROMPT_TEMPLATE
from benchmark.k2val.secrets import SecretFileError, say

SCAN_VERSION = 1


class ToolCaller(Protocol):
    async def list_definitions(self) -> str: ...

    async def call(self, name: str, arguments: dict[str, Any]) -> str: ...


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def read_lines(path: str) -> list[str]:
    if not path:
        return []
    return [
        line.strip()
        for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def compile_patterns(lines: list[str]) -> list[re.Pattern[str]]:
    out = []
    for n, line in enumerate(lines, start=1):
        try:
            out.append(re.compile(line))
        except re.error:
            # the line number only: the pattern itself is not echoed
            raise ConfigError(f"patterns file: line {n} is not a valid regex") from None
    return out


@dataclass
class ScanResult:
    categories: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(c["tokens"] + c["patterns"] for c in self.categories.values())


def scan_texts(
    sources: dict[str, str], tokens: list[str], patterns: list[re.Pattern[str]]
) -> ScanResult:
    """Counts of token and pattern matches per category. Returns no matched text."""
    result = ScanResult()
    lowered_tokens = [t.lower() for t in tokens]
    for category, text in sources.items():
        low = text.lower()
        result.categories[category] = {
            "tokens": sum(low.count(t) for t in lowered_tokens),
            "patterns": sum(len(p.findall(text)) for p in patterns),
        }
    return result


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def _digest(path: str) -> str:
    if not path:
        return ""
    try:
        return hashlib.sha256(Path(path).expanduser().read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def fingerprint(cfg: K2valConfig, conf: ModelConf) -> str:
    """What a scan is valid for: change any of it and the record is stale."""
    basis = {
        "scan_version": SCAN_VERSION,
        "model": [conf.name, conf.backend, conf.api_id, conf.space, conf.binary],
        "tools": [list(cfg.tools_k1), list(cfg.tools_k2)],
        "providers": [cfg.federation.local.name, cfg.federation.owner.name],
        "briefs": {k: s.brief for k, s in SCENARIOS.items()},
        "tokens_sha": _digest(cfg.scan.tokens_file),
        "patterns_sha": _digest(cfg.scan.patterns_file),
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def scans_dir(root: Path, config_name: str) -> Path:
    return root / "scans" / config_name


def write_scan_record(root: Path, run_dir: Path | None, record: dict) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(record["created_at"]))
    path = scans_dir(root, record["config"]) / f"{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, indent=2, sort_keys=True)
    path.write_text(text, encoding="utf-8")
    if run_dir is not None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"scan-{record['config']}.json").write_text(text, encoding="utf-8")
    return path


def latest_scan_is_clean(cfg: K2valConfig, conf: ModelConf, root: Path) -> tuple[bool, str]:
    files = sorted(scans_dir(root, conf.name).glob("*.json"))
    if not files:
        return False, "no scan record for this config (run `make k2val-scan`)"
    record = json.loads(files[-1].read_text(encoding="utf-8"))
    if record.get("fingerprint") != fingerprint(cfg, conf):
        return False, "the latest scan is stale: its inputs changed (run `make k2val-scan`)"
    if not record.get("clean"):
        return False, f"the latest scan is not clean ({record.get('total_matches')} matches)"
    return True, "clean"


def build_record(
    cfg: K2valConfig, conf: ModelConf, result: ScanResult, n_tokens: int, n_patterns: int
) -> dict:
    return {
        "config": conf.name,
        "created_at": time.time(),
        "scan_version": SCAN_VERSION,
        "fingerprint": fingerprint(cfg, conf),
        "tokens_in_list": n_tokens,
        "patterns_in_list": n_patterns,
        "categories": result.categories,
        "total_matches": result.total,
        # an empty token list proves nothing: such a scan is never clean
        "clean": result.total == 0 and n_tokens > 0,
    }


# ---------------------------------------------------------------------------
# Collecting what a CLI will see
# ---------------------------------------------------------------------------


def prompt_sources(cfg: K2valConfig, conf: ModelConf) -> str:
    sample_dir = f"/{conf.space}/logs-sample"
    local = cfg.federation.local.name
    parts = [SYSTEM_PROMPT_TEMPLATE.format(local=local)]
    parts += [s.render_brief(directory=sample_dir, local=local) for s in SCENARIOS.values()]
    return "\n".join(parts)


async def collect_sources(
    cfg: K2valConfig,
    conf: ModelConf,
    caller: ToolCaller,
    *,
    fixture_dir: str,
    sample_file: str,
    ids: list[str],
) -> dict[str, str]:
    sources = {
        "prompts": prompt_sources(cfg, conf),
        "tool_definitions": await caller.list_definitions(),
        "ids": "\n".join([*ids, conf.space, fixture_dir, sample_file]),
    }
    sources["answer:list_space_providers"] = await caller.call(
        "list_space_providers", {"space_id": conf.space}
    )
    sources["answer:get_file_distribution"] = await caller.call(
        "get_file_distribution", {"file_id_or_path": sample_file}
    )
    sources["answer:check_readiness"] = await caller.call(
        "check_readiness", {"extent": [{"dataset": fixture_dir}]}
    )
    return sources


class FastMcpHttpCaller:
    """The scan's client: the same loopback HTTP server a CLI would be given."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def list_definitions(self) -> str:
        from fastmcp import Client

        async with Client(self.url) as client:
            tools = await client.list_tools()
        return json.dumps(
            [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in tools
            ],
            sort_keys=True,
        )

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        from fastmcp import Client

        async with Client(self.url) as client:
            result = await client.call_tool(name, arguments, raise_on_error=False)
        return "".join(getattr(c, "text", "") for c in result.content)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


async def _scan_one(cfg: K2valConfig, conf: ModelConf, fed, tokens, patterns, root: Path) -> dict:
    from benchmark.k2val.clock import REAL_CLOCK
    from benchmark.k2val.fixtures import FixtureBuilder
    from benchmark.k2val.mcp_http import McpHttpServer, new_scratch

    builder = FixtureBuilder(fed, conf.space, REAL_CLOCK)
    fixture = await builder.build(SCENARIOS["V4"].fixture, f"logs-scan-{int(time.time())}")
    scratch = new_scratch("k2val-scan-")
    try:
        owner_id = await fed.provider_id("owner")
        local_id = await fed.provider_id("local")
        sample = next(iter(fixture.files.values()))
        async with McpHttpServer(cfg.path or "", frozenset(cfg.tools_k2), scratch) as server:
            sources = await collect_sources(
                cfg,
                conf,
                FastMcpHttpCaller(server.url),
                fixture_dir=fixture.dir_path,
                sample_file=sample.path,
                ids=[owner_id, local_id, cfg.federation.owner.name, cfg.federation.local.name],
            )
    finally:
        await builder.teardown(fixture)
        import shutil

        shutil.rmtree(scratch, ignore_errors=True)
    return build_record(
        cfg, conf, scan_texts(sources, tokens, patterns), len(tokens), len(patterns)
    )


def scan_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="python -m benchmark.k2val.run scan",
        description="Pre-flight scan of what a CLI leg will see. Prints match counts only.",
    )
    p.add_argument("--config", default="benchmark/k2val/k2val.toml")
    p.add_argument("--models", default="", help="CLI configs to scan; empty = all of them")
    p.add_argument("--run-id", default="", help="also copy the record into this run directory")
    p.add_argument("--artefact-root", default="")
    a = p.parse_args(argv)
    try:
        cfg = load_config(a.config)
        names = [n.strip() for n in a.models.split(",") if n.strip()] or [
            m.name for m in cfg.models.values() if m.is_cli
        ]
        confs = [cfg.models[n] for n in names]
        if not cfg.scan.tokens_file:
            raise ConfigError("[cli.scan] tokens_file is required: a scan without tokens is empty")
        tokens = read_lines(cfg.scan.tokens_file)
        patterns = compile_patterns(read_lines(cfg.scan.patterns_file))
        if not tokens:
            raise ConfigError("the tokens file is empty: such a scan proves nothing")
        import os

        from benchmark.k2val.app import configure_environment
        from benchmark.k2val.federation import FedApi

        os.environ["ONEDATA_MCP_DOTENV"] = "0"
        configure_environment(cfg)
        fed = FedApi.from_conf(cfg.federation, read_federation_secrets(cfg.federation))
    except (ConfigError, SecretFileError, KeyError, OSError) as e:
        say(f"[k2val scan] {e}", file=sys.stderr)
        return 2

    root = Path(a.artefact_root) if a.artefact_root else cfg.artefact_root
    run_dir = root / a.run_id if a.run_id else None
    exit_code = 0
    for conf in confs:
        record = asyncio.run(_scan_one(cfg, conf, fed, tokens, patterns, root))
        path = write_scan_record(root, run_dir, record)
        say(f"[k2val scan] {conf.name}: {'CLEAN' if record['clean'] else 'NOT CLEAN'} -> {path}")
        for category, counts in record["categories"].items():
            say(
                f"    {category}: token matches {counts['tokens']}, "
                f"pattern matches {counts['patterns']}"
            )
        exit_code = exit_code or (0 if record["clean"] else 1)
    return exit_code
