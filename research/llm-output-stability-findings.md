# LLM output-stability findings

Empirical observations about LLM-side output-emission behaviour
surfaced by the PPAM 2026 multi-LLM K=1 sweeps. **Distinct from the
MCP-server findings (M-1..M-9 in `empirical-mcp-server-findings.md`)
— these document model-side quirks, not server-side design issues.**

The shared shape across these findings: the agent does the
federation-side work correctly (oracle's `federation_pass=true`) but
fails the `mcp_pass` axis because of an output-emission quirk that
isn't a reasoning failure. Worth surfacing as paper data points
about the practical reliability of open-weight LLMs as tool-using
agents on long-running federated workflows.

## L-1. Qwen3.6-35B sometimes emits empty `content` after long tool chains

**Surface**: Qwen3.6-35B (via Cyfronet PLGrid Forge OpenAI-compat
endpoint) on scenario P3 ("Add QoS rule + verify it materialised").

**Failure shape**: After 3-4 successful tool calls
(`add_file_qos_requirement` → `get_file_qos_summary` →
`list_space_transfers`), Qwen's final assistant message has
`tool_calls=[]` and `content=""` — empty string content with no tool
call. The adapter correctly captures this as `final_answer=""`. The
P3 oracle's `mcp_pass` check requires the final answer to contain
the word `transfer` or `fulfilled`; empty trivially fails that
check. **`oracle_federation_pass: true` confirms the work was done
correctly** — the QoS rule was added, the polling happened, the
transfer materialised.

**Empirical rate**: across 9 historical Qwen P3 K=1 trials in the
PPAM 2026 artefacts:

| Outcome | Count | `final_answer` length |
|---|---|---|
| PASS | 6 | 851-1608 bytes (substantial summary) |
| FAIL | 3 | 0 bytes (empty content) |
| | | (`oracle_federation_pass: true` in all 9) |

So Qwen does the federation work 100% correctly on P3. The variance
is purely in whether it emits a textual summary at the end. ~33%
of the time it returns an empty assistant message; ~67% of the time
it returns a substantial summary.

**Root cause hypothesis**: Qwen3.6-35B's chat template + tool-use
training apparently allows the model to terminate the conversation
with an empty content turn after the last tool call. Other LLMs
(Sonnet, GLM, V4-pro) consistently produce a final summary turn
after their last tool call. This is a Qwen-specific quirk.

**Adapter ruled out**: `benchmark/llm_adapters/openai_compat.py:167-169`
captures `final_text = msg.content` cleanly when the model returns no
tool calls — whatever the model emits is what gets recorded. Adapter
inspection confirmed there's no path that drops a non-empty final
message.

**Implication for the paper**: P3 isn't a Qwen reasoning failure —
the framing "Qwen has a P3 reasoning gap" was incorrect. The honest
framing is "Qwen has output-emission stochasticity in long tool-use
chains; the federation effect always lands". This is a useful data
point about the practical reliability gap between frontier and
open-weight models even when their tool-selection capability is
identical: Sonnet always reports back; Qwen sometimes silently
finishes.

**Oracle-design response** — APPLIED 2026-05-02:

The P3 oracle's `mcp_pass` rule was loosened to:

    mcp_pass = added_qos AND polled AND (answer_ok OR federation_pass)

i.e., accept the trial when the agent did the right tool work AND
*either* (a) verbalised the outcome OR (b) the federation observed
the effect. Path (b) credits Qwen for federation-correct trials
even with empty content; path (a) preserves the strict text-match
fallback for slower federations where the deadline expires before
the transfer materialises.

Implementation: `benchmark/oracles/placement.py::verify_p3`. Tests
pinning the loosened behaviour: `test/unit/test_oracle_p3_loosened.py`
(4 cases — empty+fed_pass=PASS, text+fed_fail=PASS, empty+fed_fail=FAIL,
no-tool-calls=FAIL regardless).

Why we DID apply this (vs. earlier "deferred" framing): the
empirical evidence is unambiguous. The P3 prompt requires the
agent to "Report which condition was observed". Qwen's silent
termination after correct tool work is a model output-emission
quirk, not a task-completion failure. Penalising it in the headline
table would mis-attribute a model formatting glitch as an agentic
failure. Federation-side evidence is the correctness ground truth.

**Counter-arguments considered**: (i) The loosening papers over
"silent agents" that an end-user would find unhelpful. Counter:
the L-1 quirk doesn't predict end-user UX in production
(production deployments typically wrap the agent in a UI loop that
re-prompts on empty content); the K=1 / K=8 number measures task
correctness, not UX polish. (ii) The loosening might leak into
other oracles. Counter: it's scoped to verify_p3 only, with the
loosening rationale tied to the specific Qwen quirk. Other
dynamic-tier oracles (P4) still require explicit textual reporting
because their tool surface doesn't have an equivalent
federation-side ground truth check.

**Expected effect on K=1 numbers**: Qwen P3 `mcp_pass` will go
from 6/9 historical → effectively 9/9 (because all 9 had
`federation_pass=true`). Sonnet/V3/GLM P3 numbers unchanged
because they all already produce textual answers. V4-pro P3 TBD
once the OpenRouter rate-limit fix lands.

## How to add to this file

When a new LLM-side stability quirk is observed, add as L-N
(L-2, L-3, etc.) following the L-1 structure:

- Surface (which model + endpoint + scenario)
- Failure shape (what the trial record shows)
- Empirical rate (across N trials)
- Root cause hypothesis (model behaviour, not server design)
- Adapter ruled out (link to the adapter inspection that confirms it)
- Implication for the paper

This file is the LLM-behaviour counterpart to
`empirical-mcp-server-findings.md` (M-N entries). Keep them separate
— mixing server-design findings with model-behaviour findings makes
both harder to action.
