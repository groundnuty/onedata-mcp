# Paper-positioning recommendations for the PPAM 2026 onedata-mcp paper

**Audience**: the paper-writing agent.
**Purpose**: distil what to claim, what to hedge, what to cite, and how to
structure the contribution narrative — derived from the empirical work
done across this codebase + the MCP-ecosystem survey
(`mcp-ecosystem-state-2026-05.md`).

This is **not** a paper draft. It's a working doc to inform paper structure
and claim calibration. The actual prose, LaTeX, citations, and figures
are the paper-writing agent's job.

---

## 1. Claim calibration ladder

Each claim below is graded by how strongly the empirical record supports
it. Use the ladder when phrasing claims in the paper.

### 1.1 SAFE TO CLAIM (strong evidence + ecosystem gap matches)

| # | Claim | Evidence |
|---|---|---|
| C1 | Multi-LLM panel pressure surfaces MCP-server design issues that synthetic unit tests miss | M-1..M-12 catalogue. 109 unit tests passed at every M-N iteration; the issues only manifested when an LLM agent navigated the live federation. 9 of 12 issues found this way. |
| C2 | A two-axis oracle (`mcp_pass` × `federation_pass`) is necessary for multi-provider federated-data agents | A2 case (2026-05-01): single-axis oracle would have masked an Onedata divergence (mcp_pass=true but federation_pass=false). τ-bench has DB-state-vs-goal-state pattern; no precedent for *federated* version. |
| C3 | Local-deployable open-weight LLMs (Apache-2.0/MIT) can match closed frontier on agentic federated-data workflows at K=1 | 4× 18/18: Sonnet (closed), V4-pro (MIT), Devstral (mod-MIT, EU), Gemma-4 (Apache-2.0). Three OSS legs match the closed reference. |
| C4 | An end-to-end EU-sovereign agentic stack is feasible | Cyfronet+IISAS compute + Onedata+SPICE federation + Mistral Devstral model + local agent harness = 18/18 K=1. Devstral is the only EU-HQ frontier model in the panel. |
| C5 | LLM-induced output variance has distinguishable layers (model-fundamental vs serving-template vs serving-parser) | L-1 (model), L-2 (template, resolved by `--reasoning-parser`), L-3 (parser, partial-coverage on multi-call chains). Three controlled-fix experiments pinned the layers. |
| C6 | Per-LLM-space isolation enables safe LLM-level parallelism in benchmarks against shared filesystem-like APIs | 5-LLM concurrent K=1 sweeps against the same Onedata federation completed without cross-pollution; no precedent found. |

### 1.2 CLAIM CAREFULLY (defensible but contestable)

| # | Claim | Hedge guidance |
|---|---|---|
| H1 | "MCP servers are typically shipped without automated tests" | Cite Lowin + Gunndu primary sources. Don't generalise to "all"; the official servers repo *does* have tests — they're just not federation-state-aware. |
| H2 | "No semantic-level testing framework exists for MCP servers" | True as of May 2026 to the best of the survey. Hedge: "we found no published framework as of May 2026". The conformance suite tests *protocol*, not *semantics*. |
| H3 | The 7-model panel is representative | Acknowledge the bias: Cyfronet Forge dictated which OSS models we could test cheaply (Qwen, GLM); local vLLM dictated others (Gemma, Granite, Devstral). The panel is "what's deployable in May 2026 EU contexts", not "frontier-coverage". |
| H4 | LLM-rendered output corruption (L-2 underscore insertion) is operationally critical | Strong example for the paper but K=1 only — phrase as "an instance of the class" rather than "we measure how often this happens". K=8 would strengthen if completed before submission. |

### 1.3 DO NOT CLAIM (overreach risks)

| # | Don't say | Why not |
|---|---|---|
| N1 | "We invented MCP-server testing" | Inspector exists; FastMCP unit-test pattern exists; conformance suite exists. We added a *layer*, not the foundation. |
| N2 | "Open-weight LLMs match closed frontier in general" | Our K=1 results show parity *on this benchmark*. ~7 scenarios per band. Don't generalise to coding/math/reasoning benchmarks. |
| N3 | "EU sovereignty in AI is achievable" (broad claim) | We show *one* configuration works end-to-end. Don't extrapolate to "Europe doesn't need US/Chinese AI". Stay scoped to "the Onedata-MCP-Devstral stack is end-to-end EU-sovereign for federated-data agentic workflows". |
| N4 | "GLM is a poor model" | GLM is intentionally the deliberate-weak leg to show the benchmark discriminates. Frame GLM's lower score as *benchmark sensitivity*, not *model quality*. |
| N5 | Production-ready / commercial-ready language | Modified-MIT licence on Devstral has the $20M revenue cap. Acceptable for "academic + public-sector deployment", not blanket "production". Phrase accordingly. |

---

## 2. Recommended paper structure (mapping to research docs)

The paper-writing agent should map the existing research docs into
sections roughly as follows. This is a suggestion — the agent owns
final structure decisions.

```
§1 Introduction
   — claim C3, C4 in compressed form
   — motivate via federation/agentic workflow gap

§2 Related work
   — `research/mcp-ecosystem-state-2026-05.md`         ← bucket 1, 2, 3
   — emphasise MCP-protocol-vs-MCP-semantic test gap
   — cite τ-bench, BFCL, MCP-Bench, MCPEval, MCP-Universe
   — call out the federated-multi-provider gap (no precedent)

§3 Background: Onedata + MCP
   — 14-tool MCP server overview
   — federation topology (cloud-pl + Cloud-SK)
   — `research/scenario-catalogue.md` summary
   — `research/empirical-onedata-25.0-findings.md` lifts here for §3 background

§4 Methodology
   — two-axis OracleResult (C2) — make this the headline contribution
   — federation-reset protocol (5-phase, RESET_HARD_CAP, retry-with-backoff)
   — per-LLM-space architecture (C6)
   — multi-LLM panel composition + jurisdiction breakdown (C4)
   — figure: panel composition table by family/HQ/license/endpoint
   — figure: two-axis oracle quadrant (mcp_pass × federation_pass)

§5 Results
   — K=1 headline grid (use `make show-grid` output as the visual structure)
   — K=8 headline (when available — pass^k aggregated)
   — Per-fix lift attribution table (M-10 → D3, M-11 → A4, etc.)
   — Pre-fix vs post-fix progression (4-run progression in current-state-2026-05-02.md)
   — License/jurisdiction breakdown (table, with EU-sovereign row highlighted)

§6 Server-design findings (M-1..M-12)
   — `research/empirical-mcp-server-findings.md` ← direct lift
   — taxonomy: silent-fallback ergonomics, response-shape design, parameter-name fidelity
   — frame as "issues only LLM agents surface" (claim C1)

§7 Output-stability findings (L-1, L-2, L-3)
   — `research/llm-output-stability-findings.md` ← direct lift
   — L-1 = model-fundamental + oracle-loosened
   — L-2 = serving-template (resolved via vLLM flag)
   — L-3 = serving-parser (pending; vLLM-side regression)
   — frame as the layered variance taxonomy (claim C5)

§8 Threats to validity
   — single-federation (SPICE only)
   — K=1 vs K=8 stochasticity (GLM as canonical noise example)
   — tool-call-schema-driven by Onedata REST surface (other federations differ)
   — Devstral $20M revenue cap (not pure-permissive)
   — Granite L-3 unresolved (cannot quote final number)

§9 Discussion + future work
   — federation-aware MCP design as a generalisable pattern
   — applicability beyond Onedata (S3 + IAM, posix + LDAP, etc.)
   — vLLM serving-template fragility as a cross-cutting deployment lesson

§10 Reproducibility statement
   — repo branch `ppam2026/14-tools`, commit SHA at submission
   — `make sweep-k8` reproduces the headline
   — `make spaces-create && make spaces-support` provisions
   — federation token + per-LLM space registry shipped in repo
```

---

## 3. Headline-table recommendations

The K=1 (and K=8 when complete) result table is the most important
visual in the paper. Recommendations:

### 3.1 Sort + group by jurisdiction

```
Family         HQ          License         K=1     K=8
─────────────────────────────────────────────────────────
Anthropic      🇺🇸           proprietary     X/18    Y%/cell
─────────────────────────────────────────────────────────
Mistral        🇫🇷 (EU)      mod-MIT*        X/18    Y%/cell
─────────────────────────────────────────────────────────
Google         🇺🇸           Apache-2.0      X/18    Y%/cell
IBM            🇺🇸           Apache-2.0      X/18    Y%/cell
─────────────────────────────────────────────────────────
DeepSeek       🇨🇳           MIT             X/18    Y%/cell
Alibaba        🇨🇳           Apache-2.0      X/18    Y%/cell
Z.ai           🇨🇳           MIT             X/18    Y%/cell
```

Highlight the Mistral row with a pull-quote: "the only end-to-end
EU-sovereign configuration in the panel."

The closed Anthropic reference goes first as the frontier benchmark,
then the EU-HQ leg, then other open-weight grouped by HQ. Avoid
ordering purely by score — that buries the jurisdiction story.

### 3.2 The * footnote on Devstral's license

```
* Modified MIT license excludes commercial use by entities with
  consolidated monthly revenue > $20M (Mistral AI commercial
  licence terms, December 2025). Permissive for academic, research,
  and public-sector deployment. Mistral also publishes
  Devstral-Small-2-24B under plain Apache-2.0 for revenue-capped
  alternatives.
```

This footnote is non-negotiable. Reviewers will flag it. Better to own it.

### 3.3 Per-cell grid (D1..P6 × LLM)

Keep the per-cell ✓/✗ grid as a supplementary table or appendix.
Rationale: for reproducibility + showing where the panel discriminates
between scenarios. Generate via `make show-grid RID=<headline-run>`.

Use ✓ for PASS, ✗ for FAIL, — for not-yet-run, ⚠ for L-3-pending
(Granite A1/A2 if unresolved at submission).

### 3.4 Pre-fix vs post-fix delta

Showing M-10/M-11/M-12 lift attribution makes Claim C1 concrete.
The 4-run progression in `current-state-2026-05-02.md` § "Post-fix
progression" gives the data. Recommend a single-row delta visual:

```
M-10 (download_file size): D3 lift  pre 1/4 → post 4/4 fleet
M-11 (create_directory):   A4 lift  pre 2/4 → post 4/4 fleet
M-12 (prefix docstring):   D2 lift  pre 3/4 → post 4/4 fleet
```

---

## 4. Citation recommendations

From `mcp-ecosystem-state-2026-05.md`, prioritise these for
inline citation:

### Must cite (high signal)

- **Anthropic MCP spec** + protocol-conformance suite
  (`modelcontextprotocol/conformance`) — for the protocol-vs-semantic gap
- **FastMCP** — explicit reference; we use it
- **τ-bench** (Sierra Research, 2024) — closest precedent for
  state-based oracle scoring; cite to position the federated extension
- **BFCL** (Berkeley Function-Calling Leaderboard) — function-calling
  benchmark
- **MCP-Bench** (arXiv:2508.20453, 2025) — the best survey of
  models-on-MCP-servers; we invert
- **MCPEval** (arXiv:2507.12806, 2025) — MCP-specific evaluation work
- **Lowin "Stop vibe-testing"** (jlowin.dev) — primary source for "most
  MCP servers ship without tests" claim

### Cite when relevant

- **MCP-Universe** (mcp-universe.github.io) — adjacent benchmark
- **Anthropic MCP launch blog** — for the protocol-emergence narrative
- **OSWorld-MCP**, **MCPAgentBench** — tangentially related; cite if
  the paper has a benchmark-comparison section

### Onedata-side

- Onedata 25.0 paper / docs (the federation we run on)
- SPICE platform + Cyfronet/IISAS deployment notes
- The MACF-style provider-discovery patterns (paper-internal)

---

## 5. Risks to the paper's claims (where reviewers will push)

The paper-writing agent should preempt these in §8 (Threats to validity).

### 5.1 K=1 stochasticity (multiple reviewers will flag)

GLM's K=1 noise (11-15/18 across 4 runs) is the canonical example.
Other panel legs are stable, but the paper needs to acknowledge that
*K=1 numbers are point estimates, not means*. Either:

(a) Wait for K=8 before submission and report `pass^k` (preferred —
issue #22 in the repo)

(b) Report K=1 + explicitly mark single-trial estimates with a
"K=1, single trial" caveat in the table caption + a paragraph in §8

The repo's `current-state-2026-05-02.md` shows the 4-run progression
honestly. Use it for the §8 paragraph.

### 5.2 "You overlooked Granite's true score" (likely reviewer comment)

Granite K=1 v2 = 14/18, with 2 of the 4 fails (A1, A2) attributable
to L-3 (vLLM partial-parser coverage). Reviewers may say: "if the
deployment had the right serving config, Granite would score higher
— so 14/18 is unfair."

Counter-narrative: the paper is honestly measuring the *deployment*
that the operator can stand up today. The 14/18 is the post-state
of "applied all currently-known vLLM flags". L-3 documents the open
deployment-side issue. This is data, not a verdict on Granite-as-model.

### 5.3 "Why no Llama / why no GPT" (panel completeness)

Already addressed in `panel.py` comments + ecosystem doc. Llama 3.3
70B was probed and found inactive on Forge (skipped). GPT family was
not in the EU-deployable scope. Frame as "panel scope, not
arbitrary exclusion".

### 5.4 "Sonnet results are special-case" (closed-model reproducibility)

Sonnet runs via `claude-agent-sdk` over a local Claude Code session.
Reviewers may ask: "is this reproducible without Anthropic
infrastructure?" Answer: no, but Sonnet is the *frontier reference*,
not a candidate for production deployment in the paper's scope.
Document clearly.

### 5.5 "Federated state may have changed during your run"

Onedata 25.0 has eventual-consistency on multiple endpoints (M-2,
M-6, P4). Different sweep runs hit slightly different federation
states (e.g., transfer-log accumulation). The per-LLM-space
architecture mitigates intra-run pollution; cross-run consistency is
guaranteed by the federation reset protocol (Phase 1 wipe, Phase 4
convergence wait, RESET_HARD_CAP).

The paper should describe the reset protocol explicitly so reviewers
see this was thought through.

---

## 6. Narrative threads to emphasize

The paper has multiple stories braided together. The agent should
choose 2-3 to weave through; trying to tell all of them dilutes.

### Strongest threads (rank-ordered)

1. **"LLM agents surface MCP-server design issues that synthetic
   tests miss"** (claim C1, M-1..M-12). Most paper-grade thread.
   Direct contribution. Concrete, unambiguous evidence.

2. **"Open-weight LLMs can match closed frontier on federated-data
   agentic workflows under controlled deployment"** (C3, K=1
   numbers). Defensible if K=8 confirms. The "controlled deployment"
   modifier is doing important work — L-3 reminds us deployment
   matters.

3. **"End-to-end EU-sovereign agentic stack feasibility"** (C4,
   Mistral leg). Punchy headline; PPAM 2026 audience may particularly
   care; aligns with Cyfronet's institutional narrative.

### Weaker threads (mention but don't overweight)

- "Two-axis oracle is novel" — it's a useful contribution but not
  earth-shattering; treat as methods detail, not headline.
- "Per-LLM-space architecture" — useful pattern but very specific to
  Onedata; treat as methods detail.
- "L-1/L-2/L-3 layered variance" — interesting but somewhat in the
  weeds; appendix material unless reviewers ask.

### Stories to NOT tell

- "We accidentally found a tokenizer bug in Granite" — except L-2
  was *misdiagnosed* as a tokenizer bug. Don't claim a model-side
  finding when the resolution is on the deployment side.
- "We outperform MCP-Bench" — different research question. Don't
  benchmark against benchmarks.
- "Deployment matters for AI" — too generic; sharpen to the specific
  L-2/L-3 examples.

---

## 7. Reproducibility checklist for §10

The paper should include a reproducibility section. Items to check:

- [ ] Repo URL + branch + commit SHA at submission
- [ ] `make sweep-cyfronet` + `make sweep-deepseek` reproduce the
      headline numbers within K=1 noise
- [ ] `make sweep-k8` reproduces the K=8 headline (~3-4h wall)
- [ ] `make spaces-create && make spaces-support` provisions a fresh
      federation
- [ ] Every M-N + L-N has a docs entry pointing at code
- [ ] All 7 panel LLMs have `.env` template entries
- [ ] vLLM launch flags for Gemma + Granite documented
      (`--reasoning-parser gemma4`, `--tool-call-parser granite4`)
- [ ] Federation token expiration date noted
- [ ] Per-LLM space IDs registered in `_per_llm_spaces.py`

`research/current-state-2026-05-02.md` already covers most of this.

---

## 8. What's still missing for paper submission

Honest gap-list as of 2026-05-03:

1. **K=8 headline run not yet completed.** ~3-4h wall when ready.
   Without it, every K=1 number is a point estimate; reviewers will
   note. Highly recommend completing before submission.

2. **L-3 (Granite tool-call-parser) unresolved.** Pending vLLM-devops
   investigation. Even if unresolved, the paper can ship — frame as
   "open question, see Threats to Validity §8.2".

3. **Issue #22 pass^k aggregator** not yet implemented in `report.py`.
   Required to generate K=8 headline tables cleanly. Estimated
   1-2 hours of code.

4. **No comparative re-run on a second federation.** PPAM 2026 paper
   scope likely doesn't require it, but reviewers may note. Be ready
   to address.

5. **Mistral Devstral-Small-2-24B (Apache-2.0)** as an alternative
   if reviewers complain about the modified-MIT license. Not yet
   probed; the panel.py wiring would handle it via a new index.

---

## 9. Quick-reference fact sheet for the paper-writer

| Fact | Value | Source |
|---|---|---|
| Total scenarios | 18 (D1..D6, A1..A6, P1..P6) | `research/scenario-catalogue.md` |
| Total panel LLMs | 7 | `benchmark/panel.py` |
| Distinct LLM families | 6 (Anthropic, Alibaba, Z.ai, DeepSeek, Google, IBM, Mistral) | same |
| Distinct vendor jurisdictions | 4 (US, China, France, IBM-US) | same |
| OSS legs | 6 of 7 | same |
| EU-HQ legs | 1 (Mistral Devstral-2-123B) | same |
| Apache-2.0 / MIT legs | 5 of 6 OSS legs | license columns |
| Modified-MIT (revenue capped) | 1 (Devstral) | Mistral commercial terms |
| MCP-server issues found via panel | 12 (M-1..M-12) | `research/empirical-mcp-server-findings.md` |
| LLM-output-stability findings | 3 (L-1..L-3) | `research/llm-output-stability-findings.md` |
| Unit tests | 158 (was 109 before fixes) | `make test` |
| K=1 fleet @ 18/18 | 4 of 7 (Sonnet, V4-pro, Devstral, Gemma) | latest runs |
| Federation providers | 2 (cloud-pl, Cloud-SK) | `_federation_constants.py` |
| Federation country mix | 🇵🇱 + 🇸🇰 (both EU) | same |

---

## 10. Cross-references

- `research/mcp-ecosystem-state-2026-05.md` — full ecosystem survey
- `research/empirical-mcp-server-findings.md` — M-1..M-12
- `research/llm-output-stability-findings.md` — L-1..L-3
- `research/empirical-onedata-25.0-findings.md` — Onedata-side observations
- `research/scenario-catalogue.md` — full scenario reference
- `research/current-state-2026-05-02.md` — live snapshot of sweep results

The paper-writer should pull facts from these directly rather than
duplicating them in the paper body. If discrepancy, the in-repo doc is
canonical (it's auto-updated by the harness; the paper draft isn't).
