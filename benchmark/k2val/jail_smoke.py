"""`k2val jail-smoke`: one real, harmless prompt per CLI leg, inside the jail, with NO MCP server.

    python -m benchmark.k2val.run jail-smoke --config k2val.toml --models gpt-luna-low,...

It proves that the CLI starts and logs in inside the jail, and it records the shape of the CLI's
JSON event stream (event types and field names, never values) so the harness parsers can be
checked against real output. The prompt is fixed and sends nothing private. Run the leak probe
first (`make k2val-jail-probe`): the smoke does not repeat its checks.

The command line is the harness's own (`codex_argv` / `agy_argv`) minus the MCP server, and the
output goes through the harness's own parsers. Raw events go to
`<artefact root>/jail-smoke/<stamp>-<config>.jsonl` (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from benchmark.k2val.cli_agents import (
    LAST_MESSAGE_FILE,
    agy_argv,
    codex_argv,
    jail_scratch,
    parse_agy_events,
    parse_codex_events,
    run_jailed,
)
from benchmark.k2val.config import ConfigError, JailConf, ModelConf, load_config
from benchmark.k2val.jail import (
    JailError,
    JailFileError,
    jail_available,
    jail_cmd,
    read_jail_file,
    remove_tree,
)
from benchmark.k2val.secrets import redact, say

SMOKE_PROMPT = "Reply with the single word OK."


def shape(value: Any) -> Any:
    """The structure of a JSON value with every leaf replaced by its type name."""
    if isinstance(value, dict):
        return {k: shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return type(value).__name__


def event_shapes(lines: list[str]) -> list[dict]:
    """One entry per distinct event shape, in first-seen order."""
    seen: list[dict] = []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            s = shape(ev)
            if s not in seen:
                seen.append(s)
    return seen


def smoke_argv(conf: ModelConf) -> list[str]:
    if conf.backend == "codex":
        return codex_argv(conf, None, SMOKE_PROMPT)
    return agy_argv(conf, SMOKE_PROMPT)


async def smoke_one(conf: ModelConf, jail: JailConf, out_dir: Path) -> dict:
    scratch = jail_scratch("k2val-smoke-")
    workdir = scratch / "work"
    workdir.mkdir()
    try:
        cmd = jail_cmd(smoke_argv(conf), str(workdir), jail, cli=conf.backend)
        t0 = time.monotonic()
        run = await run_jailed(cmd, scratch, workdir)
        lines, rc = run.lines, run.returncode
        elapsed = time.monotonic() - t0
        try:  # /work is jail-written: the hardened read, never read_text
            last_message = (read_jail_file(workdir, LAST_MESSAGE_FILE) or "").strip()
        except JailFileError as e:
            last_message = f"<not read: {e}>"
    finally:
        remove_tree(scratch)
    if conf.backend == "codex":
        parsed = parse_codex_events(lines)
    else:
        parsed = parse_agy_events(lines, frozenset())
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    raw = out_dir / f"{stamp}-{conf.name}.jsonl"
    raw.write_text("".join(redact(line) + "\n" for line in lines), encoding="utf-8")
    return {
        "config": conf.name,
        "returncode": rc,
        "output_truncated": run.truncated,
        "seconds": round(elapsed, 1),
        "final_message_parsed": parsed.final_answer,
        "final_message_file": last_message,
        "tokens": [parsed.tokens_in, parsed.tokens_out],
        "parser_error": parsed.error,
        "unparsed_lines": parsed.unparsed,
        "event_shapes": event_shapes(lines),
        "raw_events": str(raw),
    }


def smoke_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmark.k2val.run jail-smoke")
    p.add_argument("--config", default="benchmark/k2val/k2val.toml")
    p.add_argument("--models", default="", help="CLI configs; empty = all of them")
    p.add_argument("--jail", default="", help="the jail wrapper (default: [cli.jail] wrapper)")
    a = p.parse_args(argv)
    try:
        cfg = load_config(a.config)
        names = [n.strip() for n in a.models.split(",") if n.strip()] or [
            m.name for m in cfg.models.values() if m.is_cli
        ]
        confs = [cfg.models[n] for n in names]
        if not all(c.is_cli for c in confs):
            raise ConfigError("jail-smoke runs CLI configs only")
        jail = JailConf(wrapper=a.jail) if a.jail else cfg.jail
        if not jail_available(jail):
            raise JailError("no jail: pass --jail or set [cli.jail] wrapper; never unjailed")
    except (ConfigError, JailError, KeyError) as e:
        say(f"[k2val jail-smoke] {e}", file=sys.stderr)
        return 2
    out_dir = cfg.artefact_root / "jail-smoke"
    code = 0
    for conf in confs:
        result = asyncio.run(smoke_one(conf, jail, out_dir))
        say(json.dumps(result, indent=2))
        code = code or (0 if result["returncode"] == 0 else 1)
    return code
