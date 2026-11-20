# Tool maturity: how a tool goes from experimental to stable

Status: agreed 2026-11-20.

Every tool the server registers has a maturity tier, recorded in [`onedata_mcp/tool_maturity.py`](../onedata_mcp/tool_maturity.py). Operators can expose only stable tools (`ONEDATA_MCP_MATURITY=stable`). The tier is model-invisible: it never appears in a tool's description.

## Tiers

- **experimental**: every new tool starts here. So does any tool whose model-facing surface changed in a way the rules below send back.
- **stable**: the tool passed every gate below at its current surface.

A tool's **surface** is what a model sees in `tools/list`: its name, description, input schema and annotations. `make tools-manifest` prints it.

The preview flag `ONEDATA_MCP_EXPERIMENTAL` is a separate mechanism: it registers opt-in features (extra tools, alternative schemas) that are not ready for the default tool list at all. A preview tool enters the default list as experimental before it can be promoted.

## Gates for promotion

A tool is promoted by a PR that moves it into `STABLE` and adds its validation record. The PR must show all five gates.

1. **Implementation.**
   - Unit tests cover success and the error paths a model can trigger: bad id, wrong path form, missing permission, empty result.
   - A mutating tool is in `token_policy.WRITE_TOOL_NAMES` and annotated as such.
   - List-valued inputs have size caps.
2. **Surface review.**
   - The description says what the tool does and what it returns, in the terms a model needs.
   - It names no tool that might not be offered alongside it, and cites no document the model cannot read.
   - Errors tell the caller how to fix the call.
   - `make conformance` passes, and the `tools/list` diff is reviewed.
3. **Live check.** A gated test in `test/live/` exercises the tool against a real Onedata 25.0 deployment, including at least one failure path.
4. **Validation run.** The standing panel (below) runs every scenario that requires the tool, at **K=8** repetitions, at the surface being promoted. A scenario *requires* a tool when no other tool it offers can do the job; usage in a scenario where another tool would also do is supporting evidence only. The scenarios for a tool cover:
   - a task that needs it;
   - a state where the right answer is "no, because …";
   - a task where it is offered but not needed;
   - id, name and path variants of its arguments;
   - for a mutating tool, a check of the federation's end state after the agent's final answer, so that a premature "done" is caught.

   Every non-pass is classified before anything counts:
   - harness or oracle bug: fix and rescore;
   - deployment artefact;
   - **tool-attributable**: a misread description, a wrong argument the schema or error text could have prevented, or a misread answer;
   - model capability.

   The run passes when:
   - the control model passes every scenario, or each control failure is shown to be a harness bug or artefact;
   - no tool-attributable finding is open: each is fixed and re-run, or accepted with a written reason.

   Pass rates are reported but do not gate.
5. **Record.** A validation record in `design/` names:
   - the commit and the surface (a `tools/list` hash);
   - the panel and K;
   - the per-model results and each classification.

## Staying stable

A change to a stable tool's surface:

- **name, input schema or annotations**: the tool goes back to experimental. These changes break agents and clients that already use it.
- **description wording only**: re-run the tool's scenarios on the smoke pair at K=3. The tool stays stable if no new tool-attributable failure appears; otherwise it goes back to experimental.

Bug fixes that leave the surface unchanged need only gates 1 and 3.

## The standing panel

The panel is defined by role, so that it survives model churn. The current instance is configured in the benchmark harness. Models are chosen for the failures they expose, not for their scores.

- **Probes:** at least three open-weight model families of modest capability. Today:
  - Qwen3-Coder-30B-A3B, a literal reader of descriptions and argument types;
  - GLM-5.3-Flash;
  - Qwen3.6-35B-A3B;
  - DeepSeek-V4-Flash, which reads "incomplete" as "absent" and claims "done" early.
- **A small commercial model at low effort:** Claude Haiku at effort low.
- **A capable control:** Claude Sonnet at medium effort. A scenario it fails is suspect: fix the scenario, oracle or tool before reading the probes.
- **Smoke pair**, for re-validation after wording changes: Qwen3-Coder plus Haiku at effort low.

Prompts are neutral: they name no tool and give no workflow advice. Each model config gets its own space, so trials cannot interfere.

## Test environment

Validation runs use a two-provider Onedata 25.0 federation run locally with Docker Compose, owned by this project and reset between runs. It is never a shared or production deployment. Live tests refuse any host that is not local.

## Leaving the default surface

A tool that cannot be validated in any environment we have, or that duplicates a stable tool, moves behind the preview flag rather than being deleted. Removing a tool from the default list breaks clients that use it, so that happens only in a PR that says so.

## Transition

Six stable tools had wording-only description fixes after the earlier benchmark froze: `add_file_qos_requirement` (its `expression` field), `get_file_distribution`, `get_file_qos_summary`, `get_transfer`, `list_space_providers` and `list_space_transfers`. They stay stable pending the smoke re-run that the wording rule requires; it runs as soon as the test environment exists.

## Relation to the earlier benchmark

The initial stable set is the 16 tools evaluated by the multi-LLM benchmark in `benchmark/` (`benchmark/tool_allowlist.py::HEADLINE`). That list stays frozen as the benchmark's own record. `STABLE` must contain it but may grow beyond it.
