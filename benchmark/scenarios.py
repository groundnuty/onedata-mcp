"""18-task PPAM 2026 benchmark scenario set.

Authored 2026-05-01. Anchored on the live `ppam_2026_mcp_tests` space
supported by cloud-pl + Cloud-SK (10 GiB each, both EU). See
`papers/ppam-2026/research/27-benchmark-space-snapshot.md` for federation
state, and `design/04-scenario-authoring-decisions.md` for the
non-obvious authoring choices (e.g. A4 reframed from cross-space to
cross-directory; P2 uses country=DE as an intentionally-unfulfillable
expression on our PL+SK pair).

Tier distribution per paper Table 3: 8 format / 8 static / 2 dynamic.

Naming convention for fixture files: '/ppam_2026_mcp_tests/<scenario_id>/...'
keeps scenarios isolated from each other so the federation reset between
trials only has to touch one subtree per scenario. Scenarios that need
multiple files share a per-scenario subdir.
"""

from __future__ import annotations

from benchmark._scenario_types import (
    FileFixture,
    Fixture,
    Scenario,
    TransferFixtureHint,
)
from benchmark.tool_allowlist import HEADLINE

SPACE = "ppam_2026_mcp_tests"


def _path(scenario_id: str, *parts: str) -> str:
    return f"/{SPACE}/{scenario_id.lower()}/" + "/".join(parts)


# ---------------------------------------------------------------------------
# Discovery band (D1..D6) — read-only metadata queries, format oracles.
# ---------------------------------------------------------------------------

D1 = Scenario(
    id="D1",
    band="discovery",
    brief=(
        "List every space available to you. For each space, report how many "
        "providers support it. Format your answer as a markdown table with "
        "columns 'space' and 'provider_count'."
    ),
    oracle_tier="format",
    required_tools=frozenset({"list_user_spaces", "list_space_providers"}),
    allowed_tools_minimal=frozenset({"list_user_spaces", "list_space_providers"}),
    oracle_check=(
        "Parse the agent's table; assert each space the user has access to "
        "appears with provider_count == |providers list returned by REST "
        "side-channel for that space|."
    ),
)

D2 = Scenario(
    id="D2",
    band="discovery",
    brief=(
        f"Find every file in space `{SPACE}` whose path begins with "
        f"`/{SPACE}/d2/datasets/`. Report the full paths as a bullet list, "
        "one per line."
    ),
    oracle_tier="format",
    required_tools=frozenset({"list_files_recursively"}),
    allowed_tools_minimal=frozenset({"list_user_spaces", "list_files_recursively"}),
    fixture=Fixture(
        files=(
            FileFixture(_path("D2", "datasets", "alpha.txt"), content="a"),
            FileFixture(_path("D2", "datasets", "beta.txt"), content="b"),
            FileFixture(_path("D2", "datasets", "gamma.csv"), content="g"),
            # Distractors outside the prefix:
            FileFixture(_path("D2", "outside_prefix.txt"), content="x"),
            FileFixture(_path("D2", "datasets_lookalike", "no.txt"), content="y"),
        )
    ),
    oracle_check=(
        "Parse paths from the answer; set-equality with "
        "{'/.../d2/datasets/alpha.txt', '/.../d2/datasets/beta.txt', "
        "'/.../d2/datasets/gamma.csv'}."
    ),
)

D3 = Scenario(
    id="D3",
    band="discovery",
    brief=(
        f"Read the file at `/{SPACE}/d3/manifest.txt` and report (a) its "
        "size in bytes and (b) the first 50 characters of its content. "
        "Format the answer as: 'size=<N>; head=<chars>'."
    ),
    oracle_tier="format",
    required_tools=frozenset({"download_file"}),
    allowed_tools_minimal=frozenset(
        {"list_user_spaces", "list_files_recursively", "download_file"}
    ),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("D3", "manifest.txt"),
                content="MANIFEST v1\nbuild=46-g14b5bda7\nspace=ppam_2026_mcp_tests\n",
            ),
        )
    ),
    oracle_check=(
        "Parse 'size=N' and 'head=...'. Assert size == byte length of "
        "fixture content and head == fixture content[:50]."
    ),
)

D4 = Scenario(
    id="D4",
    band="discovery",
    brief=(
        f"List every provider supporting space `{SPACE}`. For each provider, "
        "report (providerId, providerName) as a markdown table."
    ),
    oracle_tier="format",
    required_tools=frozenset({"list_space_providers"}),
    allowed_tools_minimal=frozenset({"list_user_spaces", "list_space_providers"}),
    oracle_check=("Set-equality on providerName values: {'cloud-pl', 'Cloud-SK'}."),
)

D5 = Scenario(
    id="D5",
    band="discovery",
    brief=(
        f"Get the JSON metadata attached to `/{SPACE}/d5/sample.txt`. "
        "Report each key=value pair on its own line in 'key: value' form."
    ),
    oracle_tier="format",
    required_tools=frozenset({"get_file_metadata"}),
    allowed_tools_minimal=frozenset({"list_user_spaces", "get_file_metadata"}),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("D5", "sample.txt"),
                content="payload",
                json_metadata={
                    "pipeline_stage": "raw",
                    "owner": "agent",
                    "created": "2026-05-01",
                },
            ),
        )
    ),
    oracle_check=(
        "Parse 'k: v' lines. Set-equality of (k, v) pairs against the fixture's json_metadata."
    ),
)

D6 = Scenario(
    id="D6",
    band="discovery",
    brief="List the names of all spaces available to you, one per line.",
    oracle_tier="format",
    required_tools=frozenset({"list_user_spaces"}),
    allowed_tools_minimal=frozenset({"list_user_spaces"}),
    oracle_check=(f"Parse space names from the answer; assert '{SPACE}' appears in the set."),
)


# ---------------------------------------------------------------------------
# Multi-step access band (A1..A6).
# Tiers per Table 3: A1 + A6 format; A2..A5 static.
# ---------------------------------------------------------------------------

A1 = Scenario(
    id="A1",
    band="access",
    brief=(
        f"For every file under `/{SPACE}/a1/raw/`, set custom JSON metadata "
        '`{"reviewed_by": "agent", "reviewed_at": "2026-05-01"}`. '
        "After tagging, report the count of files you tagged in the form "
        "'tagged=<N>'."
    ),
    oracle_tier="format",
    required_tools=frozenset({"list_files_recursively", "set_file_metadata"}),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "list_files_recursively",
            "set_file_metadata",
            "get_file_metadata",
        }
    ),
    fixture=Fixture(
        files=tuple(
            FileFixture(_path("A1", "raw", f"sample{i:02d}.txt"), content=f"row{i}")
            for i in range(5)
        )
        + (FileFixture(_path("A1", "outside.txt"), content="distractor"),)
    ),
    oracle_check=(
        "Parse 'tagged=N'; assert N == 5 (number of fixture files under "
        "/a1/raw/, excluding the outside.txt distractor)."
    ),
)

A2 = Scenario(
    id="A2",
    band="access",
    brief=(
        f'Set custom JSON metadata `{{"reviewed": false}}` on every file '
        f"under `/{SPACE}/a2/inbox/`. When done, report 'done'."
    ),
    oracle_tier="static",
    required_tools=frozenset({"list_files_recursively", "set_file_metadata"}),
    allowed_tools_minimal=frozenset(
        {"list_user_spaces", "list_files_recursively", "set_file_metadata"}
    ),
    fixture=Fixture(
        files=tuple(
            FileFixture(_path("A2", "inbox", f"msg{i:02d}.txt"), content=f"m{i}") for i in range(4)
        )
    ),
    oracle_check=(
        "REST side-channel: for each fixture file under /a2/inbox/, "
        "get_file_metadata(json) must contain reviewed == False. Strict "
        "equality on the boolean."
    ),
)

A3 = Scenario(
    id="A3",
    band="access",
    brief=(
        f"Rename the file `/{SPACE}/a3/staging/draft.txt` to "
        f"`/{SPACE}/a3/staging/published.txt`, then set its custom JSON "
        'metadata to `{"status": "published"}`. Report \'done\' when finished.'
    ),
    oracle_tier="static",
    required_tools=frozenset({"move_file", "set_file_metadata"}),
    allowed_tools_minimal=frozenset(
        {"list_user_spaces", "move_file", "set_file_metadata", "get_file_id"}
    ),
    fixture=Fixture(files=(FileFixture(_path("A3", "staging", "draft.txt"), content="draft v1"),)),
    oracle_check=(
        "REST side-channel: (a) /a3/staging/draft.txt does NOT exist "
        "(get_file_id raises enoent); (b) /a3/staging/published.txt exists "
        "with json metadata == {'status': 'published'}."
    ),
)

A4 = Scenario(
    id="A4",
    band="access",
    brief=(
        f"Copy the file `/{SPACE}/a4/source/data.csv` (preserving its custom "
        f"JSON metadata) to `/{SPACE}/a4/archive/data.csv`. The original "
        "file must remain in place. Report 'done' when finished."
    ),
    oracle_tier="static",
    required_tools=frozenset(
        {
            "download_file",
            "get_file_metadata",
            "create_file",
            "set_file_metadata",
        }
    ),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "download_file",
            "get_file_metadata",
            "create_file",
            "set_file_metadata",
        }
    ),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("A4", "source", "data.csv"),
                content="id,value\n1,42\n2,13\n",
                json_metadata={"format": "csv", "rows": "2"},
            ),
        ),
        notes=(
            "Reframed from paper's 'cross-space copy' to 'cross-directory "
            "copy' since we operate within a single benchmark space; "
            "see design/04 for rationale."
        ),
    ),
    oracle_check=(
        "REST side-channel: (a) source still exists with original content + "
        "metadata; (b) archive copy exists with byte-identical content and "
        "json metadata == {'format': 'csv', 'rows': '2'}."
    ),
)

A5 = Scenario(
    id="A5",
    band="access",
    brief=(
        f"Create a new file at `/{SPACE}/a5/important/checkpoint.bin` with "
        "content `placeholder` (utf-8). Then attach a QoS rule requiring it "
        "be replicated on at least 2 EU providers. Report 'done' when "
        "finished."
    ),
    oracle_tier="static",
    required_tools=frozenset({"create_file", "add_file_qos_requirement"}),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "create_file",
            "add_file_qos_requirement",
            "get_file_qos_summary",
        }
    ),
    fixture=Fixture(),  # no pre-state — agent creates the file
    oracle_check=(
        "REST side-channel: file exists with size==len('placeholder'); "
        "get_file_qos_summary returns at least one requirement whose "
        "expression matches an EU constraint (country=PL|country=SK|"
        "country=AT|geo=EU) and replicas_num >= 2."
    ),
)

A6 = Scenario(
    id="A6",
    band="access",
    brief=(
        f"Find every file in space `{SPACE}` whose JSON metadata key "
        "`pipeline_stage` equals `raw`. Report the full paths as a bullet "
        "list, one per line."
    ),
    oracle_tier="format",
    required_tools=frozenset({"query_by_metadata"}),
    allowed_tools_minimal=frozenset({"list_user_spaces", "query_by_metadata"}),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("A6", "batch01", "f1.txt"),
                content="x",
                json_metadata={"pipeline_stage": "raw"},
            ),
            FileFixture(
                _path("A6", "batch01", "f2.txt"),
                content="x",
                json_metadata={"pipeline_stage": "raw"},
            ),
            FileFixture(
                _path("A6", "batch02", "f3.txt"),
                content="x",
                json_metadata={"pipeline_stage": "anonymised"},
            ),
            FileFixture(
                _path("A6", "no_meta.txt"),
                content="x",
            ),
        )
    ),
    oracle_check=(
        "Parse paths from answer; set-equality with the two /a6/batch01/ "
        "files. /a6/batch02/f3.txt is a distractor (different stage); "
        "/a6/no_meta.txt is a distractor (no metadata)."
    ),
)


# ---------------------------------------------------------------------------
# Placement-introspection band (P1..P6).
# Tiers per Table 3: 4 static, 2 dynamic (P3, P4).
# ---------------------------------------------------------------------------

P1 = Scenario(
    id="P1",
    band="placement",
    brief=(
        f"Report the per-provider distribution of `/{SPACE}/p1/model_v1.pt`. "
        "For each supporting provider list (providerName, percentage of "
        "blocks held). Identify whether the file is fully replicated to any "
        "EU provider and name those providers."
    ),
    oracle_tier="static",
    required_tools=frozenset({"get_file_distribution", "list_space_providers"}),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "get_file_distribution",
            "list_space_providers",
        }
    ),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("P1", "model_v1.pt"),
                content="A" * 4096,
                # The fixture runner waits for both providers to fully
                # replicate before declaring P1 ready.
                qos_expressions=(("country=PL | country=SK", 2),),
            ),
        ),
        notes=(
            "Both PL and SK are EU; the agent must report both as fully "
            "replicated EU providers in its answer."
        ),
    ),
    oracle_check=(
        "Parse provider names + 'fully replicated' set from answer. "
        "Cross-validate via get_file_distribution REST side-channel: assert "
        "both 'cloud-pl' and 'Cloud-SK' actually carry 100% block coverage "
        "and the agent's answer names them both as fully-replicated EU."
    ),
)

P2 = Scenario(
    id="P2",
    band="placement",
    brief=(
        f"Find files under `/{SPACE}/p2/critical/` whose QoS rule is in the "
        "`impossible` state (i.e. the federation cannot satisfy the "
        "requirement). For each such file, report (path, qosExpression, "
        "status)."
    ),
    oracle_tier="static",
    required_tools=frozenset({"list_files_recursively", "get_file_qos_summary"}),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "list_files_recursively",
            "get_file_qos_summary",
        }
    ),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("P2", "critical", "violator.bin"),
                content="x" * 1024,
                # country=DE is unfulfillable on our PL+SK providers, so
                # the QoS rule will be in 'impossible' state.
                qos_expressions=(("country=DE", 1),),
            ),
            FileFixture(
                _path("P2", "critical", "ok.bin"),
                content="y" * 1024,
                # country=PL is fulfillable (cloud-pl satisfies it).
                qos_expressions=(("country=PL", 1),),
            ),
        ),
        notes=(
            "Uses country=DE as the unfulfillable expression. If the "
            "federation later acquires a German provider, this fixture "
            "must be revised — see design/04."
        ),
    ),
    oracle_check=(
        "Parse list of (path, expression, status) tuples from answer. "
        "Cross-validate via get_file_qos_summary REST side-channel: assert "
        "exactly one fixture file under /p2/critical/ has a rule whose "
        "status is 'impossible', the agent's answer names that file, and "
        "the reported expression matches the fixture rule."
    ),
)

P3 = Scenario(
    id="P3",
    band="placement",
    brief=(
        f"Ensure `/{SPACE}/p3/result.bin` is replicated on at least 2 EU "
        "providers. Add the appropriate QoS requirement, then verify by "
        "polling that EITHER (a) a replication transfer for this file "
        "appears in the space transfer log, OR (b) the QoS rule reaches "
        "`fulfilled` status. Wait up to 60 seconds. Report which condition "
        "was observed and the corresponding evidence (transferId or "
        "qosRequirementId)."
    ),
    oracle_tier="dynamic",
    required_tools=frozenset(
        {
            "add_file_qos_requirement",
            "list_space_transfers",
            "get_file_qos_summary",
        }
    ),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "add_file_qos_requirement",
            "list_space_transfers",
            "get_transfer",
            "get_file_qos_summary",
        }
    ),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("P3", "result.bin"),
                content="z" * 8192,
                # Initially scoped to ONE provider so the new EU rule
                # actually triggers a transfer.
                qos_expressions=(("country=PL", 1),),
            ),
        ),
    ),
    oracle_check=(
        "Within 60s of agent's qos-rule add, REST side-channel must show "
        "EITHER (a) a transfer for this file in list_space_transfers with "
        "state in {ongoing, ended} OR (b) get_file_qos_summary returning "
        "the new requirement with status == 'fulfilled'. AND the agent's "
        "answer correctly identifies which condition was observed."
    ),
)

P4 = Scenario(
    id="P4",
    band="placement",
    brief=(
        f"Identify the most-recent transfer in space `{SPACE}` involving "
        f"the file `/{SPACE}/p4/relocated.bin`. Report only the transferId."
    ),
    oracle_tier="dynamic",
    required_tools=frozenset({"list_space_transfers", "get_transfer"}),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "list_space_transfers",
            "get_transfer",
        }
    ),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("P4", "relocated.bin"),
                content="abc" * 256,
            ),
        ),
        transfers=(
            TransferFixtureHint(
                src_path=_path("P4", "relocated.bin"),
                target_provider_name="Cloud-SK",
                transfer_type="migration",
            ),
        ),
        notes=(
            "Pre-staged migration: fixture runner must trigger and wait "
            "for completion of one migration of this file before the "
            "trial starts. The captured transferId becomes the oracle's "
            "ground truth."
        ),
    ),
    oracle_check=(
        "Parse transferId from answer. Assert it matches the transferId "
        "captured by the fixture runner during pre-staging (most-recent "
        "transfer for /p4/relocated.bin in the federation transfer log)."
    ),
)

P5 = Scenario(
    id="P5",
    band="placement",
    brief=(
        f"Examine `/{SPACE}/p5/conflicted.bin`. It carries multiple QoS "
        "requirements that may conflict. Report all rule expressions "
        "currently attached and identify whether any are mutually "
        "exclusive (e.g. country=PL alongside country=SK with replicas_num "
        "= 1 each). Format each rule as 'expr=<expression>; "
        "replicas=<N>; status=<status>'."
    ),
    oracle_tier="static",
    required_tools=frozenset({"get_file_qos_summary"}),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "get_file_qos_summary",
            "get_qos_requirement",  # ablation extra; agent may use either
        }
    ),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("P5", "conflicted.bin"),
                content="conflict",
                qos_expressions=(
                    ("country=PL", 1),
                    ("country=SK", 1),
                ),
            ),
        ),
    ),
    oracle_check=(
        "Parse the rules from answer. Cross-validate via "
        "get_file_qos_summary REST side-channel: assert both "
        "'country=PL' and 'country=SK' rules exist on the fixture file "
        "and the agent reports both expressions plus a conflict / "
        "mutual-exclusion note."
    ),
)

P6 = Scenario(
    id="P6",
    band="placement",
    brief=(
        f"Find every file under `/{SPACE}/p6/single-copy/` whose effective "
        "QoS requires only 1 replica. Report the full paths as a bullet "
        "list."
    ),
    oracle_tier="static",
    required_tools=frozenset({"list_files_recursively", "get_file_qos_summary"}),
    allowed_tools_minimal=frozenset(
        {
            "list_user_spaces",
            "list_files_recursively",
            "get_file_qos_summary",
        }
    ),
    fixture=Fixture(
        files=(
            FileFixture(
                _path("P6", "single-copy", "lone1.bin"),
                content="lone",
                qos_expressions=(("country=PL", 1),),
            ),
            FileFixture(
                _path("P6", "single-copy", "lone2.bin"),
                content="lone",
                qos_expressions=(("country=SK", 1),),
            ),
            FileFixture(
                _path("P6", "single-copy", "redundant.bin"),
                content="redundant",
                qos_expressions=(("country=PL | country=SK", 2),),
            ),
        ),
    ),
    oracle_check=(
        "Parse paths from answer. Cross-validate via get_file_qos_summary "
        "REST side-channel for each fixture file: set-equality of reported "
        "paths with the federation's actual single-replica set "
        "(replicas_num=1 in the merged QoS summary). Expected: "
        "{'/.../p6/single-copy/lone1.bin', '/.../p6/single-copy/lone2.bin'}; "
        "redundant.bin (replicas_num=2) is a distractor."
    ),
)


# ---------------------------------------------------------------------------
# Authoritative scenario list (preserves paper Table 3 order).
# ---------------------------------------------------------------------------

SCENARIOS: tuple[Scenario, ...] = (
    D1,
    D2,
    D3,
    D4,
    D5,
    D6,
    A1,
    A2,
    A3,
    A4,
    A5,
    A6,
    P1,
    P2,
    P3,
    P4,
    P5,
    P6,
)


def assert_allowed_tools_subset_of_headline() -> None:
    """Sanity: every per-scenario minimal allowlist is in the headline 15.

    Scenarios may also include ablation-extras tools (e.g. get_qos_requirement
    in P5) — those are fine; the harness will resolve `allowed_tools_minimal`
    against ABLATION_FULL when running the ablation sweep, and against
    HEADLINE for the headline sweep, picking only those overlapping.
    """
    from benchmark.tool_allowlist import ABLATION_FULL

    for sc in SCENARIOS:
        non_full = sc.allowed_tools_minimal - ABLATION_FULL
        assert not non_full, (
            f"{sc.id}: tools in allowed_tools_minimal but neither in HEADLINE "
            f"nor ABLATION_EXTRAS: {sorted(non_full)}"
        )
        non_headline = sc.required_tools - HEADLINE
        assert not non_headline, (
            f"{sc.id}: required_tools includes tools not in HEADLINE 15: "
            f"{sorted(non_headline)} — scenario can't be solved in the "
            "headline sweep."
        )


# Run sanity at import time so a malformed scenario surfaces immediately
# rather than at first benchmark dispatch.
assert_allowed_tools_subset_of_headline()
