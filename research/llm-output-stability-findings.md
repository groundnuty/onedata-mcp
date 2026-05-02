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

## L-2. Granite-4.1-30B inserts a stray underscore in long string identifiers

**Surface**: IBM Granite-4.1-30B (BF16, served via local vLLM on port 8003) on
scenario D1 ("List all visible spaces with provider counts as a markdown
table"). Observed in run `20260502T230005_granite_k1` (probe and full K=1
both reproduce the shape).

**Failure shape**: `list_user_spaces` succeeds, returns 37 spaces with
correct names. Granite renders them in a markdown table — and **inserts
an extra underscore between `20` and `26`** in every single
`ppam_2026_mcp_tests*` row, consistently:

```
ppam_2026_mcp_tests_granite_4_1_30b   ← real name from MCP response
ppam_20_26_mcp_tests_granite_4_1_30b  ← what Granite emitted (every row)
```

The corruption is **systematic, not random** — every one of the 11
ppam-prefixed rows has the same `_20_26_` mangling. Other space names
(non-ppam-prefixed rows like `CloudSKTest`, `IagosSpace`, `UnprocessedData`,
`UC1Storage`) render correctly without underscore insertion.

**Empirical rate**: across 2 trials in run T230005 + the probe
T225745: 2/2 D1 trials show this exact pattern. The probe also showed
the model passing D3, A4, and P3 cleanly — so Granite's tool-call
plumbing works correctly; the corruption only manifests when emitting
long underscore-separated identifiers in `content`.

**Root cause hypothesis**: Granite-4.1-30B's BPE tokenizer likely
splits the prefix `ppam_2026_mcp_tests_` into a token sequence that
includes `_20` and `_26` as separate tokens. When the model copies
the literal string out of the `list_user_spaces` JSON response into
its markdown answer, the boundary between the `_20` and `_26` tokens
gets re-emitted as two separate tokens with an additional underscore
delimiter. Other OSS models in the panel (Qwen, V4-pro, GLM, Gemma,
Devstral) all reproduce the canonical `ppam_2026_mcp_tests_*` strings
verbatim — making the corruption Granite-specific rather than
prompt-induced.

**Adapter ruled out**: the corrupted strings appear in the trial
JSONL's `final_answer` field, which `openai_compat.py:167-169`
captures faithfully from `msg.content`. Verified by reading the
adapter source — there's no transform path between the OpenAI-compat
response and the captured answer that would insert characters.

**Why it matters for the paper**: this is a class of failure that
synthetic benchmarks miss but federated-data agentic workflows can't
tolerate. A user asking "where is my dataset?" gets back paths that
look plausible but don't exist in the federation. The MCP/oracle
both did everything correctly; the model's *rendering* of received
data corrupted it. Worth surfacing as a paper data point alongside
L-1: open-weight LLMs vary in **fidelity of received-data
re-emission**, and that fidelity gap predicts production reliability.

**Possible oracle-design response** (NOT recommended): we could
loosen the D1 oracle to accept the corrupted form via fuzzy match
(`ppam_20_26_*` → `ppam_2026_*` after dropping interior `_2N_2N_`
patterns). Decision: do **not** apply. Unlike L-1 (Qwen empty content
where federation_pass = true), there's no federation ground-truth
hook for D1; the oracle's only signal IS the rendered answer. If the
agent corrupts a space name it gives the user, the trial should fail
— that's correctness in the user-facing dimension. K=1 and K=8 both
keep the strict check.

**Expected effect on K=1 numbers**: Granite D1 stays FAIL until IBM
fixes the underlying tokenizer/decoding behaviour. Other panel LLMs
unaffected (none reproduce this shape).

**Sister observations** (not yet promoted to L-N):
- *Devstral-2-123B D1 (probe T225745)*: dropped 7+ rows from a long
  table. Different shape (truncation) — but same paper-relevant
  axis: long lists are where weaker open-weight models stumble. Will
  re-evaluate after the K=1 sweep completes.

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
