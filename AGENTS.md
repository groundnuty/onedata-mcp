# AGENTS.md

## `make` is the canonical interface

**Use `make` targets for every operation.** If a workflow can't be done
via a `make` target, ADD the target — don't reach for raw `uv run python
-m benchmark...` or `jq` commands at the shell. The Makefile is the
single source of truth for operational knowledge.

Run `make help` for the full list. Common entry points:

```bash
# Setup + dev loop
make install              # editable install of onedata-mcp
make test                 # run all unit tests
make test-verbose         # tests with -v
make test-file FILE=path  # run a single test file
make check                # lint + format + test (pre-commit gate)

# Federation provisioning
make spaces-create        # per-LLM Onedata spaces (idempotent)
make spaces-support       # provider attach (idempotent)
make spaces-status        # diagnostic

# Sweeps
make smoke                # quick D1+P1 across panel (~3 min)
make sweep-cyfronet       # Sonnet+Qwen+GLM K=1 (~25 min)
make sweep-deepseek       # V4-pro K=1 via OpenRouter (~10 min)
make sweep-all            # full panel K=1, two-phase, shared run-id
make sweep-k8             # K=8 headline (~3-4 hours)

# Custom parametrised sweep
make sweep LLMS=qwen3.6-35b SCENARIOS=D1,D2,A4 K=1 PARALLEL=2
make sweep LLMS=glm-4.7-flash RID=<existing-run-id>   # attach to run

# Reporting + inspection
make report               # regenerate REPORT_paper.md, REPORT_cyfronet.md
make list-runs            # newest artefact dirs
make show-headline        # per-LLM pass-rate (latest run)
make show-grid            # per-cell grid (D1..D6 A1..A6 P1..P6)
make show-trial RID=... LLM=... SCEN=...     # single trial details
make inspect-fail RID=... LLM=... SCEN=...   # fail diagnosis
```

**When NOT to use raw shell:** the only legitimate raw-shell operations
are `git`, `gh`, file editing tools (Read/Edit/Write), and ad-hoc
filesystem inspection (`ls`, `cat`). Anything that runs benchmark
code, calls Onedata, or queries trial JSONLs goes through `make`.

If you find yourself typing `uv run python -m benchmark.X ...` or
`jq ... artefacts/...`, stop — add the target instead.

## Repo conventions

- New MCP tool: interface in `onedata_mcp/modules`, implementation in
  `onedata_mcp/api`.
- Always run `make check` after changes; all tests must pass before
  commit.
- Add tests for every feature or bug fix.

## Project state

See `research/current-state-2026-05-02.md` for the live snapshot of
sweep results, applied fixes, and pending workstreams.