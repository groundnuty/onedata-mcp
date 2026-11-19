# Validation panel for the not-yet-validated tools (PPAM protocol, small)

Status: spec for implementation (2026-11-19). Branch `feat/readiness-validation-panel`, off `feat/k2-readiness`.

## Why

PPAM 2026 validated the HEADLINE-16 against a 7-LLM panel (K=8). A federated-data agent study also exposes tools that
never went through such a panel:
- `check_readiness` and `ensure_readiness` (new, K2);
- `get_file_attributes`, inherited upstream and never swept;
- `schedule_file_replication` and `remove_qos_requirement`, added after the PPAM freeze.

This panel applies the PPAM *protocol*, not the full evaluation, to those five tools. The protocol:
- scripted scenarios with a pre-state fixture;
- a two-axis oracle (answer, federation end-state);
- one space per model;
- K repetitions;
- failures sorted into harness bug / deployment artefact / model failure before anything counts.
The goal is to find wording that weak models misread, arguments they get wrong, and real bugs. It does not measure the
agent study's outcome.

## Guards (agreed with the coordinator; binding)

1. **Synthetic data only.** Neutral prompts: never the agent study's prompts, and nothing from its own domain. The
   domain here is "instrument logs".
2. Findings may change **understanding only**: misread wording, wrong arguments, bugs. Never workflow advice. An agent
   that computes too early is not a description to fix.
3. **Record everything.** Each trial's artefact; each change with its triggering trial, and a before/after of each
   description or answer; per-model results. All of it goes in `artefacts/k2val/<run_id>/` (gitignored), plus a
   committed summary `research/k2val-<run_id>.md`.
4. **Concurrency:** at most 2 trials at once per model. **Anthropic spend** (the Haiku configs and the Sonnet control)
   is tracked and recorded per call and per run, in one shared tally. The cap is a config value (`[spend] cap_usd`,
   default $50; the operator lifted the earlier $5 Haiku cap). Refuse to start an Anthropic trial once the run's spend
   plus a $0.50 reserve would exceed the cap, and never make a call once the spend has reached it.
5. K1 tools: findings come out as a report only. No code change here.

## Federation (a local Compose federation; one scratch space per config)

- Spaces, one per config: `k2val-glm`, `k2val-qwen36`, `k2val-qwencoder`, `k2val-deepseek`, `k2val-haiku-low`,
  `k2val-haiku-medium`, `k2val-haiku-high`, `k2val-sonnet`, and for wave 2 `k2val-gemini` and `k2val-gpt`. Plus
  `k2-live` (manual live checks, not used by the panel).
- Each space is owned by `nordic-marine-dc` (the *owner*) and supported by `vistula-dc` (the *local* provider, the
  agent's site).
- Config: `benchmark/k2val/k2val.toml.example` (committed) and `k2val.toml` (gitignored). It holds:
  - the Onezone host;
  - the local provider host and its token-file path;
  - the owner provider host and its token-file path;
  - the space name per config;
  - the two provider names;
  - each config's settings (below);
  - the key-file paths;
  - the Anthropic spend cap (`[spend] cap_usd`, default 50);
  - for wave 2, `[cli.jail] wrapper` and `[cli.scan]` (below).
- **Read tokens and keys from files at run time; never log, print or persist them.** Mask them in any error text.
- **Refuse to run** unless every federation host (zone and providers) is `127.0.0.1`, `localhost` or ends in
  `.datahub.test`.

## Models (as the agent study serves them)

Wave 1 is the eight API configs, and they must work end to end first. Wave 2 is the two CLI agent legs, disabled by
default.

| name | backend | api_id | sampling / extra |
|---|---|---|---|
| sonnet-medium (control) | Anthropic Messages API, key file `anthropic.key` | `claude-sonnet-5-5` | no sampling params (Sonnet 5.5 rejects them); `thinking = {type: "adaptive"}` (disabled is rejected); `output_config = {effort: "medium"}`. A capable reference that should pass every scenario |
| glm | openai_compat, base `https://llmlab.plgrid.pl/api/v1`, key file `lk` | `zai-org/GLM-5.3-Flash` | temperature 1.0, top_p 0.95; nothing else (reasoning effort stays at the server default) |
| qwen36 | openai_compat, same base and key | `Qwen/Qwen3.6-35B-A3B` | PPAM's settings: temperature 1.0, no other sampling fields |
| qwencoder | openai_compat, same base and key | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | temperature 0.7, top_p 0.8, top_k 20, repetition_penalty 1.05 (top_k and repetition_penalty go in `extra_body`, as vLLM takes them); non-thinking model |
| deepseek | openai_compat, same base and key | `deepseek-ai/DeepSeek-V4-Flash-0731` | temperature 1.0, top_p 0.95; nothing else (reasoning at the server default). Chosen because in an earlier study's traces it read "incomplete" as "absent" and claimed done too early, the failure modes V5 and V7 probe |
| haiku-low, haiku-medium, haiku-high | Anthropic Messages API, key file `anthropic.key` | `claude-haiku-5-5` | the same model at three efforts: `output_config = {effort: "low" / "medium" / "high"}`; no sampling params; `thinking = {type: "adaptive"}`; price tiers ≤200k prompt in/out $0.10/$0.50 per Mtok, above $0.50/$2.50 |
| gpt-luna-low (wave 2) | Codex CLI 0.169, `codex exec --json --ephemeral -s read-only` | `gpt-5.6-luna` | `-c model_reasoning_effort=low` |
| gemini-flash-low (wave 2) | Antigravity CLI `agy --print --output-format stream-json --sandbox` | `gemini-3.8-flash-low` | `--effort low` |

- The openai_compat adapter exists (`benchmark/llm_adapters/openai_compat.py`). Extend it only as needed to pass
  `top_p`, `top_k`, `repetition_penalty` and `extra_body`. It already echoes `reasoning_content` back in the assistant
  turn when a model returns it. Keep current behaviour as the default.
- Add `benchmark/llm_adapters/anthropic_api.py`: a minimal Messages-API tool loop. Add the `anthropic` package to the
  **dev** dependency group only; the server must not depend on it. Follow the official SDK docs for tool use,
  adaptive thinking and effort, and verify the parameter names against the docs, not from memory. Thinking blocks
  must be passed back unchanged in the next assistant turn, as the API requires. Accepted effort values are `low`,
  `medium`, `high`, `xhigh` and `max` (SDK `OutputConfigParam`).
- Anthropic spend (the Haiku configs and the Sonnet control together) is tracked per call and per run; see Guards.
- Key files: `<key dir>/lk` and `<key dir>/anthropic.key`.
- **CLI legs (wave 2).** `gpt-luna-low` and `gemini-flash-low` drive the real agent CLIs, which send everything they see
  to OpenAI or Google. They are disabled by default and run only when ALL of these hold; otherwise the run skips them,
  says so, and continues with the API configs. They never run unjailed.
  - `[cli.jail] wrapper` is set in `k2val.toml`. The CLI is started through one function,
    `jail_cmd(argv, workdir) -> list[str]`: a bwrap jail with a fresh empty home, a minimal env, an empty work dir, and
    no `/mnt`, `/Users`, repo, token or key paths visible. The jail itself is built separately.
  - A clean `k2val scan` record exists for the config. The scan collects sample tool answers through the same server
    (`list_space_providers`, `get_file_distribution`, `check_readiness` on a fixture), the tool definitions and the
    prompts, and checks them against a token list file (one token per line, case-insensitive substring) and a regex
    patterns file. It prints only match counts and categories, never the matched text, and writes a record to the run
    directory.
  - The MCP server for a CLI leg runs OUTSIDE the jail, as an HTTP server on `127.0.0.1` (an ephemeral port), started by
    the harness with the token in its own env. The CLI gets only the loopback URL (codex
    `-c mcp_servers.onedata.url=...`; agy `--settings` with an http `mcpServers` entry). The user's global codex and agy
    config is never modified. The trace separates MCP tool calls from the CLI's built-in tool calls and reports both.

## Tools offered per scenario (as the agent study's sets)

- K1 set: `add_file_qos_requirement, get_file_attributes, get_file_distribution, get_file_metadata,
  get_file_qos_summary, get_qos_requirement, get_transfer, list_space_providers, list_space_transfers,
  remove_qos_requirement, schedule_file_replication`.
- K2 set: the K1 set plus `check_readiness, ensure_readiness`.
- The MCP app is the in-process server from this branch, as in the PPAM harness. Pass the allow-list like PPAM's
  `allowed_tools`.

## System prompt (neutral, the same for every scenario)

"You are an assistant with access to tools of a federated data management system (Onedata). Your site is the provider
`vistula-dc`. Use the tools to complete the task, then give a short final answer."

The agent study's sentences are not used.

## Fixtures

All files live under `/<space>/logs/`. They are synthetic 256 KiB files named `log_YYYYMMDD.dat`, with random bytes
from a fixed seed per file.

- **At owner only (S0):** write the file through the OWNER provider's REST, so its blocks are at nordic-marine-dc and
  none are local.
  - Create: `POST /data/{parentId}/children?name=...&type=REG`.
  - Write: `PUT /data/{id}/content`, with the whole body and no offset (this truncates to 0 first, which is fine for
    a new file).
  - Confirm with the owner's `/distribution` that the owner covers the file and the local provider holds 0 bytes.
- **Being written (S2), mirroring the preallocated mode of the reference ingest tool** (its `preallocate` and
  `ingestsparse` steps):
  1. Create the file at the owner.
  2. Extend it with an empty write at its final size: `PUT /data/{id}/content?offset=<size>`, empty body. On 25.0 this
     sets the size and writes no block.
  3. Write the prefix at offset 0.
  4. A background writer then writes the rest in parts at their offsets on a fixed schedule from the trial's first
     model turn.
  - Never write without an offset.
  - Confirm each state on the owner's distribution before going on.
- **Locally replicated:** after an S0 fixture, replicate to the local provider with a transfer, and confirm local
  coverage.
- **QoS rule present:** add `providerId=<local id>` on the file, and wait until it is fulfilled and locally covered.
- **Reset** before each trial, in the model's own space:
  - delete every QoS requirement on the space's files;
  - delete `/logs/`;
  - wait until the owner answers the old ids absent;
  - then build the fixture.
  - Delete-then-recreate under the same names can race on 25.0, so use a fresh subdirectory per trial,
    `/logs-<trial_id>/`, and put that path in the brief.

## Scenarios (7)

Each has a brief, a fixture, the tool set, and a two-axis oracle. `mcp_pass` = the answer is right (judged by string
and regex rules on the final answer, as in PPAM). `federation_pass` = the end-state is right, or `None` where nothing
should change. The briefs name the directory and the dates, and everything the agent cannot discover with the tools
offered (V1 names its six files, as the K1 set has no listing tool); they never name a tool.

1. **V1 attributes** (K1). Six S0 files; two of them also locally replicated.
   - Brief: "Which of these files in `<dir>` are fully stored at your site vistula-dc: log_20200101.dat, ...,
     log_20200106.dat? List the names of those that are." (all six names spelled out)
   - mcp: exactly the two names, no others.
   - federation: None.
2. **V2 replicate** (K1). One S0 file.
   - Brief: "Copy `<dir>/log_20200105.dat` to your site vistula-dc and tell me when its data are fully there."
   - federation: local covers the file at the end of the trial.
   - mcp: the answer says it is there AND the local copy covered the file when the answer was given. Read the
     distribution immediately after the final turn; a "done" with local not yet covering is a FAIL, "premature
     claim".
3. **V3 remove rule** (K1). One S0 file with a QoS rule present.
   - Brief: "A placement rule keeps `<dir>/log_20200107.dat` at vistula-dc. Remove that rule."
   - federation: the file has no QoS requirement.
   - mcp: the answer says it was removed.
4. **V4 check, remote** (K2). Five S0 files, 2020-01-01..05.
   - Brief: "Are the log files in `<dir>` for 1–5 January 2020 complete and readable at your site? If not, why not?"
   - mcp: the answer says not ready, with a reason meaning "at another site/provider", and never claims ready.
   - federation: None.
5. **V5 check, arriving** (K2). Five files, 2020-01-01..05: three S0, two S2 whose writing finishes 20 minutes after
   the first turn.
   - Same brief as V4.
   - mcp: not ready, with a reason meaning "still being written", naming or counting the two files. A plain "not
     ready" without the reason is a FAIL.
   - federation: None.
6. **V6 ensure, remote** (K2). Five S0 files.
   - Brief: "Make the log files in `<dir>` for 1–5 January 2020 complete and readable at your site, and tell me when
     they are."
   - federation: all five locally covered at the end.
   - mcp: the answer says ready, and the local copy covered all five when it was given (else "premature claim").
7. **V7 ensure, arriving** (K2). Five files: three S0, two S2 whose writing finishes 6 minutes after the first turn.
   That is longer than one 240 s `ensure_readiness` call, so the agent must call again.
   - Same brief as V6.
   - federation: all five covered locally and complete.
   - mcp: the answer says ready, given after the writer finished and after local coverage; else "premature claim".
   - Also record, from the trace, whether any `ensure_readiness` answer said `ready: true` before the writer finished.
     That would be a K2 violation, a tool bug, reported separately in large letters.

## Runner and report

- New: `python -m benchmark.k2val.run --config k2val.toml --models glm,qwen36,qwencoder,haiku --scenarios V1..V7 --k 3`, and a
  `make k2val` target (make is the interface). It reuses `benchmark/_runtime_types.py` (OracleResult, ToolCall,
  AgentTrace) and the artefact pattern of `benchmark/_trial_artefact.py`. It does not change the PPAM harness's
  behaviour; the PPAM tests must still pass.
- Per trial, record:
  - the full trace (every tool call with its arguments, answer and timing);
  - the final answer;
  - both oracle axes and the diagnosis;
  - wall time and tokens;
  - Haiku cost;
  - the serving fingerprint if the API returns one (`system_fingerprint`).
- Limits: max 40 tool rounds and a 20 min wall cap per trial; a cap reached counts as no result.
- Report `research/k2val-<run_id>.md`:
  - a pass^k table per model × scenario;
  - every FAIL with its diagnosis and the two or three trace lines that show why;
  - the tool-call sequences per scenario;
  - wrong arguments;
  - errors returned by tools.

  It must not count anything yet: leave a column for the manual three-layer classification. The pass^k figures are raw
  and unclassified.
  - The three Haiku configs sit side by side, so the effort comparison reads directly.
  - `sonnet-medium` is the control: it comes first, as a control column. Every scenario where the control fails (any
    trial that is not a pass) is flagged **DESIGN SUSPECT** (a wrong tool, scenario or oracle), before any weak-model
    result for that scenario is read.

## Tests (unit, no network)

- Adapters: request shape, including the sampling/extra fields; thinking blocks passed back; tool-call round trip.
  Use fakes.
- Fixtures: the REST call sequence for S0 and S2 against pytest-httpx fakes of Onedata 25.0's shapes, including the
  empty write at the final size, and no write without an offset in S2.
- Oracles: pass/fail cases for each scenario, including "premature claim".
- Runner: concurrency ≤2 per model; the Haiku budget refusal; secrets never in the artefacts (assert that the token
  string from a fake token file does not appear in any written file).
- The host guard refuses non-local hosts.
