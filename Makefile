# PPAM 2026 benchmark — operations entry points.
#
# All commands assume `uv` is on PATH. Run `make help` for the full list.
#
# Conventions:
# - `make` is THE interface to this project. If a workflow can't be done
#   via a make target, add the target. Don't run raw `uv run python -m
#   benchmark...` — those are implementation details that should live
#   inside Makefile recipes.
# - Variables (LLMS, SCENARIOS, K, RID, FILE, etc.) are passed via the
#   command line: `make sweep LLMS=qwen3.6-35b SCENARIOS=D1,D2 K=1`.
# - Targets that need a run_id default to creating a fresh one; pass
#   RID=<existing-id> to attach to an existing run for cross-llm
#   merging or re-runs.

.PHONY: help install test test-file test-verbose lint format check \
        spaces-create spaces-support spaces-status \
        smoke smoke-d1 smoke-fast \
        sweep sweep-cyfronet sweep-deepseek sweep-claude sweep-all \
        sweep-headline sweep-k8 \
        conformance inspect-smoke \
        report report-latest \
        show-grid show-grid-rate show-headline show-trial inspect-fail list-runs \
        clean clean-artefacts

# Default target prints the command list.
help:
	@echo "PPAM 2026 benchmark — Make targets"
	@echo ""
	@echo "  Setup"
	@echo "    install            pip install the MCP server in editable mode"
	@echo "    test               run unit tests"
	@echo "    test-verbose       run tests with -v"
	@echo "    test-file FILE=... run a single test file (FILE=test/unit/foo.py)"
	@echo "    lint               ruff check (auto-fix)"
	@echo "    format             ruff format"
	@echo "    check              lint + format + test (pre-commit gate)"
	@echo ""
	@echo "  Federation provisioning  (run once per panel-LLM addition)"
	@echo "    spaces-create      create per-LLM Onedata spaces (idempotent)"
	@echo "    spaces-support     attach providers to per-LLM spaces (idempotent)"
	@echo "    spaces-status      list current spaces + their support state"
	@echo ""
	@echo "  Smokes  (cheap, single-trial verification)"
	@echo "    smoke-d1           D1 only across the panel (~1 min)"
	@echo "    smoke              D1+P1 across the panel (~3 min)"
	@echo "    smoke-fast         D-band only across the panel (~6 min)"
	@echo ""
	@echo "  K=1 sweeps  (full 18 scenarios, single trial)"
	@echo "    sweep-cyfronet     Cyfronet+Anthropic legs in parallel (~25 min)"
	@echo "    sweep-deepseek     DeepSeek leg serial via OpenRouter (~10 min)"
	@echo "    sweep-claude       Claude only (~25 min)"
	@echo "    sweep-all          Two-phase: Cyfronet+Anthropic parallel,"
	@echo "                       DeepSeek serial (~35 min, single run-id)"
	@echo ""
	@echo "  Custom sweep (variable LLMs / scenarios / trials / run-id)"
	@echo "    sweep              fully parametrised. Examples:"
	@echo "      make sweep LLMS=qwen3.6-35b"
	@echo "      make sweep LLMS=claude-sonnet-4-5,qwen3.6-35b SCENARIOS=D1,D2,A4"
	@echo "      make sweep LLMS=deepseek-v4-pro K=8 PARALLEL=1"
	@echo "      make sweep LLMS=glm-4.7-flash RID=20260502T204921_postfix_v3"
	@echo ""
	@echo "  Headline runs"
	@echo "    sweep-k8           K=8 across the full panel (~3-4 hours)"
	@echo "    sweep-headline     alias for sweep-k8"
	@echo ""
	@echo "  MCP-protocol validation  (paper-grade artifacts)"
	@echo "    conformance        run modelcontextprotocol/conformance suite v0.1.16+"
	@echo "                       against onedata-mcp on a temp HTTP transport;"
	@echo "                       output → conformance-results/<timestamp>/"
	@echo "    inspect-smoke      run modelcontextprotocol/inspector --cli"
	@echo "                       and assert the 14 tools are listed"
	@echo ""
	@echo "  Reporting + inspection"
	@echo "    report-latest      regenerate REPORT_paper.md + REPORT_cyfronet.md"
	@echo "                       for the most recent artefact run"
	@echo "    report             alias for report-latest"
	@echo "    list-runs          list artefact directories newest-first"
	@echo "    show-headline RID=...  per-LLM pass-rate + fail list"
	@echo "    show-grid RID=...      per-cell pass/fail grid (K=1, last-trial-wins)"
	@echo "    show-grid-rate RID=... per-cell PASS-count/K (K>=1; for K=8 headline)"
	@echo "    show-trial RID=... LLM=... SCEN=... — full JSONL summary"
	@echo "    inspect-fail RID=... LLM=... SCEN=... — diag + final_answer + tools"
	@echo ""
	@echo "  Housekeeping"
	@echo "    clean              remove pyc, __pycache__, build artefacts"
	@echo "    clean-artefacts    remove all run artefacts (DESTRUCTIVE)"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

install:
	uv pip install -e .

test:
	uv run pytest test/unit -q

test-verbose:
	uv run pytest test/unit -v

# Run a single test file. Example:
#   make test-file FILE=test/unit/test_oracle_p3_loosened.py
test-file:
	@if [ -z "$(FILE)" ]; then \
	  echo "ERROR: pass FILE=<path>. e.g. make test-file FILE=test/unit/test_oracle_p3_loosened.py"; \
	  exit 1; \
	fi
	uv run pytest $(FILE) -v

lint:
	uv run ruff check benchmark/ onedata_mcp/ test/ --fix

format:
	uv run ruff format benchmark/ onedata_mcp/ test/

check: lint format test

# ---------------------------------------------------------------------------
# Federation provisioning
# ---------------------------------------------------------------------------

spaces-create:
	uv run python -m benchmark.setup_per_llm_spaces

spaces-support:
	uv run python -m benchmark.support_per_llm_spaces

# Print which spaces exist + provider support — read-only diagnostic.
spaces-status:
	@uv run python -c "import asyncio,os,httpx; from pathlib import Path; \
	from dotenv import load_dotenv; load_dotenv(Path('.env')); \
	from benchmark._per_llm_spaces import PER_LLM_SPACE_ID; \
	asyncio.run(__import__('benchmark.support_per_llm_spaces',fromlist=['main']).main(\
	__import__('argparse').Namespace(size_mib=100)))"

# ---------------------------------------------------------------------------
# Smokes
# ---------------------------------------------------------------------------

smoke-d1:
	uv run python -m benchmark.run_panel --trials 1 --scenarios D1 --scenario-parallelism 2

smoke:
	uv run python -m benchmark.run_panel --trials 1 --scenarios D1,P1 --scenario-parallelism 2

smoke-fast:
	uv run python -m benchmark.run_panel --trials 1 \
	  --scenarios D1,D2,D3,D4,D5,D6 --scenario-parallelism 2

# ---------------------------------------------------------------------------
# K=1 sweeps
# ---------------------------------------------------------------------------

sweep-cyfronet:
	uv run python -m benchmark.run_panel --trials 1 \
	  --llms claude-sonnet-4-5,qwen3.6-35b,glm-4.7-flash \
	  --scenario-parallelism 2

sweep-deepseek:
	uv run python -m benchmark.run_panel --trials 1 \
	  --llms deepseek-v4-pro \
	  --scenario-parallelism 1

sweep-claude:
	uv run python -m benchmark.run_panel --trials 1 \
	  --llms claude-sonnet-4-5 \
	  --scenario-parallelism 2

# Two-phase sweep with a shared run_id so reports merge cleanly.
# Cyfronet+Anthropic in parallel (federation-friendly), DeepSeek serial
# (paid endpoint stability).
sweep-all:
	@RID=$$(date -u +%Y%m%dT%H%M%S); \
	echo "Shared run_id: $$RID"; \
	echo "===PHASE 1: Cyfronet + Anthropic (parallel=2)==="; \
	uv run python -m benchmark.run_panel --trials 1 \
	  --llms claude-sonnet-4-5,qwen3.6-35b,glm-4.7-flash \
	  --scenario-parallelism 2 --run-id "$$RID"; \
	echo ""; \
	echo "===PHASE 2: OpenRouter (serial)==="; \
	uv run python -m benchmark.run_panel --trials 1 \
	  --llms deepseek-v4-pro \
	  --scenario-parallelism 1 --run-id "$$RID"; \
	echo ""; \
	echo "===Generating reports==="; \
	uv run python -m benchmark.report --run-id "$$RID"

# Headline K=8 run — fully SEQUENTIAL across LLMs (one leg at a time)
# to be kind to the federation (no concurrent space-resets across
# different LLMs) and to local vLLM (single GPU box probably can't
# serve 3 concurrent inferences). Within each LLM scenarios run
# scenario_parallelism=1 (sequential).
#
# V4-pro is rate-limit-sensitive (OpenRouter → SiliconFlow). To avoid
# burning a 4-hour run only to discover the rate-limit budget was
# exhausted, V4-pro is gated: K=1 probe first, then 7 more if the
# probe shows no RateLimitError exhaustion.
#
# Estimated wall (very rough):
#   Each LLM K=8 = ~0.5-1.5h depending on tool-call rounds
#   7 LLMs × 1h ≈ 7h serial. Acceptable for a one-time headline run.

sweep-k8:
	@RID=$$(date -u +%Y%m%dT%H%M%S)_k8; \
	mkdir -p "artefacts/$$RID"; \
	LOG="artefacts/$$RID/sweep-k8.log"; \
	PERSTEP="artefacts/$$RID/_per-step-logs"; \
	mkdir -p "$$PERSTEP"; \
	{ \
	  echo ""; \
	  echo "============================================================"; \
	  echo "K=8 HEADLINE RUN  •  run_id=$$RID"; \
	  echo "  Sequential across 7 panel LLMs, scenario_parallelism=1"; \
	  echo "  Started: $$(date -u +%FT%TZ)"; \
	  echo "  Logs: $$LOG  +  $$PERSTEP/<step>.log"; \
	  echo "============================================================"; \
	  echo ""; \
	  \
	  echo "=== Step 1: V4-pro K=1 (rate-limit probe) ==="; \
	  uv run python -m benchmark.run_panel --trials 1 \
	    --llms deepseek-v4-pro \
	    --scenario-parallelism 1 --run-id "$$RID" 2>&1 \
	    | tee "$$PERSTEP/01-v4pro-probe.log"; \
	  RL_HITS=$$(grep -lE "RateLimitError" artefacts/$$RID/deepseek-v4-pro__*.jsonl 2>/dev/null | wc -l | tr -d ' '); \
	  if [ "$$RL_HITS" -gt 0 ]; then \
	    echo ""; \
	    echo "!!! V4-pro probe: $$RL_HITS cells hit RateLimitError after retry exhaustion."; \
	    echo "    Investigate before proceeding to K=8. Other 6 LLMs NOT yet started."; \
	    echo "    Use 'make show-grid RID=$$RID' to inspect."; \
	    exit 1; \
	  fi; \
	  echo ""; \
	  echo "=== Step 2: V4-pro K=7 more (target K=8) ==="; \
	  uv run python -m benchmark.run_panel --trials 7 \
	    --llms deepseek-v4-pro \
	    --scenario-parallelism 1 --run-id "$$RID" 2>&1 \
	    | tee "$$PERSTEP/02-v4pro-rest.log"; \
	  echo ""; \
	  \
	  echo "=== Step 3: Sonnet K=8 (Anthropic, claude-agent-sdk) ==="; \
	  uv run python -m benchmark.run_panel --trials 8 \
	    --llms claude-sonnet-4-5 \
	    --scenario-parallelism 1 --run-id "$$RID" 2>&1 \
	    | tee "$$PERSTEP/03-sonnet.log"; \
	  echo ""; \
	  \
	  echo "=== Step 4: Cyfronet Forge K=8 (Qwen, GLM — sequential) ==="; \
	  N=4; \
	  for llm in qwen3.6-35b glm-4.7-flash; do \
	    echo "--- $$llm ---"; \
	    uv run python -m benchmark.run_panel --trials 8 \
	      --llms $$llm \
	      --scenario-parallelism 1 --run-id "$$RID" 2>&1 \
	      | tee "$$PERSTEP/0$$N-$$llm.log"; \
	    N=$$((N + 1)); \
	  done; \
	  echo ""; \
	  \
	  echo "=== Step 5: Local vLLM K=8 (Gemma, Granite, Devstral — sequential) ==="; \
	  N=6; \
	  for llm in gemma-4-31b-it granite-4.1-30b devstral-2-123b; do \
	    echo "--- $$llm ---"; \
	    uv run python -m benchmark.run_panel --trials 8 \
	      --llms $$llm \
	      --scenario-parallelism 1 --run-id "$$RID" 2>&1 \
	      | tee "$$PERSTEP/0$$N-$$llm.log"; \
	    N=$$((N + 1)); \
	  done; \
	  echo ""; \
	  \
	  echo "=== Step 6: Generate reports ==="; \
	  uv run python -m benchmark.report --run-id "$$RID" 2>&1 \
	    | tee "$$PERSTEP/09-report.log"; \
	  echo ""; \
	  echo "  Finished: $$(date -u +%FT%TZ)"; \
	  echo "  Total trials: $$(ls artefacts/$$RID/*.jsonl | xargs cat 2>/dev/null | wc -l) (across 7 LLMs)"; \
	  echo "  K=8 headline complete  •  artefacts/$$RID/"; \
	} 2>&1 | tee "$$LOG"

sweep-headline: sweep-k8

# ---------------------------------------------------------------------------
# Custom sweep (parametrised)
# ---------------------------------------------------------------------------
#
# Variables (all optional; defaults shown):
#   LLMS=<comma-list>      panel LLM names. Default: full panel
#   SCENARIOS=<comma-list> scenario IDs (D1..P6). Default: all 18
#   K=<int>                trials per cell. Default: 1
#   PARALLEL=<int>         scenario_parallelism. Default: 2
#   RID=<run-id>           reuse an existing run-id. Default: fresh timestamp
#                          + descriptive suffix derived from LLMS
#   LABEL=<suffix>         override the descriptive suffix on a fresh RID
#
# Tees the live log to artefacts/<RID>/sweep.log so the run is auditable
# even if the foreground process dies.

LLMS ?=
SCENARIOS ?=
K ?= 1
PARALLEL ?= 2
RID ?=
LABEL ?=

sweep:
	@RID="$(RID)"; \
	if [ -z "$$RID" ]; then \
	  STAMP=$$(date -u +%Y%m%dT%H%M%S); \
	  if [ -n "$(LABEL)" ]; then SUFFIX="$(LABEL)"; \
	  elif [ -n "$(LLMS)" ]; then SUFFIX="$$(echo '$(LLMS)' | tr ',' '_' | tr -d ' .')"; \
	  else SUFFIX="all"; fi; \
	  RID="$${STAMP}_$${SUFFIX}"; \
	fi; \
	mkdir -p "artefacts/$${RID}"; \
	echo "Run ID: $${RID}"; \
	LLMS_ARG=""; \
	if [ -n "$(LLMS)" ]; then LLMS_ARG="--llms $(LLMS)"; fi; \
	SCEN_ARG=""; \
	if [ -n "$(SCENARIOS)" ]; then SCEN_ARG="--scenarios $(SCENARIOS)"; fi; \
	uv run python -m benchmark.run_panel \
	  --trials $(K) \
	  $$LLMS_ARG $$SCEN_ARG \
	  --scenario-parallelism $(PARALLEL) \
	  --run-id "$${RID}" 2>&1 \
	  | tee "artefacts/$${RID}/sweep.log"

# ---------------------------------------------------------------------------
# MCP-protocol validation
# ---------------------------------------------------------------------------
#
# These targets spawn onedata-mcp on a temporary HTTP transport (via
# scripts/with-http-server.sh), run an external validator against it,
# and tear the server down. Both produce paper-citable artifacts:
#
#   conformance     — modelcontextprotocol/conformance v0.1.16+
#                     (March 2026, Anthropic-canonical protocol suite).
#                     Validates JSON-RPC, capability negotiation,
#                     spec compliance. Output: conformance-results/.
#
#   inspect-smoke   — modelcontextprotocol/inspector --cli mode.
#                     Calls tools/list and asserts the expected
#                     14-tool surface is present. Cheap CI-style check.
#
# Both target localhost; no federation traffic. Safe to run alongside
# concurrent benchmark sweeps.

# Conformance port — overridable for parallel runs.
MCP_CONFORMANCE_PORT ?= 3037

conformance:
	@RID=$$(date -u +%Y%m%dT%H%M%SZ); \
	OUTDIR="conformance-results/$${RID}"; \
	mkdir -p "$${OUTDIR}"; \
	echo "Conformance run → $${OUTDIR}"; \
	scripts/with-http-server.sh --port $(MCP_CONFORMANCE_PORT) -- \
	  npx -y @modelcontextprotocol/conformance@latest server \
	    --url __MCP_URL__ \
	    --expected-failures conformance-baseline.yaml \
	    --output-dir "$${OUTDIR}" 2>&1 | tee "$${OUTDIR}/run.log"; \
	echo ""; \
	echo "Conformance results saved to $${OUTDIR}"

# Inspector smoke — one-shot tools/list assertion.
MCP_INSPECT_PORT ?= 3038

inspect-smoke:
	@scripts/with-http-server.sh --port $(MCP_INSPECT_PORT) -- \
	  bash -c '\
	    set -e; \
	    echo "Calling tools/list via inspector --cli..."; \
	    OUT=$$(npx -y @modelcontextprotocol/inspector@latest --cli __MCP_URL__ \
	      --method tools/list 2>&1); \
	    echo "$$OUT"; \
	    COUNT=$$(echo "$$OUT" | grep -cE "\"name\":\\s*\"" || true); \
	    EXPECTED_MIN=14; \
	    if [ "$$COUNT" -ge "$$EXPECTED_MIN" ]; then \
	      echo ""; \
	      echo "✓ inspect-smoke OK — found $$COUNT tools (>= $$EXPECTED_MIN expected)"; \
	    else \
	      echo ""; \
	      echo "✗ inspect-smoke FAIL — found $$COUNT tools (< $$EXPECTED_MIN expected)" >&2; \
	      exit 1; \
	    fi'

# ---------------------------------------------------------------------------
# Reporting + inspection
# ---------------------------------------------------------------------------

report-latest:
	uv run python -m benchmark.report

report: report-latest

# List artefact directories newest-first with their JSONL count.
list-runs:
	@for d in $$(ls -1d artefacts/2026* 2>/dev/null | sort -r | head -20); do \
	  cnt=$$(ls $$d/*.jsonl 2>/dev/null | wc -l | tr -d ' '); \
	  printf "%s  %3s trials\n" "$$d" "$$cnt"; \
	done

# Per-LLM pass-rate headline + fail list. Uses the latest run if RID
# isn't passed.
#
# JSONL semantics: each file may contain multiple lines if the cell was
# re-run or K>1. The LAST line is taken as canonical for K=1 inspection
# (most recent trial wins, matching re-run-overwrites semantics). For
# K>1 see issue #22 (pass^k aggregator).
show-headline:
	@RID="$(RID)"; \
	if [ -z "$$RID" ]; then \
	  RID=$$(ls -1d artefacts/2026* 2>/dev/null | sort -r | head -1); \
	  echo "(latest run: $${RID})"; \
	else \
	  RID="artefacts/$${RID}"; \
	fi; \
	for f in $$RID/*.jsonl; do \
	  basename "$$f" .jsonl; \
	done | sed 's/__.*//' | sort -u | while read llm; do \
	  pass=0; total=0; fails=""; \
	  for f in $$RID/$${llm}__*.jsonl; do \
	    total=$$((total + 1)); \
	    o=$$(tail -1 "$$f" | jq -r '.outcome'); \
	    if [ "$$o" = "PASS" ]; then \
	      pass=$$((pass + 1)); \
	    else \
	      fails="$$fails $$(basename $$f .jsonl | sed "s/$${llm}__//")($$o)"; \
	    fi; \
	  done; \
	  printf "%-22s  %2d/%-2d  fails:%s\n" "$$llm" "$$pass" "$$total" "$$fails"; \
	done

# Per-cell grid: rows = LLMs, cols = scenarios D1..D6 A1..A6 P1..P6.
show-grid:
	@RID="$(RID)"; \
	if [ -z "$$RID" ]; then \
	  RID=$$(ls -1d artefacts/2026* 2>/dev/null | sort -r | head -1); \
	  echo "(latest run: $${RID})"; \
	else \
	  RID="artefacts/$${RID}"; \
	fi; \
	printf "%-22s D1 D2 D3 D4 D5 D6 A1 A2 A3 A4 A5 A6 P1 P2 P3 P4 P5 P6\n" ""; \
	for f in $$RID/*.jsonl; do basename "$$f" .jsonl; done \
	  | sed 's/__.*//' | sort -u | while read llm; do \
	  printf "%-22s " "$$llm"; \
	  for scen in D1 D2 D3 D4 D5 D6 A1 A2 A3 A4 A5 A6 P1 P2 P3 P4 P5 P6; do \
	    o=$$(tail -1 "$$RID/$${llm}__$${scen}.jsonl" 2>/dev/null | jq -r '.outcome' 2>/dev/null); \
	    case "$$o" in \
	      PASS) printf "✓  ";; \
	      FAIL) printf "✗  ";; \
	      RESET_FAIL) printf "R  ";; \
	      ADAPTER_ERROR) printf "A  ";; \
	      *) printf "?  ";; \
	    esac; \
	  done; \
	  echo ""; \
	done

# Per-cell pass-RATE grid: rows = LLMs, cols = scenarios, cell = P/K.
# K-aware version of show-grid: for K>1 multi-line JSONLs (each line =
# one trial), counts how many lines have outcome=PASS. Useful for the
# K=8 headline.
show-grid-rate:
	@RID="$(RID)"; \
	if [ -z "$$RID" ]; then \
	  RID=$$(ls -1d artefacts/2026* 2>/dev/null | sort -r | head -1); \
	  echo "(latest run: $${RID})"; \
	else \
	  RID="artefacts/$${RID}"; \
	fi; \
	printf "%-22s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s %-5s\n" \
	  "" D1 D2 D3 D4 D5 D6 A1 A2 A3 A4 A5 A6 P1 P2 P3 P4 P5 P6; \
	for f in $$RID/*.jsonl; do basename "$$f" .jsonl; done \
	  | sed 's/__.*//' | sort -u | while read llm; do \
	  printf "%-22s " "$$llm"; \
	  for scen in D1 D2 D3 D4 D5 D6 A1 A2 A3 A4 A5 A6 P1 P2 P3 P4 P5 P6; do \
	    file="$$RID/$${llm}__$${scen}.jsonl"; \
	    if [ ! -f "$$file" ]; then printf "%-5s " "—"; continue; fi; \
	    K=$$(wc -l < "$$file" | tr -d ' '); \
	    P=$$(grep -cE '"outcome"\s*:\s*"PASS"' "$$file" 2>/dev/null); \
	    if [ "$$K" = "0" ]; then printf "%-5s " "—"; \
	    else printf "%-5s " "$$P/$$K"; fi; \
	  done; \
	  echo ""; \
	done

# Show one trial's complete JSONL summary.
# Example: make show-trial RID=20260502T204921_postfix_v3 LLM=glm-4.7-flash SCEN=A5
show-trial:
	@if [ -z "$(RID)" ] || [ -z "$(LLM)" ] || [ -z "$(SCEN)" ]; then \
	  echo "ERROR: need RID=<run-id> LLM=<llm-name> SCEN=<scenario-id>"; \
	  echo "Example: make show-trial RID=20260502T204921_postfix_v3 LLM=glm-4.7-flash SCEN=A5"; \
	  exit 1; \
	fi
	@tail -1 "artefacts/$(RID)/$(LLM)__$(SCEN).jsonl" | jq '{outcome, oracle_diagnosis, oracle_mcp_pass, oracle_federation_pass, \
	      rounds_used, finish_reason, error: (.error // "none"), \
	      tool_calls_summary: [.tool_calls[] | {tool_name, succeeded, error: (.error // "ok")}], \
	      final_answer: (.final_answer // "" | tostring)}'

# Quick failure inspection: outcome + diag + truncated final_answer.
# Example: make inspect-fail RID=... LLM=glm-4.7-flash SCEN=A5
inspect-fail:
	@if [ -z "$(RID)" ] || [ -z "$(LLM)" ] || [ -z "$(SCEN)" ]; then \
	  echo "ERROR: need RID=<run-id> LLM=<llm-name> SCEN=<scenario-id>"; \
	  exit 1; \
	fi
	@tail -1 "artefacts/$(RID)/$(LLM)__$(SCEN).jsonl" | jq -r ' \
	  "outcome: " + .outcome, \
	  "diag:    " + (.oracle_diagnosis // ""), \
	  "rounds:  " + (.rounds_used | tostring), \
	  "tools:   " + ([.tool_calls[].tool_name] | join(",")), \
	  "ans:     " + ((.final_answer // "") | .[0:500]) \
	'

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache

# DESTRUCTIVE — removes all per-run artefacts. Confirm before invoking.
clean-artefacts:
	@read -p "Remove ALL benchmark artefacts (artefacts/*)? [y/N] " ans; \
	if [ "$$ans" = "y" ]; then \
	  rm -rf artefacts/*; \
	  echo "Cleaned."; \
	else \
	  echo "Aborted."; \
	fi
