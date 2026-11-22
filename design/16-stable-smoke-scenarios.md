# Smoke scenarios for the stable tools

Status: built and unit-tested on the fake federation (2026-11-22). Nothing here has run against a live Onedata or a model.

[`10-tool-maturity.md`](10-tool-maturity.md), "Staying stable", says that a stable tool whose description wording, error hints or result shape changes must re-run "the tool's scenarios on the smoke pair at K=3". Eight such changes are owed:

- reworded descriptions of six tools: `add_file_qos_requirement` (its `expression` field), `get_file_distribution`, `get_file_qos_summary`, `get_transfer`, `list_space_providers` and `list_space_transfers` (design/10, "Transition");
- `get_qos_requirement`'s new error hints, which refuse a `qos_id` that cannot be an id (a path, a QoS expression, whitespace) and say where the ids are;
- `get_file_qos_summary`'s result, which now carries the `replicas_num` its description promises.

These tools were validated by the earlier benchmark: 18 scenarios in [`benchmark/scenarios.py`](../benchmark/scenarios.py), run against another federation. The operator decided to port the PPAM scenarios that exercised each tool into the validation panel (`benchmark/k2val`). The smoke re-run then tests what originally validated each tool, and later wording changes have a smoke set to run.

## Mapping: tool to smoke scenarios

"Required" means no other offered tool can do the job (design/10, gate 4); "natural" means the tool is the obvious one, but another offered tool could also do it.

| Tool | What changed | Smoke scenarios | Role there | PPAM origin |
|---|---|---|---|---|
| `add_file_qos_requirement` | description: `expression` | **ST1** | required | A5, P3 |
| `get_file_distribution` | description | V1; SR1, SR1i | V1: natural (`get_file_attributes` also answers it, design/13); SR1, SR1i: required (a "whole" claim passes only when read off the block map after scheduling, design/12) | P1 |
| `get_file_qos_summary` | description | V3, V3n, V3o | required: the only offered source of a file's rule ids, which these scenarios need to find a rule or rule one out | P2, P5, P6 |
| `get_file_qos_summary` | result: `replicas_num` | **ST3** | required: the only tool that lists a file's rules; its answer now holds each rule's replica count | P6 |
| `get_qos_requirement` | error hints | **ST2**; ST3 | ST2: required (the rule is given by id only); ST3: offered, the one other way to read the counts | P6, P5 |
| `get_transfer` | description | **ST4**, **ST4i** | required in both | P4 |
| `list_space_providers` | description | SR1, SR1i, SR1n, ST1 | natural: the site's id is needed (SR1, SR1i, ST1) or the site must be ruled out (SR1n). Never required: a block map names each provider (`providerName`), and a refused replication's hint lists the space's providers with their ids | D1, D4, P1 |
| `list_space_transfers` | description | **ST4** | required: the only tool that lists transfer ids | P4, P3 |

New scenarios are in bold. V1, V3, V3n, V3o, SR1, SR1i and SR1n already exist and serve as they are. The new ones are in [`benchmark/k2val/stable_scenarios.py`](../benchmark/k2val/stable_scenarios.py), which also holds this map as `SMOKE`; their readers, oracles and required calls are in [`stable_oracles.py`](../benchmark/k2val/stable_oracles.py), and their label sets in [`rubrics.py`](../benchmark/k2val/rubrics.py).

## The new scenarios

Every one is offered the panel's K1 set: all eight tools are in it, and its surface hash is recorded (below). Every brief is neutral: it names no tool and gives no workflow advice. Paths are opaque (`logs-<12 letters>`, design/08). Each answer is read by a deterministic reader and by the judge; a disagreement is NEEDS_REVIEW (design/08, "Reading the answer").

- **ST1**, from A5 and P3: "Set up a placement rule that keeps `<dir>/log_20200111.dat` at your site vistula-dc, and tell me when its data are fully there." One file, at the owner only.
  - Right answer: SAYS_DONE, said when a rule the agent made keeps the file at the site and the local blocks cover it.
  - The rule must keep the file at the site. Either its expression names the local provider's id (`providerId=<id>`, with no `\` exclusion), or no copy was scheduled by other means, so the local copy can only have come from the rule. A rule on the trial directory counts, since the file inherits it.
  - A done said before that holds fails as a PREMATURE CLAIM. The federation axis checks the same state after the 60 s settle.
  - Required: a successful `add_file_qos_requirement` on the file or its directory.
  - A rule naming the owner, or the site by its name (`providerId=vistula-dc`, which no provider has), never brings the data, and fails. That is the misreading the reworded `expression` text is about.
- **ST2**, from P6 and P5: "How many copies of its file does the placement rule with the id `<id>` ask for? Reply with the number."
  - The rule is `providerId=<owner>|providerId=<local>` with two replicas, so the default of one is not a lucky guess.
  - Right answer: TWO.
  - Required: a successful `get_qos_requirement` of that id.
  - Restraint.
- **ST3**, from P6: "Which of these files in `<dir>` have a placement rule that asks for only one copy: log_20200113.dat, log_20200114.dat, log_20200115.dat? List their names."
  - P6's three files and rules: one rule pinned to each provider with one replica (0113 at the site, 0115 at the owner), and a distractor (0114) asking for two replicas over both.
  - Right answer: {0113, 0115}.
  - Required: a successful `get_file_qos_summary` that returned the requirements of each of the three files.
  - Restraint.
- **ST4**, from P4: "What is the id of the transfer that copied `<dir>/log_20200116.dat` to your site vistula-dc? Reply with the id only." Two files, each copied to the site by its own transfer; log_20200117.dat is copied after the asked one.
  - Right answer: exactly the asked file's transfer id. The newest transfer of the space, the other file's, is a wrong answer.
  - Required: a successful `list_space_transfers` whose answer listed that id, and a successful `get_transfer` of it.
  - Restraint.
- **ST4i**, a variant of P4 that gives the transfer by its id: "Which of these files in `<dir>` did the transfer with the id `<id>` copy: log_20200116.dat, log_20200117.dat? Reply with its name." The fixture is ST4's. Both files are at the site, so their block maps do not tell.
  - Right answer: log_20200116.dat.
  - Required: a successful `get_transfer` of the id the brief gave.
  - Restraint.

**Restraint** (ST2, ST3, ST4, ST4i): the K1 set offers write tools. Any write that may have taken effect fails the trial, and the files' local blocks and rule ids are compared with the start state at the answer and again after a 60 s window (design/08, "Scoring").

**Harness additions**, all additive:
- **Fixture kinds** `s0_qos_owner` and `s0_qos_both` (`fixtures.py`).
- **Transfers.** The transfer that replicates an `s0_local` file is recorded in its baseline (`transfer_id`), and an id is available to a brief as `{ids[transfer:<file>]}`.
- **Listing bound.** A fixture may require its transfers to be among the first N ended transfers the local provider lists (`transfers_listed_within`; ST4 uses 10).
- **Fake federation.** It gains rules aimed at the owner or both providers, impossible rules, the transfer listing, the `TransferStatus` fields, and opt-in space listing so that a tool given a space name resolves it.
- **`make valfed-check`** now also builds ST3's and ST4's fixtures.

## Adapting PPAM to the validation federation

The PPAM federation had more providers, real sites and a shared, persistent fixture space. Ours has two providers with no storage attributes; the data are written at `owner`, the agent works at `local`, and each config has its own space.

- **"2 EU providers"** (A5, P3) cannot hold: no storage has a country or geo tag, and only two providers exist, one of which already holds the data. The port asks for a rule that keeps the file at the agent's site, which tests the same thing: the agent must write an expression that names a provider by its id.
- **P3's polling instructions** ("verify by polling that either a transfer appears or the rule is fulfilled, wait up to 60 s, report which") are workflow advice, which a neutral brief does not give. ST1 asks for the end state and judges it by the blocks at the answer.
- **"Most recent"** (P4) depended on PPAM's persistent fixture. Our spaces keep their transfer history across trials, since a reset deletes files and not transfers, so ST4 asks for the transfer of a file that exists in this trial only. It adds a later transfer of a second file, so the newest transfer is not the answer.
- **Named files.** The K1 set has no listing tool, so ST3 names its files, as V1 does. P6 had the agent list the directory.

## PPAM scenarios not ported

- **D2, D3, D5, D6, A1, A2, A3, A4, A6** exercise none of the eight tools. They test listing, download, metadata, move and `list_user_spaces`.
- **D1** needs `list_user_spaces`, which is not one of the eight. Its `list_space_providers` part is served by SR1, SR1i, SR1n and ST1.
- **D4** (the providers of a space, as a table) is served by the SR scenarios and ST1, as the operator decided. No K1 scenario can require `list_space_providers`, because a block map names each provider.
- **P1** (each provider's share of a file's blocks; which EU providers hold all of it) is the same question V1 asks on our layout. Its "EU" attribute does not exist here.
- **P2** (files whose rule is `impossible`) is not ported: its fixture would need a rule that 25.0 reports as `impossible`. Whether 25.0 says `impossible` or `pending` for a rule naming an unknown provider is unverified, and the summary's wording is already covered by V3, V3n and V3o.
- **P5** (two rules on one file, "mutually exclusive") does not survive the move. The panel's fixtures keep one rule per file (`qos_rules`, `fixture_qos`). P5's own premise was also false: its two rules can both be met. Its replica-count reading is ST3's.

## The offered surface

All five new scenarios use the K1 set, so no new offered set is added. `surfaces.RECORDED_SURFACES` already records the K1 set's hash, `9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3`. This branch changes no tool's surface, and the hash was recomputed on it:

    ONEDATA_MCP_TOOLS=add_file_qos_requirement,get_file_attributes,get_file_distribution,\
    get_file_metadata,get_file_qos_summary,get_qos_requirement,get_transfer,list_space_providers,\
    list_space_transfers,remove_qos_requirement,schedule_file_replication \
      make -s tools-manifest | sha256sum      # 9192d3f5...

The runner compares the listing a trial would offer with that hash before dispatch, and the unit trials run against it. A test checks that every scenario in the map runs on a recorded set that offers its tool.

## The smoke run

The smoke pair is Qwen3-Coder (`qwencoder`) and Haiku at effort low (`haiku-low`), at K=3, on every scenario in the map. There are 12 scenarios, so the run is 2 × 12 × 3 = 72 trials:

    make valfed-up && make valfed-reset
    SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
      make k2val-smoke K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml

That is the same as:

    SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
      make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
        K2VAL_MODELS=qwencoder,haiku-low K2VAL_K=3 \
        K2VAL_SCENARIOS=V1,V3,V3n,V3o,SR1,SR1i,SR1n,ST1,ST2,ST3,ST4,ST4i

To re-run one tool after a later wording change, set `K2VAL_SMOKE_SCENARIOS` to that tool's row of the map. Every scenario is named explicitly, since `--scenarios` expands only `V` ranges.

**The pass rule** (design/10, "Staying stable"): the tool stays stable if no **new** tool-attributable failure appears; otherwise it goes back to experimental.
- Every non-pass is classified first, as design/10 gate 4 lists: harness or oracle bug (fix and rescore), deployment artefact, tool-attributable, or model capability. Each one names the tool it is attributed to.
- Failures attributed to a tool count only against that tool's row.
- A tool-attributable failure is new unless the same failure, with the same cause, is in the tool's earlier record: the PPAM K=8 run (`research/k8-headline-2026-05-03.md`, `research/k8-model-failures-detail-2026-05-03.md`).
- The ST scenarios have no earlier result. Nor, in practice, do the reused panel scenarios, whose promotion runs are still pending (design/11–13, "Results: TODO"). For all of them, any tool-attributable failure is new.
- The smoke pair has no control. Before reading the pair's results, run the control once on the five new scenarios (`K2VAL_MODELS=sonnet-medium K2VAL_K=1 K2VAL_SCENARIOS=ST1,ST2,ST3,ST4,ST4i`). A new scenario the control fails is suspect, as design/10 says, and is fixed before anything counts.

## To verify on the first live run

Each of these is an assumption the unit tests make on the fake federation:

1. **The order of the ended-transfer listing.** ST4 assumes 25.0 lists ended transfers newest first. The fixture checks it before every trial: if the two transfers are not among the first 10, the trial is a RESET_FAIL and never reaches a model. `make valfed-check` builds ST4's fixture, so it shows the order before any model runs. If the order is oldest first, ST4 must be redesigned; ST4i still covers `get_transfer`.
2. **The `get_transfer` body.** The swagger's `TransferStatus` names `filePath`, `fileId`, `spaceId` and `replicatingProviderId`; the fake answers with those. The fixture reads `spaceId`.
3. **Two-provider rules.** `providerId=<owner>|providerId=<local>` with two replicas is assumed to be fulfilled with a copy at each provider, and `providerId=<owner>` with one replica to be fulfilled at once, with no copy at the site. `make valfed-check` builds ST3's fixture.
4. **An expression naming a provider by name** (`providerId=vistula-dc`) is assumed never to be fulfilled. The fake calls it `impossible`; 25.0 may say `pending`. Either way the file never arrives, and ST1 fails such a trial.
