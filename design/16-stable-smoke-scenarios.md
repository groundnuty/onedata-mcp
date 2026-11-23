# Smoke scenarios for the stable tools

Status: built and unit-tested on the fake federation (2026-11-22), revised after an external review the same day (below). First run on the validation federation 2026-11-23, control and smoke pair, and classified ([Results](#results-of-the-first-smoke-run-2026-11-23)): `add_file_qos_requirement` had one new tool-attributable finding (SF1); `get_file_qos_summary` and `get_file_distribution` stayed stable only if a record that deferred a finding to this run could serve as its baseline (SF2, SF3; the operator's call); four rows stay stable; the hint row is not verified. The three findings are fixed in the results, errors and hints of those tools ([Fixes and the re-run](#fixes-for-sf1-to-sf3-and-their-re-run)), under design/10's rule that a tool is fixed and re-run, not demoted. The re-run, `smoke-stable-2` (2026-11-23), is clean ([Results](#results-of-the-second-smoke-run-2026-11-23)): the three tools stay stable, each fix was shown to a model and not misread, and two branches of the fixes that no model was shown stay not verified.

[`10-tool-maturity.md`](10-tool-maturity.md), "Staying stable", says that a stable tool whose description wording, error hints or result shape changes must re-run "the tool's scenarios on the smoke pair at K=3". Eight such changes are owed, over seven tools:

- reworded descriptions of six tools: `add_file_qos_requirement` (its `expression` field), `get_file_distribution`, `get_file_qos_summary`, `get_transfer`, `list_space_providers` and `list_space_transfers` (design/10, "Transition");
- `get_qos_requirement`'s new error hints, which refuse a `qos_id` that cannot be an id (a path, a QoS expression, whitespace) and say where the ids are;
- `get_file_qos_summary`'s result, which now carries the `replicas_num` its description promises.

The first run's findings add three more changes of stable tools' results and errors, each owing the same re-run ([Fixes and the re-run](#fixes-for-sf1-to-sf3-and-their-re-run)): `add_file_qos_requirement`'s refusal of a `providerId=` value that is not a provider's id (SF1), `get_file_qos_summary`'s `meaning` on an empty `requirements` (SF2), and `get_file_distribution`'s `holds_whole_file` per provider (SF3).

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

The tool stays stable if no **new** tool-attributable failure appears. If one does, the tool is fixed and its smoke scenarios are re-run at K=3; a clean re-run keeps it stable, and it goes back to experimental only if the fix fails or needs a change of name, input schema or annotations (design/10, "Staying stable", as amended 2026-11-23).

1. **Classify every non-pass** as design/10 gate 4 lists: harness or oracle bug (fix and rescore), deployment artefact, tool-attributable, or model capability. A tool-attributable finding names one tool and its cause: the misread description text, the error text that did not prevent a wrong argument, or the missing or wrong result field (a `summary_shape` failure is always tool-attributable, to `get_file_qos_summary`).
2. **New or known, defined once.** A tool-attributable finding is **known** only when the same tool has a finding with the same documented cause in an earlier record, and the classification cites it. The citation names the record (the PPAM K=8 run, `research/k8-headline-2026-05-03.md` and `research/k8-model-failures-detail-2026-05-03.md`, or a validation record in `design/`) and the trial or line there. Every other tool-attributable finding is **new**, in any scenario, direct or supporting. No citation means new. This holds for the ST scenarios too: a ST finding is known only with such a citation.
3. **Count each finding against the tool it names, and that tool's rows only.**
4. **A row is verified** when its direct scenarios ran. For a supporting scenario, a trial counts only with a target-matched call of the tool. The `replicas_num` row additionally needs at least one trial with its shape check applied (not `None`). The hint row needs at least one exposure, otherwise its wording stays "not verified" (above). A row that is not verified does not clear design/10's "Transition" for that change, whatever the pass rule says.

## Results of the first smoke run (2026-11-23)

Run on the validation federation on 2026-11-23, 00:07 to 01:35 UTC, at `8863c3e`, in two run ids in the durable evidence store:
- `smoke-control-1`: the control (Sonnet at effort medium) once on each new scenario (ST1–ST7, ST4i). It holds 14 records for 8 trials: 6 are superseded ADAPTER_ERROR records (the subscription's session expired mid-run; the resume re-ran those trials).
- `smoke-stable-1`: the smoke pair (Qwen3-Coder, Haiku at effort low) on the 15 mapped scenarios at K=3. It holds one record per trial, 90 in all.

The offered surfaces, read from every counted record, are the recorded ones: K1 `9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3` and ST7_TOOLS `76995017fe996f35c81b2437727354ebed1951c6d39d767e57ee3e1372646b1c`. No trial was a RESET_FAIL, so every fixture precondition above held. Answers were read by the deterministic reader and the judge (Claude Sonnet 5.5, effort medium).

The classes are design/10's, gate 4: **L1** harness or oracle bug, **L2** deployment artefact, **L3a** tool-attributable, **L3b** model capability. The L3a bar is the same diagnosis in at least two model families, or a control failure, or one model with a traced mechanism. Models of one vendor lineage are one family (the operator, 2026-11-23), so this run has two: Qwen and Anthropic (Haiku; the control is Anthropic too).

### Rescore

The stored answers were read again offline (`rejudge --cached-only`: the stored verdicts, no judge call), into new directories beside the runs, which were left unchanged (sha256 of every file checked before and after).

| Run | As stored (`8863c3e`) | master `56c0dc2` (#41's reader fixes) | with the fixes below | after review by hand |
|---|---|---|---|---|
| control (8) | 6 PASS, 2 NEEDS_REVIEW | 6 PASS, 2 NEEDS_REVIEW | 8 PASS | 8 PASS |
| pair (90) | 63 PASS, 11 FAIL, 16 NEEDS_REVIEW | 65 PASS, 10 FAIL, 15 NEEDS_REVIEW | 76 PASS, 10 FAIL, 4 NEEDS_REVIEW | 77 PASS, 13 FAIL |

#41 changed two Haiku trials: SR1 #1 (the read rule accepts `isFullyReplicatedLocally`) and SR1n #2 ("Once that's done, I can schedule" is a condition), both now PASS. The fixes below change 13 readings and no other reading of any stored run: the 912 scored answers of this run and of the gate-4 runs (design/12–15) were read at master and on the branch, and only these 13 differ. No stored PASS changed.

### L1: reader fixes (paired regression tests in `test/unit/k2val/test_smoke_readers.py`)

| # | Reader | Misread | Fix | Trials |
|---|---|---|---|---|
| S1 | ST1 `read_rule_task` | "The QoS summary still says `pending`", "Its status still reads `pending`", "The rule showed `pending` on the first check" undid "the data are now fully at your site" | a pending rule status that its own sentence explains away (a past reading, "on the first check"; a lag, "rather than missing data", "even though … already"; a coming change, "should flip") no longer undoes a clause saying the data are fully there. The rest of the sentence is still read ("status pending and the data are not yet there" stays not done), a status with no data claim stays not done, and a bare "the data are fully there, but the rule is still pending" still says both (superseded by the second run's S10: a status never undoes a claim that the data are there) | control #0; Haiku #0, #2 → PASS |
| S2 | ST3 `read_single_copy` | "2. `log_20200114.dat` - Has a requirement with replicas_num = 2": the dash cut the name from its count, and the bare name was read as claimed | a list item labelled by one name takes the count that follows it, when that text states one kind of count | Qwen3-Coder #0, #1, #2 → PASS |
| S3 | ST4 `read_transfer_ids` | "The replicating provider ID is `<id>`" not read as a label; "… the ID of the transfer … is:" then the id alone two lines below not read as the conclusion | "provider ID is" labels an id (an id word is required: "the file is X" is no label); the lead-in of an id on its own line is the text before it | Qwen3-Coder #0 → PASS |
| S4 | ST5 (and ST2) `_FIELD`, `_CAN_ALL` | "Number of Copies Requested: 2" gave no count; "Can they all be met at once? Yes." gave no verdict | the field with "requested", "required" and the like; a "Yes" right after the restated question about being met | Qwen3-Coder #1, #2, control #0 → PASS |
| S5 | ST6 `read_shares` | "1. Site: local" with "- Percentage of Data: 25%" on the sub-item below gave no share | the first percentage under an item that names one site; a blank line, a line naming another site, or a line that is not a list item ends it ("The remaining 25% …" is no site's) | Qwen3-Coder #0, #2 → PASS |
| S6 | V3o `oracles._HAS_RULE` | "Since the QoS requirement for the file specifies this provider, it means that the file is indeed kept at the local site" not read as yes | a rule noun, then "is kept / held / stored / pinned / placed at" in the same clause | Qwen3-Coder #2 → PASS |
| S7 | the re-judge report (`ST_RIGHT`) | ST6's and ST7's right readings named `vistula-dc` and `nordic-marine-dc`, so every right ST6 and ST7 answer on this federation (`owner`, `local`) was listed as "a stored PASS the judge reads as wrong" (scoring was not affected: the oracles use the trial's names) | both depend on the trial, like ST4's | none (report only) |

### NEEDS_REVIEW resolved by hand

- **Qwen3-Coder SR1 #2** (reader OTHER, judge SAYS_NOT_YET): SAYS_NOT_YET ("The transfer is currently in "enqueued" status"; "Once bytesReplicated equals 134217728, the data are fully there" is a condition). FAIL either way.
- **Qwen3-Coder SR1i #1** (reader OTHER, judge SAYS_WHOLE): SAYS_WHOLE ("the replication is currently in progress", then "The data are now fully present at the local site"). FAIL either way (`unread_claim`).
- **Qwen3-Coder SR1n #2** (reader SAYS_NOT_POSSIBLE, judge OTHER): SAYS_NOT_POSSIBLE ("Since "oder-dc" is not a provider for this space, I cannot directly replicate the file to that site"), no write. **PASS.** The answer ends on "Let me try to find if there's another way … `<tool_call>`": the turn ended on a tool call begun as text. The gate-4 run (design/12) has two answers of the same shape, Qwen3-Coder SR1n #1 and #3, which both readers read as SAYS_NOT_POSSIBLE and which are PASS there. No row depends on this trial.
- **Qwen3-Coder ST4 #2** (reader none, judge unclear): FAIL either way, and its required call is missing.

The SR readers' miss on "Once bytesReplicated equals …, the data are fully there" is left unfixed: the trial fails under either reading, and the reader is shared with design/12's record.

### PASS of 3, after classification

| Config | V1 | V3 | V3n | V3o | SR1 | SR1i | SR1n | ST1 | ST2 | ST3 | ST4 | ST4i | ST5 | ST6 | ST7 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| haiku-low | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 |
| qwencoder | 1 | 3 | 3 | 2 | 0 | 0 | 1 | 2 | 3 | 3 | 2 | 3 | 3 | 3 | 3 |

77 of 90 pass. The control passes each of its 8 trials (1 of 1 per scenario) once S1 and S4 are fixed. **No new scenario is DESIGN SUSPECT**: the control's two NEEDS_REVIEW were reader misreads, and its rescored trials pass every check. Pass rates are reported; they do not gate.

### Per tool

| Tool | Change | Direct (scored trials, pair) | Supporting (target-matched call) | Tool-attributable findings | Verdict |
|---|---|---|---|---|---|
| `add_file_qos_requirement` | description: `expression` | ST1: 6 | none | **SF1, new** | **AT RISK**; SF1 fixed (this PR), smoke re-run pending |
| `get_file_distribution` | description | ST6: 6 | 11 of 18 (V1, SR1, SR1i) | SF3, known only by a record that deferred it to this run | **STAYS STABLE by the letter of rule 2 only**; AT RISK if that record cannot be its baseline; SF3 fixed (this PR), smoke re-run pending |
| `get_file_qos_summary` | description | ST3, ST5: 12 | 18 of 18 (V3, V3n, V3o) | SF2, known only by a record that deferred it to this run | **STAYS STABLE by the letter of rule 2 only**; AT RISK if that record cannot be its baseline; SF2 fixed (this PR), smoke re-run pending |
| `get_file_qos_summary` | result: `replicas_num` | ST3, ST5: 12; shape check applied in 12 (control 2), failed in 0 | none | none | verified; the tool's verdict is the row above |
| `get_qos_requirement` | error hints | ST2: 6, all pass | 0 of 12 (ST3, ST5) | none | **NOT VERIFIED**: no exposure (open) |
| `get_transfer` | description | ST4, ST4i: 12 | none | F2 (design/12) recurs, known | **STAYS STABLE** |
| `list_space_providers` | description | ST7: 6 | 19 of 24 (SR1, SR1i, SR1n, ST1) | none | **STAYS STABLE** |
| `list_space_transfers` | description | ST4: 6 | none | none | **STAYS STABLE** |

- **The hint row.** No trial of either run was shown `get_qos_requirement`'s id-guard hint: the run made 10 calls of the tool (9 by the pair, 1 by the control), all with a real rule id, all successful. So its wording stays **not verified**, and design/10's "Transition" stays open for it, as the rule above says.
- **"Known" and the records that deferred to this run.** SF2 and SF3 are known by the letter of rule 2: a validation record documents each, same tool and cause (design/13 and design/12, 2026-11-22). But both records logged them "for its smoke re-run (design/16)", that is, for this run to judge, and both ran at the same surface as this run (K1 `9192d3f5…`); the PPAM K=8 run had no Qwen3-Coder. Taking them as the baseline is circular: the re-run they deferred to would never judge the finding. If a deferring record cannot be the baseline, SF2 and SF3 are new and both tools are AT RISK. That is the operator's decision (below). F2 is not in this position: design/12 classified it itself (L3a) and did not defer it, though its own bar now rests on the traced-mechanism clause (one family).

### Classification of every non-pass

**Tool-attributable (L3a)**

- **SF1 (new; `add_file_qos_requirement`): a provider's name in the `providerId=` operand, accepted silently.** *Fixed (this PR); smoke re-run pending.* Qwen3-Coder ST1 #0: [0] `add_file_qos_requirement(expression "providerId=local", replicas_num 1)` → `{"qosRequirementId": …}`, no hint; [1] the summary: `"status": "impossible"`. The answer reads the impossible rule as a site problem ("Your local site might not support the space that contains this file") and the file never arrives (0 of 262144 bytes after the settle). FAIL.
  - *The same slip, elsewhere in the run.* Qwen3-Coder ST1 #1 made it too ([0] `providerId=local`, `impossible`), then listed the providers on its own and added a rule by id ([7]): PASS. In SR1i #0, #1 and #2 the same model passed the name `local` as `schedule_file_replication`'s target and drew "Pass its id '…', not its name"; its next schedule passed an id each time (#1 and #2 the site's at once; #0 the owner's after listing the providers, then the site's). In the gate-4 run, design/12 counts ten trials that drew this hint, each followed by a schedule with the id.
  - *The evidence bar:* one family (Haiku and the control listed the providers first and used the id in all their ST1 trials), so the third clause: one model with a traced mechanism. The trace runs name → silent acceptance → `impossible` → a wrong diagnosis, and the in-run contrast shows the same model correcting the same slip at once wherever a tool's error text named it. That is design/10's "a wrong argument the schema or error text could have prevented". It is not F2's shape (an instruction not followed): here the argument was taken without a word.
  - *New:* no earlier record has a provider name as a `providerId` value of a QoS expression. M-8 (the empirical findings) and PPAM A5 concern operand keys that do not exist (`cloud=EU`), not this.
  - *Context:* the reworded `expression` text is the one this case is about (ST1, above). The federation's provider names, `local` and `owner`, read like keywords and may invite the slip; a site-like name (`providerId=vistula-dc`) is the same mistake.
- **SF2 (`get_file_qos_summary`): `"status": "fulfilled"` with no requirements read as "fully stored here".** *Fixed (this PR); smoke re-run pending.* Qwen3-Coder V1 #2 ([0]–[5] each → `{"status": "fulfilled", "requirements": {}}`; "The QoS summary for each file shows a status of "fulfilled" with no requirements, indicating that each file is completely replicated locally"), V1 #0 ([7]–[12], with SF3), SR1 #0 ([5]: "the data should already be fully present at the local site"), SR1i #2 ([4], with F2). All four FAIL. The V1 answers were false (four of the six files had 0 local bytes); the SR1 and SR1i claims were true when made but unread.
  - *The evidence bar:* one family, so the third clause, applied as for SF1. The trace is complete in each trial: the call, the vacuous `fulfilled` it returned, and an answer that gives that field as its reason. The description lists the status values but says neither what `fulfilled` means with no requirement nor that the status says nothing of where the data are. SF1's in-run contrast is corroboration, not the clause, and SF2 has none: no trial shows another wording preventing it.
  - *Known by the letter:* design/13, "Results", V1, Qwen3-Coder #5 and #7, logged there (as L3b) for this re-run. See "known" above.
- **SF3 (`get_file_distribution`): the local provider's empty entry read as holding the file.** *Fixed (this PR); smoke re-run pending.* Qwen3-Coder V1 #0 ([6]: the local entry has `"success": true`, `virtualSize` 262144, `physicalSize` 0 and no blocks; the answer: "each file has a successful distribution at the local provider", "the correct virtual size matching its physical size at the local provider") and SR1n #0 ([2]: "the file is already present at the "local" provider with a full physical size of 262144 bytes", which then shaped its copy to `owner`). Both FAIL (V1 #0 also for SF2; SR1n #0 for its write).
  - *The evidence bar:* the third clause, as SF2: in V1 #0 the answer reasons from the empty entry's `success` and `virtualSize`. SR1n #0 gives the owner's size to the local site, a plainer misread of the same entry.
  - *Known by the letter:* design/12, "Results", SR1o, Qwen3-Coder #0–#2, #4–#7, logged there (as L3b) for this re-run.
- **F2, completion claimed off the transfer's status (logged against `get_transfer`).** Qwen3-Coder SR1i #1 ("I've checked the transfer status and can confirm … Bytes replicated: 12,425,587 (which matches the file size)"; the file is 134,217,728 bytes) and SR1i #2 ("the replication transfer is complete"). Known: design/12, "Results", F2 (Qwen3-Coder SR1i #1, #3, #5, #6).

**Model capability (L3b)**

- Qwen3-Coder SR1 #1, #2 and SR1i #0: one status read, then stopped ("Since I cannot wait or poll multiple times in this environment", SR1 #1; SR1i #0 at "replicating, about 50%"). The data were in fact already whole at each answer. As design/12's SR1 Qwen3-Coder #2 and #4.
- Qwen3-Coder SR1n #0, #1: a copy to `owner` "instead", after the providers listing ("the closest we can get to fulfilling your request since "oder-dc" is not a recognized provider", #0). As design/12's SR1n.
- Qwen3-Coder V3o #0: found the rule's provider id, did not match it to the site ("we cannot confirm if it's the local site without knowing the local provider ID"). As design/11's Qwen3-Coder V3o #1.
- Qwen3-Coder ST4 #2: one listing with the default state, `ongoing` ([0] → no transfers), "There are no transfers found for the space", and the turn ended on a tool call begun as text (`<tool_call>`). In all three of its ST4 trials the first listing used the default; in #0 and #1 it listed `ended` next and answered rightly. Logged as an observation for `list_space_transfers`: its empty answer does not say that only ongoing transfers were listed.

**Harness (L1)** S1–S6, above. **Deployment artefacts (L2):** the control's 6 ADAPTER_ERROR records, re-run.

### What the protocol asks next

- **`add_file_qos_requirement`.** The fix the trace points to is in the tool: refuse a `providerId=` operand whose value is the name of a provider of the file's space, as `schedule_file_replication` does ("Pass its id …, not its name"). That is a change to its error hints, so ST1 re-runs on the smoke pair at K=3. The operator ruled on 2026-11-23 (design/10, "Staying stable") that a tool with a new tool-attributable finding is fixed and re-run, and demoted only if the fix fails or needs a name, schema or annotation change; the fix needs none.
- **`get_file_qos_summary` and `get_file_distribution`.** Whether the deferring records could be the baseline no longer decides anything: SF2 and SF3 are treated as new and fixed, each tool followed by its scenarios on the smoke pair at K=3. For SF2 the summary says that a status with no requirement is vacuous and says nothing of where the data are; for SF3 each provider entry says whether it holds the whole file.
- **`get_qos_requirement`'s hints:** not verified; the Transition item stays open.
- **The other four rows** (`get_file_qos_summary`'s `replicas_num`, `get_transfer`, `list_space_providers`, `list_space_transfers`): cleared.

## Fixes for SF1 to SF3 and their re-run

The three fixes change results, errors and hints only (design/10, "Aim"; findings M-36 to M-38 in `research/empirical-mcp-server-findings.md`). No tool's name, input schema, annotations or description changed: the `tools/list` hash of each of the three tools, of K1 (`9192d3f5…`) and of ST7_TOOLS is the one before the change.

| Finding | Tool | What a model now gets |
|---|---|---|
| SF1 | `add_file_qos_requirement` | Before the rule is created, each `providerId=<value>` operand is checked against the providers of the file's space (one `GET /spaces/{id}`; the space comes from the file id). A value that is a provider's name is refused: "'local' is a provider's name; providerId= takes its id: providerId=<id>, here providerId=<that id>", with the space's providers listed. A value that is neither a name nor an id of one is refused with the providers listed. Ids pass unchanged and no other operand is looked at. With no `providerId=` operand there is no extra request, and if the space or its providers cannot be read the rule is created as before. |
| SF2 | `get_file_qos_summary` | When `requirements` is an empty mapping, a `meaning` field: "No QoS requirement applies to this file (inherited ones included); 'fulfilled' here says nothing about where its data are." (the status as returned). With requirements, nothing is added. |
| SF3 | `get_file_distribution` | On each readable provider entry of a regular file, `holds_whole_file`: true or false, from that provider's blocks against the file's size (the largest size any provider reports, or a block's end if larger). `null` with a `holds_whole_file_note` when no provider reports a size above 0, or the provider's blocks could not be read in full. The existing fields stay; a directory's entries and failed entries are left as they are. |

Unit tests: `test/unit/api/test_qos_provider_operands.py` (a name, an unknown value, a valid id, a compound expression, a failed or empty listing, an id that does not decode, no operand), `test/unit/modules/test_qos_wrapper.py` (the refusal through the tool; an empty and a non-empty summary), `test/unit/modules/test_files_wrapper.py` (whole, partial, empty, zero or unknown size, unreadable blocks).

### The re-run

Each of the three tools owes its scenarios on the smoke pair at K=3 (design/10, "Staying stable"): ST1 for `add_file_qos_requirement`; V1, V3, V3n, V3o, ST3 and ST5 for the summary; V1, SR1, SR1i, SR1n and ST6 for the distribution. The union is 11 scenarios, 2 × 11 × 3 = 66 trials. SR1n is not in the map's row for `get_file_distribution` (V1, SR1, SR1i, ST6); it is added because SF3 was seen there (SR1n #0). The map is unchanged.

    make valfed-up && make valfed-reset && make valfed-check   # check $VALFED_HOME/HOLD first: the host is shared
    SSL_CERT_FILE=$VALFED_HOME/tls/ca-bundle.pem \
      make k2val K2VAL_CONFIG=benchmark/k2val/k2val.valfed.toml \
        K2VAL_ARTEFACT_ROOT=$VALFED_HOME/evidence/k2val \
        K2VAL_MODELS=qwencoder,haiku-low K2VAL_K=3 K2VAL_RID=smoke-stable-2 \
        K2VAL_SCENARIOS=V1,V3,V3n,V3o,SR1,SR1i,SR1n,ST1,ST3,ST5,ST6

`K2VAL_ARTEFACT_ROOT` is the Makefile's name for `--artefact-root`. The pass rule is the one above: a clean re-run keeps the three tools stable.

- **SF1's refusal is NOT VERIFIED** until a re-run trial draws it, that is, a model sends a provider's name (or another value that is not an id) in `providerId=` and is shown the refusal. With no such trial the hint stays an open item: unit tests do not clear it (design/10). SF2's `meaning` and SF3's `holds_whole_file` are in front of a model on every call that returns an empty summary or a regular file's distribution. The report counts exposures only of `get_qos_requirement`'s hint, so for these three they are read off the recorded calls. *Run 2:* the name refusal was drawn in two trials; the refusal of a value that is neither, and SF3's `null`, in none ([Exposure to the fixes](#exposure-to-the-fixes)).
- **The `get_qos_requirement` hint row stays NOT VERIFIED (open).** Nothing here changes it: the run still needs a trial that sends that tool a malformed id. *Run 2:* still no exposure.

## Results of the second smoke run (2026-11-23)

Run `smoke-stable-2` on the validation federation, 2026-11-23, 14:59 to 15:56 UTC, at master `e2697c2` (PR #48, the three fixes), in the durable evidence store. The smoke pair at K=3 on the 11 scenarios above: 66 records, one per trial. No trial had an infrastructure outcome, so every fixture precondition held. The offered surface of every trial is K1, `9192d3f502ebbd04a4d17ef787c27f7193df0fb89bac5eafa1ea618ae1324df3`. Answers were read by the deterministic reader and the judge (Claude Sonnet 5.5, effort medium). The classes and the L3a bar are those of the first run; this run has two families, Qwen (Qwen3-Coder) and Anthropic (Haiku).

**Verdict: the re-run is clean.** No new tool-attributable failure in `add_file_qos_requirement`, `get_file_qos_summary` or `get_file_distribution`. Each fix was shown to a model, and no model shown it misread it. All three tools stay stable.

### Rescore

The stored answers were read again offline (`rejudge --cached-only`: the stored verdicts, no judge call), into a new directory beside the run. The run was left unchanged (sha256 of every file checked before and after). Master's readers are the runner's: no file under `benchmark/` changed between `e2697c2` and master `d3ed709`. A rescore at master reproduces every stored outcome.

| Run | As stored (`e2697c2`) = master | with S8 and S9 | with S10 too | after review by hand |
|---|---|---|---|---|
| pair (66) | 53 PASS, 4 FAIL, 9 NEEDS_REVIEW | 56 PASS, 5 FAIL, 5 NEEDS_REVIEW | 56 PASS, 5 FAIL, 5 NEEDS_REVIEW (no change) | 58 PASS, 8 FAIL |

The fixes change 5 readings, all in this run. Every scored answer in the store was read with master's code and with the branch's: this run, the first smoke run and its control, the gate-4 runs of design/12–15, and a later SR1 and SR1i run of two configs, still being written (996 answers with S8 and S9, 1004 with S10). No stored PASS changed, and S10 changes no reading that S8 and S9 left.

### L1: reader fixes (paired regression tests in `test/unit/k2val/test_smoke_readers.py`, "the second run")

| # | Reader | Misread | Fix | Trials |
|---|---|---|---|---|
| S8 | ST1 `read_rule_task`: `_RULE_STATUS`, `_STATUS_EXPLAINED`, `_STATUS_COMING`, `_st1_clauses` | Each of these, beside "the data are now fully at your site", was read as undoing the claim. Three more ways S1 did not cover of explaining a pending status: (1) "The QoS summary still lists the requirement as `pending`, so the rule's own status may update shortly"; (2) "… still reports the requirement as `pending`, so the rule's status may not have updated yet"; (3) "the QoS summary still says `pending` on two checks. I expect it to flip to `fulfilled` shortly, since the data are already there", where the explanation is in the next sentence | "lists" is a status verb, and "may not have updated (yet)" is a lag. A status is also explained by the next sentence when that sentence says the status is about to change. A clause that says the status is about to change is not itself a done claim. "Will not change" and "may not update" explain nothing; this guard also closes the same opening in S1's same-sentence cue | Haiku #0, #1, #2 → PASS |
| S9 | ST1 `read_rule_task`: `_WHEN_TO_COME` | A done word in a clause about a time still to come was read as a claim. "To check when the data are fully replicated … I'll need to monitor the QoS status" was read as SAYS_DONE, a false PASS (the danger direction). "… to verify when it reaches "fulfilled" status" cancelled a "hasn't completed yet" | After "when", "until" or "once" with "it" or "the …" and a present-tense verb, a done word is no claim. A past "when I checked, the data were fully there" is still one | Qwen3-Coder #0 → FAIL, as the judge read it; Qwen3-Coder #2 → OTHER, so NEEDS_REVIEW, FAIL either way |

Paired negatives: the newly matched status phrasing with no data claim stays SAYS_NOT_DONE, explained or not. Beside a bare "done" or "the rule is fulfilled", a third sentence that does not say the status will change leaves the answer saying both. "Once the rule is fulfilled, the data are fully there" is no claim.

**S10, a reading rule (the operator, 2026-11-23): a rule's status never undoes a claim that the data are fully there.** This is the second run in which Haiku's answer "the data are fully at your site; the rule still says pending" needed a new cue (S1, then S8). The cues are no longer needed for it:
- The oracle judges every "done" against the local blocks at the answer: a claim made before they cover the file fails as a PREMATURE CLAIM. And 25.0 reports a rule `pending` for seconds after its data are whole (all three Haiku ST1 trials here).
- So where a done clause says the data are there (`_DATA_DONE`: "fully at", "holds the whole file", "all of its bytes", with the data or the site named), a pending rule status counts as no "not done", explained or not. The rest of its clause is still read: "the rule is pending and the data are not yet fully there" still says not done.
- Where no clause says so, nothing changes: beside a bare "done" or "the rule is fulfilled", a status not explained away still makes the answer say both, and a status alone is still "not done". "The rule is now in place at your site" is about the rule, not the data.
- The pinned case "The data are fully at vistula-dc, but the rule is still pending" (`test_stable_scenarios.py`, and its two forms in `test_smoke_readers.py`) is now SAYS_DONE. Its paired negatives are "Done, but the rule is still pending" and the other bare-"done" forms, which stay OTHER.
- The judge's rubric is unchanged; it already read these answers as done, so no stored verdict is invalidated.
- S8's cues stay: they still decide a "done" with no data claim.

**Not fixed, resolved by hand.** Two disagreements are left to the hand review, and the readers are not changed for them:
- The SR reader is shared with design/12's record. On SR1 #1 it reads a step of a narrative ("is currently in progress") against a later claim of whole, as in the first run's SR1i #1. On SR1n #2 it reads "not found among the space's providers" as a refusal.
- The judge wrote ST6 #0's owner share as "Owner Site=50", the site as the answer writes it. The comparison is exact, so a right answer goes to NEEDS_REVIEW, never to a wrong verdict ("The smoke run", above).

### NEEDS_REVIEW resolved by hand

- **Qwen3-Coder SR1 #1** (reader OTHER, judge SAYS_WHOLE): SAYS_WHOLE. "I've checked the file distribution and can confirm that the data are now fully replicated at your local site", after [4] the distribution: the local entry `holds_whole_file: true`, blocks [0, 134217728]. "The transfer was initiated and is currently in progress" is step 2 of its account. **PASS:** read from the blocks, whole at the answer.
- **Qwen3-Coder SR1i #1** (reader OTHER, judge SAYS_NOT_YET): SAYS_NOT_YET ("the data haven't been fully copied yet"). FAIL either way: the copy went to the owner and the required call is missing (below).
- **Qwen3-Coder SR1n #2** (reader SAYS_NOT_POSSIBLE, judge CLAIMS_A_COPY): CLAIMS_A_COPY ("the replication to the "local" provider has already completed successfully"). FAIL either way: it scheduled a copy ([1]).
- **Qwen3-Coder ST1 #2** (reader OTHER after S9, judge SAYS_NOT_DONE): not done ("Would you like me to check the current status of this requirement …?"). FAIL either way.
- **Qwen3-Coder ST6 #0** (reader {local=25, owner=50}, judge {local=25, Owner Site=50}): "Owner Site holds 50% of the data" is the owner's share. **PASS.**

### PASS of 3, after classification

| Config | V1 | V3 | V3n | V3o | SR1 | SR1i | SR1n | ST1 | ST3 | ST5 | ST6 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| haiku-low | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 | 3 |
| qwencoder | 3 | 3 | 3 | 2 | 3 | 0 | 1 | 1 | 3 | 3 | 3 |

58 of 66 pass. Pass rates are reported; they do not gate. Qwen3-Coder's V1 and SR1, where the first run's SF2 and SF3 trials were, pass 3 of 3 here (1 and 0 in the first run).

### Exposure to the fixes

Read off the recorded tool results of every trial. "Misreading possible" counts the trials in which the first run's misreading could have recurred: for SF2, an empty summary in a task that asks where the data are; for SF3, a provider entry that does not hold the whole file.

| Fix | Shown | Misreading possible | Acted rightly | Not shown (NOT VERIFIED) |
|---|---|---|---|---|
| SF1: refusal of a name in `providerId=` | 2 trials, 2 calls: Qwen3-Coder ST1 #1 [0] and #2 [0], `providerId=local` → `No QoS requirement was added. 'local' is a provider's name; providerId= takes its id: providerId=<id>, here providerId=b767…` | 2 | 2 of 2: the next `add_file_qos_requirement` call (#1 [2], after listing the providers; #2 [1]) sent the local provider's id, and the rule was created | the refusal of a value that is neither a provider's id nor its name |
| SF2: `meaning` on an empty summary | 13 trials, 23 calls: every successful summary with empty `requirements` carried it | 6: Qwen3-Coder V1 #0–#2, SR1 #2, SR1i #2, ST1 #0. The other 7 (V3 and V3n, both configs) ask to remove a rule | Not misread in 13 of 13: no answer took the empty summary's `fulfilled` as a statement of where the data are (SR1i #2 and ST1 #0 failed for other reasons, below). In V1 #0, Qwen3-Coder read the six summaries ([1]–[6]), went on to the attributes ([7]–[12]) and named the two local files; the first run's V1 #2 stopped at the summaries. SR1 #2: "The QoS summary shows "No QoS requirement applies" which means there are no specific replication rules, but the file is still accessible and fully present", the last from the blocks | none |
| SF3: `holds_whole_file` per provider | 31 trials, 62 calls; every value true or false | 19. In 10, an empty local entry (no blocks): Haiku SR1 #1, V1 #0–#2; Qwen3-Coder SR1 #0, SR1i #0, SR1n #0, ST1 #0, V1 #1, #2. In 10, a partial one: a copy in progress (Haiku SR1 #1, #2, SR1i #0, #2) or ST6's split file (all six ST6 trials) | Not misread in 19 of 19: no answer read an entry that is not whole as whole. Qwen3-Coder V1 #1 and #2 list each file's `holds_whole_file` and answer from it. SR1n #0, the same scenario and index as the first run's SF3 trial, now refuses rightly (PASS) | `null` with `holds_whole_file_note` |

An observation on SF3, not a failure: twice Qwen3-Coder read an empty local entry (`holds_whole_file: false`, no blocks) as part of a copy. It wrote "It's also partially stored at the "local" provider" (SR1n #0) and "while there is a replica at your local provider, it doesn't yet hold the complete file" (ST1 #0). Neither claimed the whole file, and neither trial failed for it. The field says "not whole", not "none".

### Classification of every non-pass

**Harness (L1):** S8 and S9, above. **Deployment artefacts (L2):** none.

**Model capability (L3b)**

- **Qwen3-Coder ST1 #0.** It set the rule by id ([4], 5.4 s into the trial). The summary ([5], 7.0 s) and `get_qos_requirement` ([7], 9.4 s) read `pending`, and it answered: "the requirement is currently in "pending" status, meaning the replication process has started but hasn't completed yet … To confirm when the data are fully replicated, you would need to monitor the file's status." The snapshot taken at the answer, 11.7 s in, found the local copy whole and the rule fulfilled. It read the status twice, 2.4 s apart, without reading the blocks after the rule, then stopped and handed the watching back, as the first run's SR1 #1.
  - *Not L3a against `get_file_qos_summary`:* the answer reads `pending` for what it says, the rule not yet met, and claims nothing the status does not say. Haiku saw `pending` beside whole data in all three of its ST1 trials and answered from the data (the blocks, or `isFullyReplicatedLocally`).
  - *Decided (operator, 2026-11-23): L3b.* It stopped without waiting, and the summary was accurate. This was the one trial on which `get_file_qos_summary`'s verdict turned.
  - It saw SF2's `meaning` ([1], before the rule) and SF3's field ([2]) and read both rightly.
- **Qwen3-Coder ST1 #2.** SF1's refusal, then the rule by id ([1]), then no read at all; it ends by asking whether to check. FAIL.
- **Qwen3-Coder SR1n #1 and #2.** Each copied to `local` "instead" of `oder-dc` after the providers listing, as design/12's SR1n. In #1 it also judged the copy from the transfer's status, past `schedule_file_replication`'s note: "According to the documentation, I should verify this by checking the target provider's blocks of the file, but since the transfer status shows "completed" and all bytes were replicated, the data are fully there."
- **Qwen3-Coder V3o #0.** "I cannot confirm whether the provider ID `b767…` corresponds to the local site without additional information." As the first run's V3o #0 and design/11's Qwen3-Coder V3o #1.

**Known: F2 (logged against `get_transfer`)**

- **Qwen3-Coder SR1i #2.** It scheduled by id ([1]; the result carries the note "Scheduled, not yet copied: judge completion by the target provider's blocks of the file, not by the transfer's status"). Then it read `get_transfer` ([2], [4] "completed") and answered from it: "the `bytesReplicated` shows that 134,217,728 bytes have been replicated, indicating that the data are fully there". FAIL (`unread_claim`).
  - Known: design/12, "Results", F2.
  - The note is F2's fix (PR #46), and it was in front of the model here. SR1n #1, above, is the same claim.
  - The summary's `meaning` ([3]) played no part in the claim.

**Outside the map: `schedule_file_replication` (experimental, not a row here)**

- **Qwen3-Coder SR1i #0 and #1: after the name refusal, a copy to the owner.**
  - The calls:
    - [0] `target_provider_id "local"` → "Pass its id 'b767…', not its name: 'local' is the name of a provider that supports the space …", then the providers, then 25.0's own error: "Specified space: … is not supported by provider "local"."
    - [1] the providers listed.
    - [2] a schedule to the owner's id.
  - #0's answer gives 25.0's text as its reason: "I was unable to do so due to the error indicating that the "local" provider doesn't support the space … even though it's listed in the provider list". #1 says nothing about why.
  - Both FAIL: the required call is missing, and the site holds none of the file.
- **Why it is new.** In the gate-4 runs, each of the ten such refusals was followed by the site's id (design/12; the target read from its records). In the first smoke run, one of three (SR1i #0) went to the owner first and then to the site; here two of three did, and stopped there. M-21 (the findings log) fixed this contradiction in the hint's own opening sentence (PR #35) and predicted this failure: "a literal model concludes the site cannot hold the file". The contradiction is still carried by 25.0's message, which the hint keeps after itself (the findings log's design rule 3).
- **The bar:** one family, with a traced mechanism in #0: the refusal text, the wrong target, and that text quoted as the reason. That is L3a by the third clause, against `schedule_file_replication`.
- **The contrast in this run:** the same model sent the same name to `add_file_qos_requirement` twice (ST1 #1, #2). That tool's refusal is built before any request and carries no 25.0 message, and both times the model used the id at once.
- **Effect on this record:** it changes no verdict here. It is fixed in a separate PR, with its own entry in the findings log; SR1 and SR1i are re-run at K=8 after it (design/12's gate 4).

### Per tool

| Tool | Changes re-run | Direct (scored trials) | Supporting (target-matched call) | New tool-attributable | Fix shown, acted rightly | Verdict |
|---|---|---|---|---|---|---|
| `add_file_qos_requirement` | SF1, description | ST1: 6 | none | none | 2 trials, 2 of 2 | **STAYS STABLE** |
| `get_file_qos_summary` | SF2, description | ST3, ST5: 12 | 18 of 18 (V3, V3n, V3o) | none (Qwen3-Coder ST1 #0 is L3b, decided) | 13 trials (6 where the misreading was possible), not misread in 13 | **STAYS STABLE** |
| `get_file_distribution` | SF3, description | ST6: 6 | 14 of 18 (V1, SR1, SR1i) | none | 31 trials (19 where the misreading was possible), not misread in 19 | **STAYS STABLE** |

Two branches of the fixes were shown to no model and stay **NOT VERIFIED**, as open items (design/10): `add_file_qos_requirement`'s refusal of a `providerId=` value that is neither a provider's id nor its name, and `get_file_distribution`'s `holds_whole_file: null` with its note. They change no verdict above: each tool's fix was exposed and worked.

The other rows this run touches:
- **`get_file_qos_summary`, result `replicas_num`:** the shape check was applied in 12 trials (ST3, ST5) and failed in none. Verified again.
- **`get_qos_requirement`, error hints:** 3 calls (Qwen3-Coder ST1 #0, V3o #0, #2), all with a real rule id, all successful. No exposure: still **NOT VERIFIED** (open).
- **`list_space_providers`:** supporting, 21 of 24 trials (SR1, SR1i, SR1n, ST1) with a target-matched call. No finding.
- **`get_transfer`:** no direct scenario ran. F2 recurs (SR1i #2), known.
- **`list_space_transfers`:** no direct scenario ran. Qwen3-Coder listed transfers in ST1 #0 and #1. No finding.

### What the protocol asks next

- **The three tools stay stable** (design/10, "Staying stable": the fix was made and the re-run is clean). SF1 to SF3 are closed.
- **Open, not verified:** SF1's "neither" refusal, SF3's `null` branch, and `get_qos_requirement`'s hint. Each stays open until a trial shows it; none needs a run of its own.
- **For design/12 (`schedule_file_replication`, experimental):**
  - The owner-id slip above: fixed in a separate PR; SR1 and SR1i are re-run at K=8 after it.
  - F2 recurred in front of its note (SR1i #2; the same claim in SR1n #1), for that re-run to watch.

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

**Answered by the run of 2026-11-23.** `make valfed-check` built ST3's to ST6's fixtures, and no smoke trial was a RESET_FAIL, so the preconditions behind 1–6 held: the ended transfers were listed newest first (1), the transfer bodies named the file and the site (2), the rules read back and evaluated as intended (3, 4), the `split` file left the quarter between its parts held by neither provider (5: `owner` [0, 131072], `local` [196608, 65536]), and the spaces resolved by name (6). For 7, 25.0 reports such a rule as `impossible` (Qwen3-Coder's `providerId=local` in ST1 #0 and #1), and the file never arrives.
