# CLAUDE.md

This file is auto-loaded by Claude Code when working in this repo. The
authoritative agent-facing guidance lives in **`AGENTS.md`** — read it
in full at session start.

## Key principles (one-liner each, see AGENTS.md for detail)

1. **`make` is THE interface.** Don't type `uv run python -m
   benchmark...` or ad-hoc `jq artefacts/...`. If a workflow can't be
   done via a `make` target, ADD the target instead of bypassing it.
   Run `make help` for the full menu.

2. **`make check` before commit.** lint + format + tests. All tests
   must pass. New behaviour gets a new test.

3. **MCP tool layout.** Interface in `onedata_mcp/modules`,
   implementation in `onedata_mcp/api`.

4. **Live state snapshot.** `research/current-state-2026-05-02.md`
   has the latest sweep numbers, applied fixes, and pending
   workstreams. Read it when resuming after a break.

## Cross-references

- `AGENTS.md` — full operational guidance (this file's source of truth)
- `Makefile` — every workflow lives here; `make help` lists targets
- `research/empirical-mcp-server-findings.md` — M-1..M-12 server-design issues
- `research/llm-output-stability-findings.md` — L-1+ model-behaviour quirks
- `research/scenario-catalogue.md` — full reference for all 18 scenarios

## What does NOT belong here

Implementation details, API behaviour catalogues, scenario specs,
fix histories — those live in their respective files under
`research/` or in `AGENTS.md`. This file is a thin pointer; keep it
under ~30 lines so it doesn't drift from AGENTS.md.
