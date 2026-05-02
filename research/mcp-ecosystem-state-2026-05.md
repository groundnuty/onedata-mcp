# MCP-server creation, testing, and benchmarking ecosystem (state as of May 2026)

**Purpose.** Background research for the PPAM 2026 paper on the Onedata MCP server.
The goal is to honestly position our work — the 14-tool fork, the 18-scenario
multi-LLM benchmark, the M-1..M-12 fix history, and the federation-side oracle —
against what the broader MCP ecosystem actually does (not what it should do).

This document covers four buckets:

1. How MCP servers are *created* (SDKs, scaffolding, reference implementations,
   distribution).
2. How MCP servers are *tested* (Inspector, FastMCP in-memory pattern, conformance,
   community testing tools, LLM-in-the-loop tests).
3. How LLM-tool-use is *benchmarked* more broadly (BFCL, τ-bench, MCP-Bench,
   MCP-Universe, MCPEval, OSWorld-MCP, MCPAgentBench).
4. How the PPAM 2026 onedata-mcp approach *compares* to that ecosystem.

Citations are inline as markdown links so the paper-writing pass can follow up.
Where I could not confirm something, I say so explicitly — gaps are data points.

---

## 1. MCP server CREATION ecosystem (May 2026)

### 1.1 Official SDKs

The Model Context Protocol was launched by Anthropic in November 2024 with
TypeScript and Python SDKs. As of May 2026 the
[modelcontextprotocol GitHub org](https://github.com/modelcontextprotocol)
maintains official SDKs for **TypeScript/Node.js, Python, Go, C#/.NET (with
Microsoft), Java, Kotlin, Ruby, and PHP**, with community SDKs for Rust, Swift,
and others
([MCP Cheat Sheet 2026](https://www.webfuse.com/mcp-cheat-sheet),
[Wikipedia summary](https://en.wikipedia.org/wiki/Model_Context_Protocol)).
TypeScript and Python are the most mature; Go and C# are next; the rest lag
the spec by one or two minor versions.

The **Python SDK is special** because it absorbed FastMCP 1.0 in 2024
([FastMCP welcome](https://gofastmcp.com/getting-started/welcome)), so the
official SDK ships *two* server APIs side-by-side: a "low-level" `Server`
class that exposes raw protocol primitives, and the higher-level `FastMCP`
decorator API that auto-generates JSON Schema from Python type hints.

**FastMCP 2.x** (the standalone project at
[gofastmcp.com](https://gofastmcp.com)) continues to be developed by Jeremiah
Lowin (`@jlowin`) ahead of the SDK's vendored copy. FastMCP claims
[~1M downloads/day and ~70% of MCP servers across all languages](https://gofastmcp.com/getting-started/welcome)
ride on some version of FastMCP — a striking concentration if accurate, and
the PPAM 2026 fork sits inside that 70%.

### 1.2 Scaffolding / generators

There is **no `npm create @modelcontextprotocol/server` or equivalent
"first-party" CLI** as of May 2026. Scaffolding is community-driven:

- [codingthefuturewithai/mcp-cookie-cutter](https://github.com/codingthefuturewithai/mcp-cookie-cutter) — Cookiecutter template (stdio + SSE).
- [maheshmahadevan/mcp-cookie-cutter](https://github.com/maheshmahadevan/mcp-cookie-cutter) — pip-installable CLI tool.
- [biocontext-ai/mcp-server-cookiecutter](https://github.com/biocontext-ai/mcp-server-cookiecutter) — FastMCP-based template for biomedical tooling.
- [monarch-initiative/cookiecutter-mcp](https://github.com/monarch-initiative/cookiecutter-mcp) — for ontology/data-science MCPs.
- [ruvnet/dynamo-mcp](https://github.com/ruvnet/dynamo-mcp) — a meta-MCP that exposes Cookiecutter templates over MCP.

These all bake in stdio + (optionally) Streamable HTTP transports and a
README skeleton; none of them ship with a non-trivial test suite. The lack of
a canonical scaffolder is itself a small data point: the protocol is mature
enough that one *could* exist, but the ecosystem is still pre-paving-the-cow-path.

### 1.3 Reference implementations

The canonical examples everyone copies from are in
[modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers).
As of May 2026 the steering group maintains seven reference servers there:
**Everything, Fetch, Filesystem, Git, Memory, Sequential Thinking, Time**
(repository language mix: ~69% TypeScript, ~19% Python, ~10% JavaScript).
The README explicitly cautions that they are *educational* — "intended as
reference implementations to demonstrate MCP features and SDK usage" — not
production-grade. A long list of community/third-party servers (GitHub,
Slack, Postgres, Notion, etc.) is curated in the same repo's README.

The patterns these servers share are: stdio transport by default; one tool
per file or one tool per logical resource; minimal happy-path tests (when
tests exist at all); and configuration through environment variables.

### 1.4 Hosting / runtime / distribution

There are three transports today: **stdio** (default for local dev / desktop
clients), **SSE** (deprecated in newer spec revisions but still common), and
**Streamable HTTP** (the canonical remote transport per the
[2025-11-25 spec](https://modelcontextprotocol.io/specification/2025-11-25)).
Distribution is overwhelmingly:

- **`npx @scope/mcp-server-foo`** for TypeScript servers — invoked by the
  client config; npm package with a `bin` entry.
- **`uvx mcp-server-foo`** for Python servers — `uv tool` runs the package
  in an ephemeral venv.
- **Docker images** — increasingly common for servers with heavy native
  deps; Docker has positioned itself as a natural distribution channel
  ([dev.to roundup](https://dev.to/leomarsh/mcp-server-executables-explained-npx-uvx-docker-and-beyond-1i1n)).
- **Embedded / first-party hosted** — servers run inside SaaS providers
  (e.g. GitHub's official MCP server, Slack's, etc.) and exposed via
  Streamable HTTP with bearer-token auth.

For our case (a Python FastMCP server hitting a federation API), `uvx` +
optional Docker is the conventional packaging shape; transport is stdio
during dev and Streamable HTTP for hosted federation use.

### 1.5 Anthropic guidelines for server design

Anthropic published
["Writing tools for agents"](https://www.anthropic.com/engineering/writing-tools-for-agents)
and
["Code execution with MCP"](https://www.anthropic.com/engineering/code-execution-with-mcp)
in 2025–2026. The relevant rules of thumb:

- **Tool names should reflect conceptual purpose, not implementation.**
- **Namespace by service+resource** (e.g. `asana_projects_search`) when an
  agent will see many tools.
- **Parameter names should be unambiguous** (`user_id`, not `user`).
- **Responses should be concise**; truncate / summarise large artifacts;
  avoid logging debug output back to the agent.
- **Resources** (passive) and **Tools** (active) should be partitioned
  carefully; static context goes in resources.

These are guidelines, not enforced rules — the spec validates JSON-RPC
shape, not semantic-design quality. A common observation is that *agent
failure modes are usually upstream of the protocol*: bad tool naming, bad
parameter naming, response shape too verbose. That matches our M-1..M-12
findings shape.

---

## 2. MCP server TESTING ecosystem (May 2026)

This is the bucket where the gap between published guidance and shipped
practice is widest.

### 2.1 The Inspector — a debugger, not a test framework

[`@modelcontextprotocol/inspector`](https://github.com/modelcontextprotocol/inspector)
is the official Anthropic-maintained tool. It's an interactive React web UI
+ a Node proxy that connects to a server via stdio, SSE, or Streamable HTTP
and exposes Tools / Resources / Prompts / Notifications panels for manual
exploration
([modelcontextprotocol.io/docs/tools/inspector](https://modelcontextprotocol.io/docs/tools/inspector)).

Critically, Inspector is **a debugger and a smoke-test surface, not a
programmatic test framework**. The official docs list its workflow as:
"launch Inspector with your server, verify basic connectivity, check
capability negotiation … iterative testing." There is no scripted
assertion API, no test reporter, no CI integration mode. You can put it in
CI as a "does the server start and list its tools" smoke check, but
nothing more.

### 2.2 The FastMCP in-memory `Client(server)` pattern

This is the closest thing the ecosystem has to a *standard* unit-test
pattern, and it's the one we use. From
[gofastmcp.com/development/tests](https://gofastmcp.com/development/tests):

```python
from fastmcp import FastMCP, Client

server = FastMCP("WeatherServer")

@server.tool
def get_temperature(city: str) -> dict:
    return {"city": city, "temp": 72}

async def test_weather():
    async with Client(server) as client:
        result = await client.call_tool("get_temperature", {"city": "NYC"})
        assert result.data == {"city": "NYC", "temp": 72}
```

Key properties:

- The `Client` connects to the in-memory `FastMCP` instance directly — no
  subprocess, no socket — but still through the *real* MCP protocol layer,
  not a mock.
- Tests run in milliseconds; deterministic; pytest-friendly with async
  fixtures.
- This pattern supports happy-path, error-path, and parameterised
  edge-case tests.

Jeremiah Lowin's
["Stop vibe-testing your MCP server"](https://jlowin.dev/blog/stop-vibe-testing-mcp-servers)
(2026) advocates for this pattern explicitly and labels the alternative —
typing prompts into Claude and "checking if the output looks right" —
"vibe-testing": stochastic, slow, expensive, opaque, superficial.
Independently, Klement Gunndu's
["Your MCP server has no tests. Here are 4 patterns to fix that"](https://dev.to/klement_gunndu/your-mcp-server-has-no-tests-here-are-4-patterns-to-fix-that-2k59)
(2026) names four canonical patterns: (1) FastMCP in-memory Client
unit tests; (2) schema validation tests; (3) parameterised edge-case tests;
(4) Inspector for interactive debugging — and asserts: *"Most MCP servers
ship with exactly one test: a developer typing a prompt into Claude and
checking if the output looks right. Most of them have zero automated tests.
If you ship yours with a proper test suite, you are already ahead of 90% of
the ecosystem."* Whether 90% is exact or rhetorical, the qualitative claim
matches what one sees scrolling
[modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers)
and the long tail of community servers.

There is no official guidance from the SDK team on this. Issue
[modelcontextprotocol/python-sdk#1252](https://github.com/modelcontextprotocol/python-sdk/issues/1252)
(opened August 2025, still open as of early 2026) explicitly asks "what is
the recommended way of writing unit tests for MCP endpoints?" and remains
labelled "ready for work" — i.e. the core team has acknowledged the
documentation gap but not closed it.

### 2.3 Integration tests / subprocess

Servers that go beyond unit tests usually do one of two things:

- **Subprocess + JSON-RPC over stdio.** Spawn the server binary, write
  initialise/list-tools/call-tool envelopes to stdin, parse JSON-RPC from
  stdout, assert. This is what
  [`mcp-server-tester`](https://github.com/r-huijts/mcp-server-tester)
  (community, "WORK IN PROGRESS") and
  [`mcp-test-runner`](https://github.com/privsim/mcp-test-runner) do.
- **Testcontainers + pytest**, e.g. ARM's
  [Automate MCP testing with pytest + testcontainers](https://learn.arm.com/learning-paths/cross-platform/automate-mcp-with-testcontainers/github-actions-ci/)
  guide, which spins the server in a container, runs assertions over its
  HTTP transport, and wires the lot into GitHub Actions.

Both patterns are individually rare and individually wired; nothing has
crystallised into a "the way you do MCP integration tests" yet.

### 2.4 Conformance / protocol-compliance

There **is** an official conformance suite as of 2026:
[modelcontextprotocol/conformance](https://github.com/modelcontextprotocol/conformance).
Latest release v0.1.16 (March 2026). It is maintained by the
modelcontextprotocol org (i.e. the spec steering group, not Anthropic
alone), has 17 releases, and ships:

- A CLI for running scenarios (`server-initialize`, `tool-listing`,
  tool-invocation variants, OAuth Dynamic Client Registration flows, etc.).
- Baseline files for known failures, so SDK projects can run conformance in
  CI without flapping on already-documented gaps.
- A composite GitHub Action for SDK repos.
- A "tier" assessment scoring SDKs against canonical-spec coverage.

The 2026 MCP roadmap
([modelcontextprotocol.io/development/roadmap](https://modelcontextprotocol.io/development/roadmap),
also discussed in the
[WorkOS 2026-roadmap analysis](https://workos.com/blog/2026-mcp-roadmap-enterprise-readiness))
elevates conformance to a first-class workstream alongside enterprise auth
and registry/discovery. **Important caveat for the paper:** the conformance
suite tests *protocol-level* behaviour (does your server speak JSON-RPC
correctly, does it advertise capabilities right, does OAuth DCR work). It
does not test *semantic* correctness — whether `list_files` actually
returns the right files. That's a different problem, and our oracle does
the second.

### 2.5 Community / third-party testing tools

Beyond Inspector and the conformance suite, the community has produced:

- [thoughtspot/mcp-testing-kit](https://github.com/thoughtspot/mcp-testing-kit)
  — TypeScript test utilities, framework-agnostic (vitest/jest), thin
  wrapper to spin a server and assert.
- [haakco/mcp-testing-framework](https://github.com/haakco/mcp-testing-framework)
  — broader cross-server compatibility validation.
- [r-huijts/mcp-server-tester](https://github.com/r-huijts/mcp-server-tester)
  — uses Claude to *generate* test cases from tool schemas, then validates.
- [Apify Tester MCP Client](https://www.pulsemcp.com/), Loop,
  [mcpcat.io](https://mcpcat.io/guides/writing-unit-tests-mcp-servers/)
  guides, and various blog-post-based test harnesses.

None of these has emerged as a clear winner. The
[testomat.io 2026 testing-tools roundup](https://testomat.io/blog/mcp-server-testing-tools/)
lists ~10 options; an enterprise team picking one today would have to
evaluate. By contrast, the conformance suite (§2.4) does have a clear
canonical home and is referenced by the 2026 roadmap.

### 2.6 LLM-in-the-loop tests

This is the most interesting empty slot. There is **no broadly-adopted
practice of running an LLM-driven agent against an MCP server in CI as a
regression test**. The Inspector workflow is manual and human-in-the-loop;
FastMCP `Client` tests deliberately remove the LLM; conformance tests
remove the LLM. The blog posts that *do* describe LLM-in-the-loop testing
(jlowin, Klement Gunndu, Kai Gritun, mcpcat.io) all *warn against it* as
the *primary* test mode because of stochasticity, cost, and opacity. They
recommend it only as a final smoke check, supplementing deterministic
unit tests.

The closest things to LLM-in-the-loop MCP testing as a *published practice*
are the academic benchmarks discussed in §3 (MCP-Bench, MCP-Universe,
MCPEval, OSWorld-MCP, MCPAgentBench). Those are evaluation frameworks for
*models*, not regression-test rigs for *servers* — though the
methodologies overlap, and our work fits inside that overlap.

### 2.7 CI/CD: typical pattern

A typical MCP-server CI pipeline as of 2026 has:

1. Lint (ruff/black for Python, eslint/prettier for TS).
2. Unit tests (FastMCP `Client` for Python; protocol-mock-based for TS).
3. Optional: Inspector smoke (start server, list tools, exit).
4. Optional: Docker image build.
5. Rare: integration tests against a real upstream API.

Test counts in published reference servers are mostly small — single-digit
to a few dozen. The
[GitHub MCP server](https://github.com/github/github-mcp-server)
([deepwiki testing infrastructure](https://deepwiki.com/github/github-mcp-server/4.3-local-development-setup))
is one of the better-tested examples in the ecosystem: hundreds of
table-driven Go tests against a `MockRoundTripper` covering every endpoint
pattern, every status code, GraphQL operations, lockdown mode, edge cases
like empty results and special characters. That is the high end of what
mature MCP-server test suites look like — and it's a *server-mocks-the-API*
shape, not server-against-real-federation.

---

## 3. LLM-tool-use BENCHMARKING ecosystem (broader context)

### 3.1 The general-purpose function-calling benchmarks

These predate MCP and evaluate function-calling across whatever schema the
benchmark defines.

- **[BFCL — Berkeley Function Calling Leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html)**
  ([ICML 2025 paper](https://openreview.net/forum?id=2GmDdhBdDk)).
  V4 by mid-2026. Static datasets, AST-based scoring of generated function
  calls, hundreds of test cases. The default reference for "can your model
  call functions" leaderboards. Doesn't use MCP — it uses generic
  function-calling schemas with vendor-specific tool-API plumbing
  underneath.

- **[τ-bench / τ²-bench](https://github.com/sierra-research/tau-bench)**
  (Sierra Research, 2024;
  [τ²-bench paper](https://arxiv.org/pdf/2506.07982)). Customer-service
  scenarios (airline, retail). Multi-turn, with an *LLM-simulated user*
  driving the agent. Crucially, scoring is
  **database-state at end of conversation vs annotated goal state** —
  this is the same shape as our federation-side ground-truth oracle, and is
  an established pattern in customer-service LLM eval. Stochastic
  (the user simulator is an LLM); reports `pass^k` reliability.

- **ToolBench, NESTful, Toolathlon** — adjacent function-calling benchmarks
  with different scope (long-tail real APIs, nested calls, etc.). Less
  central, but referenced in
  [function-calling surveys](https://huggingface.co/datasets/tuandunghcmut/BFCL_v4_information).

None of these treats MCP specifically.

### 3.2 MCP-specific benchmarks

This space is younger and has multiplied in 2025–2026:

- **[MCPEval](https://arxiv.org/html/2507.12806)** (Liu et al., July 2025).
  Five MCP servers; ten LLMs across healthcare, finance, Airbnb, sports,
  national parks. Auto-synthesises tasks from each server's tool list using
  a Task-LLM. Two evaluation axes: **Tool Call Matching** vs reference
  trajectories, and **LLM Judging** of planning/execution-flow/context.
  Important precedent for "use the LLM to author the eval". Limited tool
  count (~few dozen total).

- **[MCP-Bench](https://arxiv.org/abs/2508.20453)** (Accenture Labs,
  August 2025; NeurIPS 2025 SEA workshop;
  [code](https://github.com/Accenture/mcp-bench)). 28 *live* MCP servers,
  250 tools, 20 LLMs. Multi-step cross-server tasks. Three-axis evaluation:
  schema-understanding, trajectory planning, task completion. The
  largest and most cited MCP-server benchmark as of mid-2026.

- **[MCP-Universe](https://mcp-universe.github.io/)** (Salesforce AI
  Research, arXiv:2508.14704, August 2025). 6 domains (Location/Repos/
  Finance/3D/Browser/Web), 231 tasks, three evaluation tracks (ReAct,
  function-calls, agent). Best-performing model 43.72% overall — i.e.
  even leading agents miss more than half of real-world MCP tasks.

- **[OSWorld-MCP](https://arxiv.org/abs/2510.24563)** (X-PLUG/Alibaba,
  ICLR 2026). 158 tools, 7 desktop apps (LibreOffice, VS Code, Chrome,
  VLC, OS utilities), 25 distractor tools. Combined evaluation of MCP tool
  invocation, GUI ops, and decision-making.

- **[MCPAgentBench](https://arxiv.org/abs/2512.24565)** (December 2025).
  Sandboxed MCP tool simulation with distractor tools to test
  selection/discrimination; introduces Task-Finish-Score, Task-Efficiency,
  Time-Efficiency, Token-Efficiency.

These benchmarks all evaluate **models on MCP servers**, not **MCP servers
themselves**. The implicit assumption is that the servers are
fixed/correct and the model is the variable. Our paper inverts that
framing — we hold (mostly) one *family* of models and vary the *server* —
and that inversion is the angle worth claiming.

### 3.3 Multi-LLM panels for tool-use evaluation

Multi-LLM evaluation is itself standard practice in this space:

- **BFCL** routinely scores 50+ models per release.
- **MCP-Bench** evaluates 20 advanced LLMs.
- **MCPEval** evaluates 10 (open and closed source).
- **MCP-Universe** evaluates "numerous" LLMs across three tracks.

So a 7-LLM panel covering Claude, Qwen3.6, DeepSeek-V4-pro, GLM, Gemma,
Granite, etc. is **not large by leaderboard standards**, but is *also* not
small for a server-validation context — most server-validation work uses
zero or one model.

### 3.4 Federated-data + agentic LLM benchmarks

Within these benchmarks, none I could find as of May 2026 specifically
target *federated scientific data*. MCP-Bench has "scientific computing"
and "academic search" categories but they're consumer-facing APIs (PubMed,
arXiv search), not multi-provider federations with cross-provider
consistency requirements like Onedata. **Gap noted explicitly** —
federation-side ground-truth verification of agent-driven data operations
across a Onedata-like federation appears genuinely under-explored in
published MCP benchmarks.

---

## 4. Comparison to the PPAM 2026 onedata-mcp approach

What is **standard** in our setup (the paper should not over-claim novelty here):

- **FastMCP `Client(server)` in-memory unit-test pattern** — standard;
  documented as the canonical in-process unit-test approach
  ([gofastmcp.com/development/tests](https://gofastmcp.com/development/tests)).
- **Inspector smoke at dev time** — standard, expected, low-novelty.
- **A unit-test count in the low-3-digits** — comparable to the GitHub MCP
  server (Go tests in similar order of magnitude); above the
  long-tail community-server median (which Lowin/Gunndu put near zero
  automated tests). Going from 109 → 158 across the iteration is a
  *quantity-of-care* signal but not a methodological novelty.
- **Lint + test CI on every push** — standard.
- **Iterative server refinement driven by usage** — every published MCP
  benchmark drives some server refinement; we just published the deltas.

What is **unusual but not novel**:

- **Multi-LLM panel covering 7 vendor families** — multi-LLM evaluation is
  standard in benchmark papers (BFCL, MCP-Bench, MCPEval, MCP-Universe);
  applying a multi-LLM panel to *MCP-server validation as a regression
  rig* (rather than to model leaderboarding) is less common. The closest
  precedent is the iterative-fix loops the MCP-Bench / MCP-Universe teams
  ran while building their benchmarks — but they didn't publish that loop
  as the contribution; we do.
- **Ground-truth verification by inspecting backend state** — established
  in τ-bench (database-state vs goal-state) and in execution-based
  benchmarks generally (MCP-Universe). Applying it to a *real federation*
  rather than a sandbox is the substantive shift.

What may be **genuinely novel** (worth claiming carefully in the paper):

1. **Two-axis OracleResult (`mcp_pass` × `federation_pass`)**. Decoupling
   "did the MCP-tool call succeed at the protocol/contract layer" from
   "did the federation actually end up in the right state" is a clean
   separation I did not find in any of the surveyed benchmarks.
   τ-bench collapses both into a single goal-state check; MCP-Bench and
   MCP-Universe focus on trajectory + task completion judged by an LLM
   judge. **Two axes change what we can diagnose**: a passing `mcp_pass`
   with a failing `federation_pass` is exactly the silent-fallback shape
   that drives the most useful M-* findings. This deserves a named
   contribution.
2. **Federation-side ground-truth verification of MCP-tool semantic
   effect against a *live, federated, multi-provider* data layer.** Not
   a sandbox, not a single-server stub, not a mock — a real Onedata 25.0
   deployment. I could not find a comparable setup in any surveyed
   benchmark.
3. **Per-LLM-space architecture for parallel benchmark isolation.**
   Isolating each panel LLM into its own Onedata space lets the suite
   run without cross-LLM fixture pollution and unlocks LLM-level
   parallelism. None of the surveyed benchmarks (MCP-Bench / MCP-Universe
   / MCPEval / OSWorld-MCP / MCPAgentBench) describes per-model state
   isolation at the data-layer level — they either ephemeralise per-task
   or rely on an LLM judge to ignore residue. The pattern is general
   enough to lift out of Onedata.
4. **The M-1..M-12 "issues only LLM agents surface, not unit tests"
   finding shape.** Our 158 unit tests + 18 scenarios surface *different*
   classes of issues; the M-* issues are the ones unit tests miss.
   Comparable to the way MCP-Universe finds its 56% headroom, but with a
   structured taxonomy of *server-design* issues rather than *agent
   capability* issues. I did not find a published catalogue of
   server-side fixes driven by agent failures of this shape.
5. **L-1 / L-2 LLM-output-stability findings as paper data points.**
   This is a methodology contribution — multiple sweeps reveal that part
   of the variance the harness sees is in the *model's* output stability,
   not the server. Most surveyed benchmarks treat each task as one-shot
   (BFCL) or report `pass^k` to characterise reliability (τ-bench). A
   structured separation of "server-induced variance" from
   "model-induced variance" appears under-reported and is one of the more
   interesting things to land cleanly in the paper.

What we should be **careful not to claim**:

- That LLM-in-the-loop testing of MCP servers is novel — it is
  **uncommon in shipped servers** but standard in the academic
  benchmarking literature. The novel framing is *iterative server
  refinement driven by LLM-agent failures inside a federation oracle*,
  not the existence of LLM-in-the-loop testing.
- That a multi-LLM panel is novel — leaderboards have done this for
  years.
- That FastMCP unit-testing or Inspector debugging are novel — both are
  table stakes.

---

## Sources (canonical)

- [modelcontextprotocol GitHub org](https://github.com/modelcontextprotocol)
- [modelcontextprotocol/servers (reference servers)](https://github.com/modelcontextprotocol/servers)
- [modelcontextprotocol/conformance (official conformance suite, v0.1.16 March 2026)](https://github.com/modelcontextprotocol/conformance)
- [modelcontextprotocol/inspector (official debugger)](https://github.com/modelcontextprotocol/inspector)
- [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) and [issue #1252 — recommended way of unit-testing MCP](https://github.com/modelcontextprotocol/python-sdk/issues/1252)
- [MCP spec, 2025-11-25 revision](https://modelcontextprotocol.io/specification/2025-11-25)
- [MCP 2026 roadmap](https://modelcontextprotocol.io/development/roadmap) and [WorkOS analysis](https://workos.com/blog/2026-mcp-roadmap-enterprise-readiness)
- [Inspector docs](https://modelcontextprotocol.io/docs/tools/inspector)
- [FastMCP welcome](https://gofastmcp.com/getting-started/welcome) and [tests doc](https://gofastmcp.com/development/tests)
- [Lowin: "Stop vibe-testing your MCP server" (2026)](https://jlowin.dev/blog/stop-vibe-testing-mcp-servers)
- [Gunndu: "Your MCP server has no tests. Here are 4 patterns to fix that"](https://dev.to/klement_gunndu/your-mcp-server-has-no-tests-here-are-4-patterns-to-fix-that-2k59)
- [mcpcat.io: Unit testing MCP servers](https://mcpcat.io/guides/writing-unit-tests-mcp-servers/) and [Inspector setup](https://mcpcat.io/guides/setting-up-mcp-inspector-server-testing/)
- [Anthropic: Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents) and [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
- [MCP Cheat Sheet 2026 (Webfuse)](https://www.webfuse.com/mcp-cheat-sheet)
- [BFCL leaderboard](https://gorilla.cs.berkeley.edu/leaderboard.html) and [BFCL paper (ICML 2025)](https://openreview.net/forum?id=2GmDdhBdDk)
- [τ-bench](https://github.com/sierra-research/tau-bench) and [τ²-bench paper (arXiv:2506.07982)](https://arxiv.org/pdf/2506.07982)
- [MCP-Bench (arXiv:2508.20453)](https://arxiv.org/abs/2508.20453) and [code](https://github.com/Accenture/mcp-bench)
- [MCP-Universe (Salesforce AI Research)](https://mcp-universe.github.io/)
- [MCPEval (arXiv:2507.12806)](https://arxiv.org/html/2507.12806)
- [OSWorld-MCP (arXiv:2510.24563, ICLR 2026)](https://arxiv.org/abs/2510.24563)
- [MCPAgentBench (arXiv:2512.24565)](https://arxiv.org/abs/2512.24565)
- [GitHub MCP server testing infrastructure (deepwiki)](https://deepwiki.com/github/github-mcp-server/4.3-local-development-setup)
- [thoughtspot/mcp-testing-kit](https://github.com/thoughtspot/mcp-testing-kit)
- [r-huijts/mcp-server-tester (WIP)](https://github.com/r-huijts/mcp-server-tester)
- [haakco/mcp-testing-framework](https://github.com/haakco/mcp-testing-framework)
- [ARM: Automate MCP testing with pytest+testcontainers+GH Actions](https://learn.arm.com/learning-paths/cross-platform/automate-mcp-with-testcontainers/github-actions-ci/)
- [testomat.io: 2026 MCP testing tools roundup](https://testomat.io/blog/mcp-server-testing-tools/)
