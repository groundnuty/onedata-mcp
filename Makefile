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

.PHONY: help install test test-file test-verbose test-readiness-live test-qos-live tools-manifest \
        lint format check \
        spaces-create spaces-support spaces-status \
        smoke smoke-d1 smoke-fast \
        sweep sweep-cyfronet sweep-deepseek sweep-claude sweep-all \
        sweep-headline sweep-k8 \
        k2val k2val-smoke k2val-rejudge k2val-scan k2val-jail-probe k2val-jail-smoke \
        test-k2val-jail-live \
        valfed-up valfed-reset valfed-down valfed-status valfed-check valfed-readiness-live \
        valfed-qos-live valfed-transfer-live test-transfer-live \
        test-files-live valfed-files-live \
        conformance inspect-smoke \
        report report-latest rescore \
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
	@echo "    tools-manifest     print tools/list as sorted JSON, no network (ROOT=<checkout>"
	@echo "                       to import another checkout; set ONEDATA_MCP_TOOLS to pick tools)"
	@echo "    test-readiness-live  readiness tools against the LOCAL Compose federation only"
	@echo "    test-qos-live      remove_qos_requirement against the LOCAL Compose federation only"
	@echo "    test-transfer-live schedule_file_replication against the LOCAL Compose federation only"
	@echo "    test-files-live    get_file_attributes, list_children, create_directory, LOCAL only"
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
	@echo "  K2 validation panel  (design/08; LOCAL Compose federation only)"
	@echo "    k2val              run the panel. Needs benchmark/k2val/k2val.toml. Examples:"
	@echo "      make k2val                                  all configs, V1..V7, K=3"
	@echo "      make k2val K2VAL_MODELS=sonnet-medium,haiku-low K2VAL_SCENARIOS=V2,V5 K2VAL_K=1"
	@echo "                         (K2VAL_CONFIG=... K2VAL_RID=<run id>; an id that has artefacts is refused)"
	@echo "      make k2val K2VAL_RID=<run id> K2VAL_RESUME=1   continue that run: a trial with a valid"
	@echo "                       record is skipped, one that ended in RESET_FAIL, HARNESS_ERROR,"
	@echo "                       ORACLE_ERROR or ADAPTER_ERROR runs again (records are appended)"
	@echo "                       Each answer is read by its deterministic reader AND a rubric judge"
	@echo "                       ([judge], default claude-sonnet-5-5 at effort medium, on the"
	@echo "                       subscription); a disagreement is NEEDS_REVIEW (design/08)"
	@echo "    k2val-smoke        the stable tools' smoke re-run (design/10, design/16): the smoke"
	@echo "                       pair at K=3 on K2VAL_SMOKE_SCENARIOS (K2VAL_CONFIG=...)"
	@echo "    k2val-rejudge      read a run's stored answers again, offline: RID=<run id> (or"
	@echo "                       K2VAL_RUN_DIRS=\"dir1 dir2\"); cached verdicts are reused; no trial runs"
	@echo "    k2val-scan         pre-flight scan for the CLI legs (wave 2; K2VAL_MODELS=gpt-luna-low,..)"
	@echo "    k2val-jail-probe   leak probe of the CLI jail: PASS/FAIL table (K2VAL_JAIL_CLIS=codex,agy)"
	@echo "    k2val-jail-smoke   one harmless prompt per CLI in the jail, no MCP (after the probe)"
	@echo "    test-k2val-jail-live  the leak probe as a pytest (K2VAL_JAIL_LIVE=1)"
	@echo ""
	@echo "  Validation federation  (design/10; Onedata 25.0 in Compose on 127.0.0.1; VALFED_HOME=<dir>)"
	@echo "    valfed-up          bring it up (or check it), cap CPUs, mint tokens, write"
	@echo "                       benchmark/k2val/k2val.valfed.toml (VALFED_CORE_REPO, VALFED_KEY_DIR)"
	@echo "    valfed-reset       empty every space; ids, users and tokens stay"
	@echo "    valfed-down        remove it, its data and its tokens"
	@echo "    valfed-status      containers, CPU and memory caps, peak memory, host load"
	@echo "    valfed-check       no-model check: panel fixtures built and torn down in omcp-smoke"
	@echo "    valfed-readiness-live  the gated live readiness test, in omcp-live"
	@echo "    valfed-qos-live    the gated live test of remove_qos_requirement, in omcp-live"
	@echo "    valfed-transfer-live  the gated live test of schedule_file_replication, in omcp-live"
	@echo "    valfed-files-live  the gated live test of the three file tools, in omcp-live"
	@echo ""
	@echo "  MCP-protocol validation  (paper-grade artifacts)"
	@echo "    conformance        run modelcontextprotocol/conformance suite 0.1.16 (pinned)"
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

# Print the server's tools/list as sorted JSON (name, description, inputSchema, annotations),
# with no network. Run it on two checkouts and diff to see what a change did to the surface:
#   ONEDATA_MCP_TOOLS=a,b make tools-manifest ROOT=/path/to/other/checkout
tools-manifest:
	uv run python scripts/tools_manifest.py $(ROOT)

# Live test of check_readiness / ensure_readiness. It writes a file and a QoS rule, and
# refuses any host but 127.0.0.1, localhost or *.datahub.test. Needs the usual ONEDATA_*
# variables and ONEDATA_MCP_LIVE_READINESS_SPACE (see the test's docstring).
test-readiness-live:
	ONEDATA_MCP_LIVE_READINESS=1 uv run pytest test/live/test_readiness_live.py -v -s

# Live test of remove_qos_requirement (add, remove, gone from the summary; an unknown id's
# error). It writes a file and QoS requirements, and refuses any host but 127.0.0.1, localhost
# or *.datahub.test. Needs the usual ONEDATA_* variables and ONEDATA_MCP_LIVE_QOS_SPACE.
test-qos-live:
	ONEDATA_MCP_LIVE_QOS=1 uv run pytest test/live/test_qos_live.py -v -s

# Live test of schedule_file_replication (a copy to the local provider, judged by its block map;
# an unknown target; a provider name as the id). It writes files through a SECOND provider and
# schedules transfers, and refuses any host but 127.0.0.1, localhost or *.datahub.test. Needs the
# usual ONEDATA_* variables, ONEDATA_MCP_LIVE_TRANSFER_SPACE, and the source provider's
# ONEDATA_MCP_LIVE_TRANSFER_SOURCE_HOST and ONEDATA_MCP_LIVE_TRANSFER_SOURCE_TOKEN.
test-transfer-live:
	ONEDATA_MCP_LIVE_TRANSFER=1 uv run pytest test/live/test_transfer_live.py -v -s

# Live test of get_file_attributes, list_children and create_directory (design/13-15). It
# writes files and directories, and refuses any host but 127.0.0.1, localhost or
# *.datahub.test. Needs the usual ONEDATA_* variables and ONEDATA_MCP_LIVE_FILES_SPACE.
test-files-live:
	ONEDATA_MCP_LIVE_FILES=1 uv run pytest test/live/test_files_live.py -v -s

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
	  echo "  Order: free legs first, V4-pro LAST (rate-limit-sensitive)"; \
	  echo "  Started: $$(date -u +%FT%TZ)"; \
	  echo "  Logs: $$LOG  +  $$PERSTEP/<step>.log"; \
	  echo "============================================================"; \
	  echo ""; \
	  \
	  echo "=== Step 1: Sonnet K=8 (Anthropic, claude-agent-sdk) ==="; \
	  uv run python -m benchmark.run_panel --trials 8 \
	    --llms claude-sonnet-4-5 \
	    --scenario-parallelism 1 --run-id "$$RID" 2>&1 \
	    | tee "$$PERSTEP/01-sonnet.log"; \
	  echo ""; \
	  \
	  echo "=== Step 2: Cyfronet Forge K=8 (Qwen, GLM — sequential) ==="; \
	  N=2; \
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
	  echo "=== Step 3: Local vLLM K=8 (Gemma, Granite, Devstral — sequential) ==="; \
	  N=4; \
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
	  echo "=== Step 4: V4-pro K=1 (rate-limit probe) ==="; \
	  uv run python -m benchmark.run_panel --trials 1 \
	    --llms deepseek-v4-pro \
	    --scenario-parallelism 1 --run-id "$$RID" 2>&1 \
	    | tee "$$PERSTEP/07-v4pro-probe.log"; \
	  RL_HITS=$$(grep -lE "RateLimitError" artefacts/$$RID/deepseek-v4-pro__*.jsonl 2>/dev/null | wc -l | tr -d ' '); \
	  if [ "$$RL_HITS" -gt 0 ]; then \
	    echo ""; \
	    echo "!!! V4-pro probe: $$RL_HITS cells hit RateLimitError after retry exhaustion."; \
	    echo "    Skipping V4-pro K=7 expansion. Other 6 LLMs already complete (K=8)."; \
	    echo "    Generating reports for what we have."; \
	  else \
	    echo ""; \
	    echo "=== Step 5: V4-pro K=7 more (target K=8) ==="; \
	    uv run python -m benchmark.run_panel --trials 7 \
	      --llms deepseek-v4-pro \
	      --scenario-parallelism 1 --run-id "$$RID" 2>&1 \
	      | tee "$$PERSTEP/08-v4pro-rest.log"; \
	  fi; \
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
# K2 validation panel (design/08)
# ---------------------------------------------------------------------------
#
# Runs against the LOCAL Compose federation only: the panel refuses any host but 127.0.0.1,
# localhost or *.datahub.test. Config: benchmark/k2val/k2val.toml (copy the .example and set
# the token and key file paths; it is gitignored). Artefacts: artefacts/k2val/<run id>/
# (gitignored); the report: research/k2val-<run id>.md.
K2VAL_CONFIG ?= benchmark/k2val/k2val.toml
K2VAL_MODELS ?=
K2VAL_SCENARIOS ?= V1..V7
K2VAL_K ?= 3
K2VAL_RID ?=
K2VAL_RESUME ?=

RID ?=
K2VAL_RUN_DIRS ?= $(if $(RID),artefacts/k2val/$(RID))

k2val-rejudge:
	@test -n "$(K2VAL_RUN_DIRS)" || { echo "set RID=<run id> or K2VAL_RUN_DIRS=..."; exit 2; }
	uv run python -m benchmark.k2val.run rejudge \
	  $(foreach d,$(K2VAL_RUN_DIRS),--run-dir $(d)) \
	  $(if $(wildcard $(K2VAL_CONFIG)),--config $(K2VAL_CONFIG))

k2val:
	uv run python -m benchmark.k2val.run --config $(K2VAL_CONFIG) \
	  $(if $(K2VAL_MODELS),--models $(K2VAL_MODELS)) \
	  --scenarios $(K2VAL_SCENARIOS) --k $(K2VAL_K) \
	  $(if $(K2VAL_RID),--run-id $(K2VAL_RID)) \
	  $(if $(K2VAL_RESUME),--resume)

# The smoke re-run that design/10 ("Staying stable") asks of a stable tool whose description,
# error hints or result shape changed: the smoke pair (Qwen3-Coder, Haiku at effort low) at K=3
# on every scenario design/16 maps to the stable tools (benchmark/k2val/stable_scenarios.py's
# SMOKE_SCENARIOS; a test keeps the two equal). One tool's scenarios: set K2VAL_SMOKE_SCENARIOS.
K2VAL_SMOKE_SCENARIOS ?= V1,V3,V3n,V3o,SR1,SR1i,SR1n,ST1,ST2,ST3,ST4,ST4i

k2val-smoke:
	$(MAKE) k2val K2VAL_MODELS=qwencoder,haiku-low K2VAL_K=3 \
	  K2VAL_SCENARIOS=$(K2VAL_SMOKE_SCENARIOS)

k2val-scan:
	uv run python -m benchmark.k2val.run scan --config $(K2VAL_CONFIG) \
	  $(if $(K2VAL_MODELS),--models $(K2VAL_MODELS)) \
	  $(if $(K2VAL_RID),--run-id $(K2VAL_RID))

# The CLI jail (design/08, "The CLI jail"). The probe runs no CLI and sends no prompt; it needs
# bwrap, slirp4netns and nft, and the CLIs' logins under $HOME. The smoke sends ONE fixed,
# harmless prompt per CLI ("Reply with the single word OK.") with no MCP server attached.
K2VAL_JAIL_CLIS ?= codex,agy
K2VAL_JAIL ?= benchmark/k2val/jail/cli-jail.sh
comma := ,

k2val-jail-probe:
	uv run benchmark/k2val/jail/leak-probe.sh \
	  $(foreach c,$(subst $(comma), ,$(K2VAL_JAIL_CLIS)),--cli $(c)) \
	  $(if $(wildcard $(K2VAL_CONFIG)),--config $(K2VAL_CONFIG))

k2val-jail-smoke:
	uv run python -m benchmark.k2val.run jail-smoke --config $(K2VAL_CONFIG) \
	  --jail $(K2VAL_JAIL) $(if $(K2VAL_MODELS),--models $(K2VAL_MODELS)) </dev/null

test-k2val-jail-live:
	K2VAL_JAIL_LIVE=1 K2VAL_CONFIG=$(K2VAL_CONFIG) uv run pytest test/live/test_k2val_jail_live.py -v -s

# ---------------------------------------------------------------------------
# Validation federation (design/10, "Test environment")
# ---------------------------------------------------------------------------
#
# The project's own two-provider Onedata 25.0 federation in Docker Compose, on 127.0.0.1 only:
# benchmark/valfed/layout.toml, driven by benchmark/valfed/valfed.py (its docstring has the
# details). Needs Docker and VALFED_HOME, an existing directory with room for all its data.
# Run the panel on it with the SSL_CERT_FILE line at the top of benchmark/k2val/k2val.valfed.toml.
VALFED = uv run python -m benchmark.valfed.valfed

valfed-up:
	$(VALFED) up

valfed-reset:
	$(VALFED) reset

valfed-down:
	$(VALFED) down

valfed-status:
	$(VALFED) status

valfed-check:
	$(VALFED) check

valfed-readiness-live:
	$(VALFED) readiness-live

valfed-qos-live:
	$(VALFED) qos-live

valfed-transfer-live:
	$(VALFED) transfer-live

valfed-files-live:
	$(VALFED) files-live

# ---------------------------------------------------------------------------
# MCP-protocol validation
# ---------------------------------------------------------------------------
#
# These targets spawn onedata-mcp on a temporary HTTP transport (via
# scripts/with-http-server.sh), run an external validator against it,
# and tear the server down. Both produce paper-citable artifacts:
#
#   conformance     — modelcontextprotocol/conformance 0.1.16 (pinned: a
#                     new suite version may add scenarios the baseline
#                     does not list, so a bump is a deliberate change)
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
	  npx -y @modelcontextprotocol/conformance@0.1.16 server \
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

# Re-evaluate mcp_pass for every trial in a saved run against the
# CURRENT oracle code. Writes <llm>__<scenario>.rescored.jsonl
# sidecars. Originals are preserved unchanged.
#
# Use case: parser bug fix lands AFTER a sweep — rescore lifts
# false-negative trials (model was right, oracle missed it) to PASS
# without re-running the LLMs. federation_pass is preserved from the
# original (federation state is gone post-sweep).
#
# Example: make rescore RID=20260503T002305_k8
rescore:
	@if [ -z "$(RID)" ]; then \
	  echo "ERROR: pass RID=<run-id>.  e.g. make rescore RID=20260503T002305_k8"; \
	  exit 1; \
	fi
	uv run python -m benchmark.rescore --run-id $(RID)

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
