"""CLI of the validation panel.

    python -m benchmark.k2val.run --config benchmark/k2val/k2val.toml \\
        --models glm,qwen36,qwencoder,deepseek,haiku-low,haiku-medium,haiku-high,sonnet-medium \\
        --scenarios V1..V7 --k 3

    python -m benchmark.k2val.run scan   --config ... --models gpt-luna-low,gemini-flash-low
    python -m benchmark.k2val.run change --run-id ... --target ... --trigger-trial ... \\
        --reason ... --before-file ... --after-file ...
    python -m benchmark.k2val.run rejudge --run-dir artefacts/k2val/<run id> [--config ...]

`make k2val` runs the first form. Everything runs against the LOCAL Compose federation only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from benchmark.k2val.artefact import K2TrialArtefact
from benchmark.k2val.config import (
    ConfigError,
    K2valConfig,
    ModelConf,
    load_config,
    read_federation_secrets,
    read_model_key,
)
from benchmark.k2val.scenarios import SCENARIOS, parse_scenarios
from benchmark.k2val.secrets import SecretFileError, redact, say

DEFAULT_CONFIG = "benchmark/k2val/k2val.toml"
ALL_SCENARIOS = "V1..V7"


def _run_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m benchmark.k2val.run",
        description="Validation panel for the tools PPAM never swept (design/08). "
        "Subcommands: `scan` (pre-flight scan for the CLI legs), `change` (record a change).",
    )
    p.add_argument("--config", default=DEFAULT_CONFIG, help="k2val.toml (default: %(default)s)")
    p.add_argument("--models", default="", help="comma-separated config names; empty = all")
    p.add_argument("--scenarios", default=ALL_SCENARIOS, help="e.g. V1..V7 or V2,V5")
    p.add_argument("--k", type=int, default=3, help="trials per (config, scenario)")
    p.add_argument("--run-id", default="", help="default: a UTC timestamp. Reuse to resume.")
    p.add_argument("--artefact-root", default="", help="override [run] artefact_root")
    p.add_argument("--no-report", action="store_true", help="do not write research/k2val-<id>.md")
    p.add_argument(
        "--no-judge",
        action="store_true",
        help="run without the judge: every judged trial is then NEEDS_REVIEW (judge_error)",
    )
    return p


def judge_conf(cfg: K2valConfig | None):
    from benchmark.k2val.judge import JudgeConf

    if cfg is None:
        return JudgeConf()
    j = cfg.judge
    return JudgeConf(j.model, j.effort, j.binary, j.timeout_s, j.concurrency)


def _select(cfg: K2valConfig, wanted: str) -> list[ModelConf]:
    if not wanted.strip():
        return list(cfg.models.values())
    names = [n.strip() for n in wanted.split(",") if n.strip()]
    unknown = [n for n in names if n not in cfg.models]
    if unknown:
        raise ConfigError(f"unknown model(s) {unknown}; configured: {sorted(cfg.models)}")
    return [cfg.models[n] for n in names]


async def _tools_snapshot(app, cfg: K2valConfig, run_dir: Path) -> None:
    """The tool definitions the models see, written once per run: a later change to a
    description shows as a difference between two runs' snapshots."""
    import hashlib

    async def collect() -> dict:
        tools = {t.name: t for t in await app.list_tools()}
        offered = sorted(set(cfg.tools_k1) | set(cfg.tools_k2))
        rows = {
            n: {"description": tools[n].description, "parameters": tools[n].parameters}
            for n in offered
            if n in tools
        }
        digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
        return {"sha256": digest, "tools": rows}

    (run_dir / "tools_snapshot.json").write_text(
        json.dumps(await collect(), indent=2, sort_keys=True), encoding="utf-8"
    )


def run_main(argv: list[str]) -> int:
    args = _run_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)  # the host guard runs here
        scenarios = [SCENARIOS[s] for s in parse_scenarios(args.scenarios)]
        selected = _select(cfg, args.models)
    except (ConfigError, ValueError, OSError) as e:
        say(f"[k2val] {e}", file=sys.stderr)
        return 2

    from benchmark.k2val import claude_sdk, cli_agents

    models: list[ModelConf] = []
    skipped: list[str] = []
    root = Path(args.artefact_root) if args.artefact_root else cfg.artefact_root
    for conf in selected:
        if conf.is_cli:
            ok, why = cli_agents.leg_enabled(cfg, conf, root)
            if not ok:
                skipped.append(f"{conf.name}: {why}")
                continue
        models.append(conf)
    for line in skipped:
        say(f"[k2val] SKIPPED {line}")
    if not models:
        say("[k2val] no model to run.", file=sys.stderr)
        return 2

    try:
        from benchmark.k2val.app import build_app, configure_environment
        from benchmark.k2val.federation import FedApi
        from benchmark.k2val.legs import make_api_adapter_factory
        from benchmark.k2val.report import build_report, write_report
        from benchmark.k2val.runner import RunDeps, run_sweep
        from benchmark.k2val.spend import SpendTracker

        configure_environment(cfg)
        secrets = read_federation_secrets(cfg.federation)
        for conf in models:  # fail now, not after a fixture is built
            if conf.key_file:
                read_model_key(conf)
            if conf.is_claude_sdk:
                claude_sdk.resolve_cli(conf)
    except (ConfigError, SecretFileError, FileNotFoundError) as e:
        say(f"[k2val] {e}", file=sys.stderr)
        return 2

    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    spend = None
    if any(m.is_anthropic for m in models):
        spend = SpendTracker(cfg.spend.cap_usd, cfg.spend.reserve_usd, run_dir)
    app = build_app()
    api_factory = make_api_adapter_factory(cfg, spend)

    def adapter_factory(conf, on_cost):
        if conf.is_cli:
            return cli_agents.make_cli_adapter(cfg, conf, run_dir, secrets)
        if conf.is_claude_sdk:  # the subscription: no key to read, nothing to cost
            return claude_sdk.make_claude_sdk_adapter(cfg, conf, run_dir)
        return api_factory(conf, on_cost)

    from benchmark.k2val.judge import make_judge

    deps = RunDeps(
        cfg=cfg,
        run_id=run_id,
        run_dir=run_dir,
        fed=FedApi.from_conf(cfg.federation, secrets),
        app=app,
        adapter_factory=adapter_factory,
        spend=spend,
        judge=None if args.no_judge else make_judge(judge_conf(cfg)),
    )
    meta = {
        "judge": None if args.no_judge else vars(judge_conf(cfg)),
        "k": args.k,
        "models": [m.name for m in models],
        "scenarios": [s.id for s in scenarios],
        "skipped": skipped,
        "started_at": time.time(),
    }
    say(
        f"[k2val] run_id={run_id}  artefacts -> {run_dir}  configs={[m.name for m in models]}  "
        f"scenarios={[s.id for s in scenarios]}  K={args.k}"
    )

    def on_trial(a: K2TrialArtefact) -> None:
        say(
            f"  [{a.llm_name} / {a.scenario_id} / trial {a.trial_ix}] {a.outcome}  "
            f"({a.wall_clock_seconds:.0f}s, rounds={a.rounds_used}, in={a.usage_in_tokens}, "
            f"out={a.usage_out_tokens}, ${a.cost_usd:.4f})"
            + (f"  {a.oracle_diagnosis[:80]}" if a.oracle_diagnosis else "")
            + (f"  err={a.error[:60]}" if a.error else "")
        )

    async def go() -> None:
        # one event loop for the snapshot and the sweep: the app is used in no other
        await _tools_snapshot(app, cfg, run_dir)
        await run_sweep(deps, models, scenarios, args.k, on_trial=on_trial)

    t0 = time.time()
    asyncio.run(go())
    meta["finished_at"] = time.time()
    meta["spend"] = spend.summary() if spend else None
    (run_dir / "run.json").write_text(redact(json.dumps(meta, indent=2)), encoding="utf-8")
    for note in deps.notes:
        say(f"[k2val] NOTE {note}")
    say(f"[k2val] DONE in {time.time() - t0:.0f}s")
    if not args.no_report:
        text = build_report(run_dir, cfg, run_id=run_id, meta=meta)
        say(f"[k2val] report -> {write_report(text, cfg.research_dir, run_id)}")
    return 0


def change_main(argv: list[str]) -> int:
    from benchmark.k2val.changes import record_change

    p = argparse.ArgumentParser(prog="python -m benchmark.k2val.run change")
    p.add_argument("--artefact-root", default="artefacts/k2val")
    p.add_argument("--run-id", required=True)
    p.add_argument("--target", required=True, help="e.g. tool:check_readiness or answer:V5")
    p.add_argument("--trigger-trial", required=True, help="trial id that triggered the change")
    p.add_argument("--reason", required=True)
    p.add_argument("--before-file", required=True)
    p.add_argument("--after-file", required=True)
    a = p.parse_args(argv)
    path = record_change(
        Path(a.artefact_root) / a.run_id,
        target=a.target,
        trigger_trial=a.trigger_trial,
        reason=a.reason,
        before=Path(a.before_file).read_text(encoding="utf-8"),
        after=Path(a.after_file).read_text(encoding="utf-8"),
    )
    say(f"[k2val] change recorded in {path}")
    return 0


def rejudge_main(argv: list[str]) -> int:
    """Read stored answers again (rejudge.py): the current deterministic readers, the judge
    (cached verdicts reused), and a new score where the artefact allows it. No trial runs."""
    from benchmark.k2val.rejudge import (
        VerdictCache,
        compare,
        iter_trials,
        judge_trials,
        write_outputs,
    )

    p = argparse.ArgumentParser(prog="python -m benchmark.k2val.run rejudge")
    p.add_argument("--run-dir", action="append", required=True, help="a run's artefact directory")
    p.add_argument("--config", default="", help="k2val.toml, for its [judge] (default: built-in)")
    p.add_argument("--out", default="", help="output directory (default: <first run dir>/rejudge)")
    p.add_argument("--cached-only", action="store_true", help="make no judge call")
    a = p.parse_args(argv)
    try:
        cfg = load_config(a.config) if a.config else None
    except ConfigError as e:
        say(f"[k2val] {e}", file=sys.stderr)
        return 2
    conf = judge_conf(cfg)
    dirs = [Path(d) for d in a.run_dir]
    out = Path(a.out) if a.out else dirs[0] / "rejudge"
    trials = list(iter_trials(dirs))
    cache = VerdictCache(out / "verdicts.jsonl")
    say(f"[k2val] rejudge: {len(trials)} trials from {[str(d) for d in dirs]} -> {out}")

    def progress(t, v) -> None:
        say(f"  {t.key}: {v.value!r}" + (f"  judge_error={v.error}" if v.error else ""))

    from benchmark.k2val import judge as judge_module

    judge = None if a.cached_only else judge_module.make_judge(conf)
    verdicts = asyncio.run(judge_trials(trials, judge, conf, cache, on_verdict=progress))
    rows = compare(trials, verdicts)
    jsonl, md = write_outputs(rows, out)
    disagree = sum(1 for r in rows if not r["agree"])
    say(f"[k2val] {len(rows)} answers read; {disagree} disagreements -> {md} ({jsonl.name})")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The repository's `.env` points the PPAM harness at the live federation. It must never be
    # read here: this switch is set before anything imports `onedata_mcp.config`.
    os.environ["ONEDATA_MCP_DOTENV"] = "0"
    if argv[:1] == ["scan"]:
        from benchmark.k2val.scan import scan_main

        return scan_main(argv[1:])
    if argv[:1] == ["change"]:
        return change_main(argv[1:])
    if argv[:1] == ["rejudge"]:
        return rejudge_main(argv[1:])
    if argv[:1] == ["jail-smoke"]:
        from benchmark.k2val.jail_smoke import smoke_main

        return smoke_main(argv[1:])
    return run_main(argv)


if __name__ == "__main__":
    sys.exit(main())
