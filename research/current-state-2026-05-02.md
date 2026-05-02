# PPAM 2026 benchmark — current state (2026-05-02)

Snapshot at end-of-session 2026-05-02. Captures the panel composition,
infrastructure state, K=1 results, and the queued work for whoever
picks up the harness next (paper-writing agent, future-self after
context compaction, or a peer reviewing the project).

## Panel composition (4 LLMs)

| LLM | Family | Endpoint | Open-weight | License |
|---|---|---|---|---|
| `claude-sonnet-4-5` | Anthropic | `claude-agent-sdk` (local CC binary) | ✗ | Proprietary |
| `qwen3.6-35b` | Alibaba | Cyfronet Forge (`Qwen/Qwen3.6-35B-A3B`) | ✓ | Apache-2.0 |
| `deepseek-v4-pro` | DeepSeek | OpenRouter → SiliconFlow pinned | ✓ | **MIT** |
| `glm-4.7-flash` | Z.ai | Cyfronet Forge (`zai-org/GLM-4.7-Flash`) | ✓ | MIT |

DeepSeek-V4 (released after my Jan-2026 cutoff): 1.6T / 49B MoE, 1M
context. Two SKUs (Flash 284B/13B + Pro 1.6T/49B), both plain MIT.
Discovered via OpenRouter probe + HF API verification.

Mistral-Small-4-119B-2603 candidate to be added when user has access
(would be 5th leg, EU-deployment narrative).

## Infrastructure state

### Per-LLM spaces (Onedata)

Each panel LLM has its own dedicated space on `data.spice-platform.eu`:

```
ppam_2026_mcp_tests_claude_sonnet_4_5  d3a48a8d428c9a8ac1ffee471a2d8bb3ch0d5f
ppam_2026_mcp_tests_qwen3_6_35b        b724a1f754a37c38dc0615cb079f651fchf8b3
ppam_2026_mcp_tests_glm_4_7_flash      028ebe59f7d722b86ca61ac87810c6a4ch8964
ppam_2026_mcp_tests_deepseek_v4_pro    bf2994a889b94e6720415b758e478fc6chd084
```

All supported by both `cloud-pl` + `Cloud-SK` providers (~100 MiB
allocation each). Created + provisioned via `setup_per_llm_spaces.py`
+ `support_per_llm_spaces.py` (idempotent — safe to re-run for new LLMs).

Legacy spaces retained in `_per_llm_spaces.py` registry for older
artefact compatibility: `deepseek-v3`, `qwen3-coder-30b`, `qwq-32b`.

### Harness wiring

```
benchmark/run_panel.py         CLI entry; --llms filter, --scenario-parallelism flag
benchmark/trial_runner.py      Per-trial orchestration; resolves per-LLM space; retry-with-backoff
benchmark/fixture_runner.py    prepare_trial(space_name, space_id) — federation reset
benchmark/_scenario_specialise.py  Rewrites brief + paths per LLM at trial dispatch time
benchmark/llm_adapters/
  openai_compat.py             For Forge + OpenRouter; forwards LLMConfig.extra as extra_body
  claude_agent_sdk.py          For Claude (no API key, local session auth)
benchmark/oracles/             Per-scenario verifiers (D1..P6); 18 scenarios across 3 bands
benchmark/report.py            Generates REPORT_paper.md + REPORT_cyfronet.md from artefacts
benchmark/setup_per_llm_spaces.py    Onedata space provisioner
benchmark/support_per_llm_spaces.py  Provider-side support attacher
Makefile                       All operations behind `make help`
```

### Sweep outputs

```
artefacts/<run_id>/<llm>__<scenario>.jsonl   per-trial records
artefacts/<run_id>/REPORT_paper.md           compact pass-rate table
artefacts/<run_id>/REPORT_cyfronet.md        per-model diagnostic + Forge gaps
```

`artefacts/` is gitignored. Reports must be moved to `research/` or
`docs/` if they should ship with the repo.

## K=1 results (latest, run `20260502T145805` post fixes)

After applying:
- M-1..M-8 MCP server design fixes (per `empirical-mcp-server-findings.md`)
- Oracle D3 `\n` normalisation
- Retry-with-backoff in `_prepare_with_retry` (3 retries, exponential 1s/2s/4s)

```
| LLM                | Pass rate    | Notes                                         |
|--------------------|--------------|-----------------------------------------------|
| claude-sonnet-4-5  | 17/18 (94%)  | Only fail: D3 size off-by-one                 |
| qwen3.6-35b        | 17/18 (94%)  | Only fail: P3 (real reasoning gap)            |
| deepseek-v3        | 13/18 (72%)  | (pre-V4-swap; V4-pro sweep pending)           |
| glm-4.7-flash      | 12/18 (67%)  | Real reasoning gaps in A2/A4/D2/D5/P3/P6      |
```

**Total RESET_FAIL: 0/72** (the retry-with-backoff is doing its job).

DeepSeek-V4-pro swap committed (29eeed7) but **NOT YET RUN**. Expected
to land 14-17/18 in priors but actual TBD.

## Pending workstreams

1. **Run V4-pro sweep** to honestly measure post-swap. `make sweep-deepseek` (~10-15 min, single LLM).
2. **K=8 headline run** — `make sweep-k8` is wired, ~3-4 hours wall.
   Two-phase: Cyfronet+Anthropic parallel, DeepSeek serial.
3. **Add Mistral-Small-4-119B-2603** when user provisions access.
4. **#22 pass^k aggregator** — `report.py` has K=1 reports; needs
   `--aggregate-k` mode for K=8 headline table with pass^k columns.
5. **#23 -spice-v1 onezone patch verification** (open).
6. **#24 federation health** — 2/5 OPs unreachable (informational).
7. **M-10 / M-11 / M-12 fleet-wide pass-rate fixes** — APPLIED 2026-05-02
   (see `research/empirical-mcp-server-findings.md`). Three low-LOC
   wrapper changes lift fleet pass rate on D3 (size out-of-band),
   A4 (`create_directory` tool + `create_parents=True` default),
   D2 (relative-prefix docstring). Surfaced by V3 K=1 diagnostic;
   net-new vs M-1..M-9. Re-run V4-pro K=1 to measure post-fix delta.

## Cost notes (DeepSeek via OpenRouter)

K=1 (18 scenarios, ~580K input + 18K output, paid via SiliconFlow):
- DeepSeek-V4-pro: ~$0.27/sweep ($0.435/M in × 0.58M = $0.25)
- DeepSeek-V3 (legacy): ~$0.09/sweep

K=8: ~$2/sweep for V4-pro. Several full headline runs cost <$10.

## Key documents (this directory)

- `current-state-2026-05-02.md` — this file
- `empirical-mcp-server-findings.md` — 9 MCP-server design issues with fixes (M-1..M-9)
- `empirical-onedata-25.0-findings.md` — 19+ Onedata-side empirical observations
- `scenario-catalogue.md` — full reference for all 18 scenarios (prompts + pass/fail)

## How to resume

```bash
cd ~/repos/onedata/spice/onedata-mcp-ppam2026
git status && git log --oneline -10        # commit 29eeed7 is the most recent
make test                                  # confirm 109/109 pass
make spaces-status 2>/dev/null || make spaces-support  # confirm spaces are healthy
make smoke                                 # D1+P1 across the panel — ~3 min sanity check
make sweep-all                             # K=1 across the 4-LLM panel — ~35 min
```

For the headline:
```bash
make sweep-k8     # K=8 across the full panel — ~3-4 hours
```

The harness is in a known-good state. The next high-value action is
either (a) run the V4-pro K=1 sweep to confirm the swap is healthy,
then (b) launch the K=8 headline.
