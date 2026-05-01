# Design decision: oracle philosophy — measure agent + MCP, not Onedata

**Status:** decided 2026-05-01 (clarified in mid-session by user).
**Affects:** every oracle in `benchmark/oracles/`, the trial-result type
`OracleResult`, the pass^k aggregator (workstream #22), and the
paper §5/§7 narrative.

## The principle

> "We are not testing if Onedata works, we test if Onedata received the
> MCP call. If Onedata fails for Onedata's own reasons, that is still
> MCP success."

What we measure: does the agent + MCP stack issue the right tool call
sequence to address the task? What we **do not** measure: does Onedata
then propagate metadata via dbsync within N seconds; does Onedata
return 200 vs 503 on transient internal errors; does Onedata's eventual
consistency window happen to close before the oracle polls.

If the agent's MCP call sequence is correct, the trial is credited as
agent success even when Onedata's post-state diverges.

## Two-axis OracleResult

```python
@dataclass(frozen=True)
class OracleResult:
    mcp_pass: bool                # what pass^k aggregates over
    federation_pass: bool | None  # post-state matches expectation; None for format
    diagnosis: str
```

Both axes are evaluated independently. They can disagree:

| `mcp_pass` | `federation_pass` | meaning |
|---|---|---|
| True | True | Agent did right, Onedata propagated. ✓ Trial pass. |
| True | False | **Agent did right, Onedata diverged.** ✓ Trial pass on the headline; the divergence rate becomes a §7 *Threats* signal. |
| False | True | Agent failed, federation matches anyway (e.g. residual state from prior trial). ✗ Trial fail; informational. |
| False | False | Agent failed and federation doesn't match. ✗ Trial fail. |
| False | None | Format-tier scenario, agent's answer wrong. ✗ Trial fail. |
| True | None | Format-tier scenario, answer correct. ✓ Trial pass. |

## What goes into pass^k

Headline `pass^k` aggregates over `mcp_pass` only. Federation_pass is a
side metric reported separately — useful for §7 *Threats* narrative
("in N of 432 trials, agent's MCP calls were correct but the
federation diverged, attributable to Onedata-side eventual consistency
or transient errors") and for debugging individual trials.

## Why two axes, not one

The naïve oracle inspects federation post-state and conflates two
failure modes the paper needs to distinguish:

- **Agent capability gap**: agent failed to compose the right tool
  call sequence for the task. This is what the paper measures.
- **Substrate flakiness**: Onedata had an off moment. This is
  out of scope; absorbing it into pass^k would lower scores for
  reasons unrelated to agent capability and contaminate the
  cross-LLM comparison.

The two-axis result preserves the post-state oracle as a useful test
("did the federation actually reach the intended state?") without
penalising the agent when it didn't.

## What changes in oracle implementation

**Static + dynamic oracles** now compute both axes:

- `mcp_pass`: predicate over `AgentTrace.tool_calls`. Helpers in
  `benchmark/oracles/_helpers.py::find_calls` and `has_successful_call`.
- `federation_pass`: REST side-channel inspection, the same logic the
  earlier draft of these oracles used.
- `diagnosis`: human-readable string surfacing both axes when either
  fails. Especially calls out "MCP succeeded but federation diverged"
  for the row 2 case.

**Format oracles** (D1-D6, A1, A6) return `federation_pass=None`. The
agent's answer is the unit of measurement; there's no separate
post-state to inspect (no writes happen for format-tier scenarios, so
federation state == fixture state == ground truth).

**Tool-call inspection treats received-by-Onedata as success** — even
if Onedata returned a 5xx, the call WAS received per the user's
principle. We use `find_calls` (returns successful + failed) for "did
the agent attempt this action", and `has_successful_call` (returns
only successful) for "did the agent get a 2xx that it could read and
proceed from". The latter is rare; default to `find_calls`.

## What changes for the harness (workstream #19)

The harness must populate `AgentTrace.tool_calls` with `ToolCall(name,
arguments, succeeded, error)` for every MCP call the agent makes. The
existing `forge_harness.py` already logs tool names and timing
(`metrics.tool_calls`); needs an extension to capture the full
arguments dict and succeed/fail bit.

Implementation hint: `AsyncOpenAI.chat.completions.create()` returns
`message.tool_calls` with `tc.function.arguments` as a JSON string;
deserialise to `ToolCall.arguments` dict. The `succeeded` bit comes
from whether the subsequent `mcp.call_tool(...)` raised `ToolError`.

## What changes for the aggregator (workstream #22)

Emit two columns per trial:

- pass^k headline: aggregate `mcp_pass` over n=8 trials per (LLM, task).
- federation-divergence: count of `mcp_pass=True ∧ federation_pass=False`
  per (LLM, task). Optional column on Table 5; appears in §7 narrative
  if the rate is non-trivial.

## What changes for the paper

§4¶3 (oracle taxonomy) currently reads as a one-axis success criterion.
The writing agent should add a sentence acknowledging the two-axis
distinction:

> "We separate **MCP success** (the agent's call sequence is correct)
> from **federation success** (the post-state matches expectation).
> Headline pass^k aggregates the former; the latter is a side metric
> that surfaces Onedata-side eventual-consistency divergence in §7."

§7 *Threats to Validity* gets a new sub-paragraph (Infrastructure):

> "The federation operates eventually-consistently via dbsync; we
> observed mcp_pass=True with federation_pass=False in N of 432 trials,
> attributable to dbsync propagation delays exceeding the polling
> window. Pinning the dbsync calibration sweep tightens this; until
> then we report the divergence rate explicitly."

## Cross-references

- `benchmark/_runtime_types.py::OracleResult` — the dataclass
- `benchmark/oracles/_helpers.py::find_calls` / `has_successful_call`
- `benchmark/oracles/{discovery,access,placement}.py` — every oracle
- `test/unit/test_oracle_two_axis.py` — explicit four-cell coverage
- `IMPLEMENTATION_NOTES.md` — pointers to all of the above
- Paper draft: `paper.tex` §4¶3 and §7 (writing-agent edits documented in
  `papers/ppam-2026/research/28-empirical-spec-corrections.md`)
