# PPAM 2026 benchmark — operations entry points.
#
# All commands assume `uv` is on PATH. Run `make help` for the full list.

.PHONY: help install test lint format check \
        spaces-create spaces-support spaces-status \
        smoke smoke-d1 smoke-fast \
        sweep-cyfronet sweep-deepseek sweep-claude sweep-all \
        sweep-headline sweep-k8 \
        report report-latest \
        clean clean-artefacts

# Default target prints the command list.
help:
	@echo "PPAM 2026 benchmark — Make targets"
	@echo ""
	@echo "  Setup"
	@echo "    install         pip install the MCP server in editable mode"
	@echo "    test            run unit tests (109/109 expected)"
	@echo "    lint            ruff check (auto-fix)"
	@echo "    format          ruff format"
	@echo "    check           lint + format + test (pre-commit gate)"
	@echo ""
	@echo "  Federation provisioning  (run once per panel-LLM addition)"
	@echo "    spaces-create   create per-LLM Onedata spaces (idempotent)"
	@echo "    spaces-support  attach providers to per-LLM spaces (idempotent)"
	@echo "    spaces-status   list current spaces + their support state"
	@echo ""
	@echo "  Smokes  (cheap, single-trial verification)"
	@echo "    smoke-d1        D1 only across the panel (~1 min)"
	@echo "    smoke           D1+P1 across the panel (~3 min)"
	@echo "    smoke-fast      D-band only across the panel (~6 min)"
	@echo ""
	@echo "  K=1 sweeps  (full 18 scenarios, single trial)"
	@echo "    sweep-cyfronet  Cyfronet+Anthropic legs in parallel (~25 min)"
	@echo "    sweep-deepseek  DeepSeek leg serial via OpenRouter (~10 min)"
	@echo "    sweep-claude    Claude only (~25 min)"
	@echo "    sweep-all       Two-phase: Cyfronet+Anthropic parallel,"
	@echo "                    DeepSeek serial (~35 min, single run-id)"
	@echo ""
	@echo "  Headline runs"
	@echo "    sweep-k8        K=8 across the full panel (~3-4 hours)"
	@echo "    sweep-headline  alias for sweep-k8"
	@echo ""
	@echo "  Reporting"
	@echo "    report-latest   regenerate REPORT_paper.md + REPORT_cyfronet.md"
	@echo "                    for the most recent artefact run"
	@echo "    report          alias for report-latest"
	@echo ""
	@echo "  Housekeeping"
	@echo "    clean           remove pyc, __pycache__, build artefacts"
	@echo "    clean-artefacts remove all run artefacts (DESTRUCTIVE)"

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

install:
	uv pip install -e .

test:
	uv run pytest test/unit -q

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

# Headline K=8 run — same two-phase structure as sweep-all but K=8 trials
# per cell. Estimated 3-4 hours wall. Re-run only after harness is locked.
sweep-k8:
	@RID=$$(date -u +%Y%m%dT%H%M%S)_k8; \
	echo "Shared run_id: $$RID  (K=8 headline)"; \
	echo "===PHASE 1: Cyfronet + Anthropic (parallel=2)==="; \
	uv run python -m benchmark.run_panel --trials 8 \
	  --llms claude-sonnet-4-5,qwen3.6-35b,glm-4.7-flash \
	  --scenario-parallelism 2 --run-id "$$RID"; \
	echo ""; \
	echo "===PHASE 2: OpenRouter (serial)==="; \
	uv run python -m benchmark.run_panel --trials 8 \
	  --llms deepseek-v4-pro \
	  --scenario-parallelism 1 --run-id "$$RID"; \
	echo ""; \
	echo "===Generating reports==="; \
	uv run python -m benchmark.report --run-id "$$RID"

sweep-headline: sweep-k8

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

report-latest:
	uv run python -m benchmark.report

report: report-latest

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
