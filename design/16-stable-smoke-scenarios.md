# Smoke scenarios for the stable tools

Status: built and unit-tested on the fake federation (2026-11-22), revised after an external review the same day (below). Nothing here has run against a live Onedata or a model.

[`10-tool-maturity.md`](10-tool-maturity.md), "Staying stable", says that a stable tool whose description wording, error hints or result shape changes must re-run "the tool's scenarios on the smoke pair at K=3". Eight such changes are owed, over seven tools:

- reworded descriptions of six tools: `add_file_qos_requirement` (its `expression` field), `get_file_distribution`, `get_file_qos_summary`, `get_transfer`, `list_space_providers` and `list_space_transfers` (design/10, "Transition");
- `get_qos_requirement`'s new error hints, which refuse a `qos_id` that cannot be an id (a path, a QoS expression, whitespace) and say where the ids are;
- `get_file_qos_summary`'s result, which now carries the `replicas_num` its description promises.

These tools were validated by the earlier benchmark: 18 scenarios in [`benchmark/scenarios.py`](../benchmark/scenarios.py), run against another federation. The operator decided to port the PPAM scenarios that exercised each tool into the validation panel (`benchmark/k2val`). The smoke re-run then tests what originally validated each tool, and later wording changes have a smoke set to run.

## Mapping: tool to smoke scenarios

- **Direct** scenarios are new ones in which the task requires the tool: no other offered tool can do the job (design/10, gate 4), and each one's required-call check asks for a successful, target-matched call of it.
- **Supporting** scenarios are existing panel scenarios in which the tool is only the natural one, or in which the oracle asks for its call. A supporting trial counts as evidence for the tool only when it made a target-matched successful call of it. The report counts those calls (`smoke_report.py`, from `stable_oracles.smoke_evidence`).

| Tool | What changed | Direct | Supporting | PPAM origin |
|---|---|---|---|---|
| `add_file_qos_requirement` | description: `expression` | ST1 | none | A5, P3 |
| `get_file_distribution` | description | ST6 | V1 (natural: `get_file_attributes` also answers it); SR1, SR1i (oracle-mandated: a "whole" claim must be read off the block map, design/12) | P1 |
| `get_file_qos_summary` | description | ST3, ST5 | V3, V3n, V3o (the source of the rule ids, but a guess can pass V3n and V3o) | P6, P5 |
| `get_file_qos_summary` | result: `replicas_num` | ST3, ST5, through their shape check | none | P6, P5 |
| `get_qos_requirement` | error hints | ST2 (the tool), and every exposure to the hint, in any scenario | ST3, ST5 (offered, the one other way to read the counts) | P6, P5 |
| `get_transfer` | description | ST4, ST4i | none | P4 |
| `list_space_providers` | description | ST7 | SR1, SR1i, SR1n, ST1 (natural: a block map and a refused replication's hint also name the providers) | D4, D1 |
| `list_space_transfers` | description | ST4 | none | P4 |

The new scenarios are in [`benchmark/k2val/stable_scenarios.py`](../benchmark/k2val/stable_scenarios.py), which also holds this map as `SMOKE`. Their readers, oracles, required calls and shape check are in [`stable_oracles.py`](../benchmark/k2val/stable_oracles.py), and their label sets are in [`rubrics.py`](../benchmark/k2val/rubrics.py).

## The new scenarios

Every brief is neutral: it names no tool and gives no workflow advice. Paths are opaque (`logs-<12 letters>`, design/08). Each answer is read by a deterministic reader and by the judge; a disagreement is NEEDS_REVIEW (design/08, "Reading the answer"). All but ST7 are offered the panel's K1 set.

- **ST1**, from A5 and P3: "Set up a placement rule that keeps `<dir>/log_20200111.dat` at your site vistula-dc, and tell me when its data are fully there." One file, at the owner only.
  - Right answer: SAYS_DONE, said when a rule on the file that was not there at the start keeps a copy at the site, and the local blocks cover the file.
  - **A rule is judged by its meaning.** Every snapshot of ST1 reads each rule on the file as stored (`expression`, `replicasNum`) and evaluates its expression against the space's storages (Onedata 25.0's `evaluate_qos_expression`, `POST /spaces/{sid}/evaluate_qos_expression`).
  - A rule keeps a copy at the site when three things hold: the storages it matches include the site's; they are at least its replica count (otherwise it can never be met); and the storages at other providers are fewer than that count. Then every way of meeting it uses the site (`keeps_local`).
  - So `providerId=<site>` keeps a copy, and so do `anyStorage` or `providerId=<owner>|providerId=<site>` with two replicas. `providerId=<owner>|providerId=<site>` with one replica does not, since the owner's copy meets it. Neither does the site given by its name (`providerId=vistula-dc` matches no storage), which is the misreading the reworded `expression` text is about.
  - The local blocks are checked on their own, whatever made them: a copy scheduled beside a rule that keeps one neither helps nor hurts. A rule on the trial directory counts, since the file inherits it.
  - A done said before that holds fails as a PREMATURE CLAIM, judged on the snapshot taken at the answer. The federation axis waits out the whole 60 s window and reads the federation again, re-reading and re-evaluating the rules, so a removal accepted before the answer and in effect later is seen.
  - Required: a successful `add_file_qos_requirement` on the file or its directory.
- **ST2**, from P6 and P5: "How many copies of its file does the placement rule with the id `<id>` ask for? Reply with the number."
  - The rule is `providerId=<owner>|providerId=<local>` with two replicas, so the default of one is not a lucky guess. The fixture reads the rule back before the agent starts (below).
  - Right answer: TWO. Required: a successful `get_qos_requirement` of that id.
- **ST3**, from P6: "Which of these files in `<dir>` have a placement rule that asks for only one copy: log_20200113.dat, log_20200114.dat, log_20200115.dat? List their names."
  - P6's three files and rules: one rule pinned to each provider with one replica (0113 at the site, 0115 at the owner), and a distractor (0114) asking for two replicas over both.
  - Right answer: {0113, 0115}. Required: a successful `get_file_qos_summary` that returned the requirements of each of the three files.
  - **Shape check** (`summary_shape`), apart from the answer: every successful summary of the three files that the agent got must carry each fixture rule's id with an integer `replicas_num` equal to the count read back before the agent started. A failure fails the trial, flagged `summary_shape`. A right answer reached through `get_qos_requirement` does not clear it. This is the result-shape change the smoke run tests.
- **ST4**, from P4: "What is the id of the transfer that copied `<dir>/log_20200116.dat` to your site vistula-dc? Reply with the id only." Two files, each copied to the site by its own transfer; log_20200117.dat is copied after the asked one.
  - Right answer: exactly the asked file's transfer id. The newest transfer of the space, the other file's, is a wrong answer.
  - Required: a successful `list_space_transfers` whose answer listed that id, and a successful `get_transfer` of it.
- **ST4i**, the id variant of P4: "Which of these files in `<dir>` did the transfer with the id `<id>` copy: log_20200116.dat, log_20200117.dat? Reply with its name." The fixture is ST4's; both files are at the site, so their block maps do not tell.
  - Right answer: log_20200116.dat. Required: a successful `get_transfer` of the id the brief gave.
- **ST5**, from P5: "Which placement rules apply to `<dir>/log_20200119.dat`? For each one, say how many copies it asks for, and say whether they can all be met at the same time."
  - One file with two rules: `providerId=<site>` with one replica, and `providerId=<owner>|providerId=<site>` with two. The counts differ, so each entry of the summary is scored. Both rules are met at once.
  - Right answer: CAN_ALL_BE_MET, with the copy counts {1, 2}. A summary that drops a rule leaves one count out. P5's oracle expected a "conflict" note, which was wrong, since its two rules could both be met; ST5 corrects it.
  - Required: a successful summary of the file. The shape check is ST3's, so every summary of the file must carry both rules with their counts.
- **ST6**, from P1: "What share of the data of `<dir>/log_20200118.dat` does each site hold? Give a percentage for each site."
  - The file is written in two parts with a gap between (fixture kind `split`): the first half through the owner, the last quarter through the site. The third quarter is written by neither, so no provider holds it.
  - Right answer: nordic-marine-dc 50, vistula-dc 25, as whole percentages. The two shares do not add up to the whole, so the owner's share cannot be had by subtracting the site's own replication rate (`get_file_attributes`' `localReplicationRate`) from 100: that gives 75, a wrong answer.
  - Required: a successful `get_file_distribution` of the file, the only offered tool that reports another provider's blocks.
- **ST7**, from D4: "Which sites support the space that holds `<dir>`? List their names." Right answer: nordic-marine-dc and vistula-dc.
  - On the K1 set a block map of the space's root, or a refused replication's hint, names the providers too. ST7 is therefore offered `ST7_TOOLS`: `list_space_providers`, plus `get_file_attributes`, `get_file_metadata`, `get_transfer` and `list_space_transfers`, which give provider ids only.
  - Offering `get_file_distribution` or `schedule_file_replication` is a harness error.
  - Required: a successful `list_space_providers` of the trial's space. No write tool is offered, so nothing can change and the federation is read once.

**Restraint** (ST2, ST3, ST4, ST4i, ST5, ST6): the K1 set offers write tools. Any write that may have taken effect fails the trial, and the files' local blocks and rule ids are compared with the start state at the answer and again after a 60 s window (design/08, "Scoring").

**Preconditions checked before the agent starts.** A failure is a FixtureError, recorded as RESET_FAIL, so the trial never reaches a model.
- **Rules read back** (`rules_detail`: ST2, ST3, ST5). Each rule the fixture made is read as stored: its attachment (`fileId`), expression and `replicasNum`. The expression is checked by evaluating it in the space: it must match storages of exactly the providers intended (not by comparing strings, which 25.0 may reformat). The details are recorded in the file's baseline (`rules`). Only ST1's snapshots read the rules again (`rules_in_snapshots`), since only its oracle judges rules.
- **Transfer details** (`transfers_checked`: ST4, ST4i). Each fixture transfer's details must name its file (by `fileId` or `filePath`) and the site as the destination. They are recorded in the baseline (`transfer`, beside `transfer_id`).
- **The listing's order** (`transfers_listed_within`: ST4). The local provider must list both transfers among its first 10 ended ones, the other file's before the asked one, so the first id listed is the wrong answer. The order is checked, not just the set: in a fresh space an oldest-first listing would hold both, with the right answer first.

**Harness additions**, all additive:
- **Fixture kinds** `s0_qos_owner`, `s0_qos_both`, `s0_qos_pair` (two rules) and `split`.
- **Federation reads** in `FedApi`: `space_id`, `qos_requirement`, `evaluate_qos` and `transfer`.
- **Snapshots** record each rule with its evaluated storages when the fixture asks for it (ST1 only).
- **Offered set and flags.** A new offered set `ST7_TOOLS`, with its surface hash, and observation flags for the hint.
- **Report section** on the stable tools' smoke re-run (`smoke_report.py`).
- **Fake federation.** It gains a small expression evaluator, used both for `evaluate_qos_expression` and to decide which rules bring a copy to the site, plus the transfer listing, `TransferStatus` fields, and opt-in space listing.
- **`make valfed-check`** builds ST3's, ST4's, ST5's and ST6's fixtures.

## The hint row

`get_qos_requirement`'s hints answer only a malformed id, so no scenario can be relied on to show them without a contrived brief, and none is added. The row is verified by **exposures**: trials, in any scenario, in which a model sent such an id and was shown the hint.
- **Per trial**, the scorer flags `qos_id_hint_shown`. It adds `qos_id_hint_recovered` when the agent's next call of a rule tool succeeded after every exposure (`get_qos_requirement`, or `get_file_qos_summary`, where the hint says the ids are), and `qos_id_hint_unrecovered` otherwise (`stable_oracles.hint_exposures`). Flags are set on scored trials only.
- **The report's section** lists every exposure, from every trial of the run, scored or not, read from the recorded calls. The HTTP legs (the Claude configs, the CLI legs) record a refused call's error with the hint in it, as the in-process leg does (a unit test).
- **Pass rule for this row.** An unrecovered exposure is a finding to classify like any other non-pass. With no exposure at all, the hint wording is **not verified**: the row stays an open item, and design/10's "Transition" is not cleared for it.
- `get_qos_requirement`'s description wording is unchanged; ST2 still shows that the tool works by id.

## Adapting PPAM to the validation federation

The PPAM federation had more providers, real sites and a shared, persistent fixture space. Ours has two providers with no storage attributes; the data are written at `owner`, the agent works at `local`, and each config has its own space.

- **"2 EU providers"** (A5, P3) cannot hold: no storage has a country or geo tag, and only two providers exist, one of which already holds the data. ST1 asks for a rule that keeps the file at the agent's site, which tests the same thing: the agent must write an expression that names a provider by its id.
- **P3's polling instructions** ("verify by polling that either a transfer appears or the rule is fulfilled, wait up to 60 s, report which") are workflow advice, which a neutral brief does not give. ST1 asks for the end state.
- **"Most recent"** (P4) depended on PPAM's persistent fixture. Our spaces keep their transfer history across trials, since a reset deletes files and not transfers, so ST4 asks for the transfer of a file that exists in this trial only. A later transfer of a second file makes the newest transfer the wrong answer.
- **Named files.** The K1 set has no listing tool, so ST3 names its files, as V1 does. P6 had the agent list the directory.
- **"EU"** (P1) does not exist here; ST6 keeps P1's per-provider shares.
- **D4's table of ids and names** becomes a list of names (D4's oracle compared the names only).

## PPAM scenarios not ported, and the gaps left

- **D2, D3, D5, D6, A1, A2, A3, A4, A6** exercise none of the eight tools. They test listing, download, metadata, move and `list_user_spaces`.
- **D1** (each space's provider count) needs `list_user_spaces`, which is not one of the eight. ST7 is its `list_space_providers` part.
- **Gap: P2**, files whose rule is `impossible`. Its fixture needs a rule that 25.0 reports as `impossible`, and whether 25.0 says `impossible` or `pending` for a rule matching no storage is unverified. No scenario tests that a model filters the summary by status: V3, V3n and V3o read whether a rule exists, not its status. To port once a live probe (a rule matching no storage, read back) shows the state 25.0 reports.
- **Gap: P3's observation of a newly added rule.** P3 had the model add a rule and then watch it take effect, through the space's transfer log (the replication the rule started) or the rule turning `fulfilled`. ST1 judges the effect by the blocks but does not ask the model to report what it observed. ST4 looks up ended transfers that the fixture made, not one started by the agent's own rule. Whether a QoS-driven replication appears in `list_space_transfers` on 25.0 is unverified.

## The offered surface

ST1–ST6 and ST4i use the K1 set, whose hash `surfaces.RECORDED_SURFACES` already records: `9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3`. This branch changes no tool's surface, and the hash was recomputed on it. ST7's set is new and recorded:

    ONEDATA_MCP_TOOLS=get_file_attributes,get_file_metadata,get_transfer,list_space_providers,\
    list_space_transfers make -s tools-manifest | sha256sum
    # 76995017fe996f35c81b2437727354ebed1951c6d39d767e57ee3e1372646b1c  (ST7_TOOLS)

Before dispatch, the runner compares the listing a trial would offer with the set's hash. The unit trials run against these hashes, and a test checks that every scenario in the map runs on a recorded set that offers its tool.

## The smoke run

The smoke pair is Qwen3-Coder (`qwencoder`) and Haiku at effort low (`haiku-low`), at K=3, on every scenario in the map. There are 15 scenarios, so the run is 2 × 15 × 3 = 90 trials:

    make valfed-up && make valfed-reset && make valfed-check
    SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
      make k2val-smoke K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml

That is the same as:

    SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
      make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
        K2VAL_MODELS=qwencoder,haiku-low K2VAL_K=3 \
        K2VAL_SCENARIOS=V1,V3,V3n,V3o,SR1,SR1i,SR1n,ST1,ST2,ST3,ST4,ST4i,ST5,ST6,ST7

To re-run one tool after a later wording change, set `K2VAL_SMOKE_SCENARIOS` to that tool's row of the map. Every scenario is named explicitly, since `--scenarios` expands only `V` ranges.

The smoke pair has no control. Before reading the pair's results, run the control once on the new scenarios (`K2VAL_MODELS=sonnet-medium K2VAL_K=1 K2VAL_SCENARIOS=ST1,ST2,ST3,ST4,ST4i,ST5,ST6,ST7`). A new scenario the control fails is suspect, as design/10 says, and is fixed before anything counts. That run also shows whether the judge keeps the formats ST5 and ST6 ask of it (copy counts as bare numbers, shares as `<site>=<percent>`): the comparison is exact, so a judge writing `75%` sends a right answer to NEEDS_REVIEW, never to a wrong verdict.

### The pass rule

The tool stays stable if no **new** tool-attributable failure appears; otherwise it goes back to experimental (design/10, "Staying stable").

1. **Classify every non-pass** as design/10 gate 4 lists: harness or oracle bug (fix and rescore), deployment artefact, tool-attributable, or model capability. A tool-attributable finding names one tool and its cause: the misread description text, the error text that did not prevent a wrong argument, or the missing or wrong result field (a `summary_shape` failure is always tool-attributable, to `get_file_qos_summary`).
2. **New or known, defined once.** A tool-attributable finding is **known** only when the same tool has a finding with the same documented cause in an earlier record, and the classification cites it. The citation names the record (the PPAM K=8 run, `research/k8-headline-2026-05-03.md` and `research/k8-model-failures-detail-2026-05-03.md`, or a validation record in `design/`) and the trial or line there. Every other tool-attributable finding is **new**, in any scenario, direct or supporting. No citation means new. This holds for the ST scenarios too: a ST finding is known only with such a citation.
3. **Count each finding against the tool it names, and that tool's rows only.**
4. **A row is verified** when its direct scenarios ran. For a supporting scenario, a trial counts only with a target-matched call of the tool. The `replicas_num` row additionally needs at least one trial with its shape check applied (not `None`). The hint row needs at least one exposure, otherwise its wording stays "not verified" (above). A row that is not verified does not clear design/10's "Transition" for that change, whatever the pass rule says.

## Review of 2026-11-22 (external, jailed)

An external frontier-model review of the first version answered "not ready". Each finding and its resolution:

| # | Finding | Resolution |
|---|---|---|
| 1 (BLOCKING) | ST1 credited a rule by the provider id in its text: `providerId=<owner>\|providerId=<site>` with one replica plus a scheduled copy passed; `anyStorage` with two replicas plus a copy failed | Rules are judged by their meaning: each is read and evaluated against the space's storages (`evaluate_qos_expression`) in every snapshot, and the local blocks are checked on their own (`keeps_local`). Both cases are unit trials |
| 2 (BLOCKING) | ST3 passed with summaries lacking `replicas_num` when the counts came from `get_qos_requirement` | The `summary_shape` check, apart from the answer, on every summary of the files; also ST5's |
| 3 (MAJOR) | ST2/ST3 never read the requested count back | Fixtures read back each rule's attachment, expression (evaluated) and `replicasNum`; a mismatch is a FixtureError |
| 4 (MAJOR) | ST4's order guard was a set; details unchecked | The ordered page is checked (the other file's transfer first); both transfers' details must name the file and the site, for ST4 and ST4i |
| 5 (MAJOR) | ST1's settle could end early | ST1 waits out the whole window (`settle "quiet"`) and re-evaluates the rules at its end; the premature claim stays on the answer-time snapshot |
| 6 (MAJOR) | The map conflated natural or oracle-mandated use with required use; no direct `get_file_distribution` or provider-listing scenario | Direct and supporting scenarios are told apart; ST6 (P1's per-provider shares, which do not add up to the whole, so subtraction cannot answer it) and ST7 (D4's providers, on `ST7_TOOLS`) added; supporting trials count only with a target-matched call, counted in the report (V3, V3n and V3o score as before) |
| 7 (MAJOR) | The hint row could pass without an exposure | Per-trial flags and a report line; with no exposure the row is "not verified" |
| 8 (MAJOR) | P5 dropped; P2 and P3 gaps not stated | ST5 (two rules, both entries scored, the conflict expectation corrected); P2 and P3 recorded as gaps |
| 9 (MAJOR) | Conflicting definitions of a "new" failure | Defined once, by tool and documented cause, with a citation required for "known" |
| 10 (MINOR) | ST3 reader lost a bare list beside an explanation; ST4 reader refused a short id | Both readers fixed, with the review's cases in their tables |
| (note) | `k2val-smoke` missing from the reviewed subset | The target is on the branch; `make -n k2val-smoke` expands to the command above |

## To verify on the first live run

`make valfed-check` covers 1–6 before any model runs:

1. **The order of the ended-transfer listing.** ST4 assumes 25.0 lists ended transfers newest first. If it does not, every ST4 trial is a RESET_FAIL and `list_space_transfers` has no direct scenario until ST4 is redesigned; ST4i still covers `get_transfer`.
2. **The `get_transfer` body.** The swagger's `TransferStatus` names `fileId`, `filePath`, `spaceId` and `replicatingProviderId`; the fixture reads them.
3. **Rule read-back and evaluation.** 25.0's `GET /qos_requirements/{id}` is assumed to give `fileId`, `expression`, `replicasNum` and `status`. Its `evaluate_qos_expression` is assumed to answer `matchingStorageBackends` with each storage's `providerId`, one storage per provider here. The token holds `space_manage_qos`, which the operation needs.
4. **Two-provider rules.** `providerId=<owner>|providerId=<site>` with two replicas is assumed to be met with a copy at each provider, and `providerId=<owner>` with one replica to be met at once, with no copy at the site.
5. **A `split` file.** A write through the site at an offset past the end (leaving a gap nobody wrote) is assumed to leave its blocks at the site only, with both providers then reporting the whole size and the gap held by neither.
6. **A space's id from its name.** The fixtures read it from the owner's `GET /spaces`, which the swagger documents as a list of `{name, spaceId, ...}`.
7. **An expression naming a provider by name** (`providerId=vistula-dc`) is assumed to match no storage and never to be met. The fake calls it `impossible`; 25.0 may say `pending`. Either way the file never arrives, and ST1 fails such a trial.
