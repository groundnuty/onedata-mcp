# Validation panel for the not-yet-validated tools (PPAM protocol, small)

Status: spec for implementation (2026-11-19). Branch `feat/readiness-validation-panel`, off `feat/k2-readiness`.

## Why

PPAM 2026 validated the HEADLINE-16 against a 7-LLM panel (K=8). A federated-data agent study also exposes tools that
never went through such a panel:
- `check_readiness` and `ensure_readiness` (new, K2);
- `get_file_attributes`, inherited upstream and never swept;
- `schedule_file_replication` and `remove_qos_requirement`, added after the PPAM freeze.

This panel applies the PPAM *protocol*, not the full evaluation, to those five tools. The protocol:
- scripted scenarios with a pre-state fixture;
- a two-axis oracle (answer, federation end-state);
- one space per model;
- K repetitions;
- failures sorted into harness bug / deployment artefact / model failure before anything counts.
The goal is to find wording that weak models misread, arguments they get wrong, and real bugs. It does not measure the
agent study's outcome.

## Guards (agreed with the coordinator; binding)

1. **Synthetic data only.** Neutral prompts: never the agent study's prompts, and nothing from its own domain. The
   domain here is "instrument logs".
2. Findings may change **understanding only**: misread wording, wrong arguments, bugs. Never workflow advice. An agent
   that computes too early is not a description to fix.
3. **Record everything.** Each trial's artefact; each change with its triggering trial, and a before/after of each
   description or answer; per-model results. All of it goes in `artefacts/k2val/<run_id>/` (gitignored), plus a
   committed summary `research/k2val-<run_id>.md`.
4. **Concurrency:** at most 2 trials at once per model. **Anthropic spend** (the Haiku configs and the Sonnet control)
   is tracked and recorded per call and per run, in one shared tally. The cap is a config value (`[spend] cap_usd`,
   default $50; the operator lifted the earlier $5 Haiku cap). Refuse to start an Anthropic trial once the run's spend
   plus a $0.50 reserve would exceed the cap, and never make a call once the spend has reached it.
5. K1 tools: findings come out as a report only. No code change here.

## Federation (a local Compose federation; one scratch space per config)

- Spaces, one per config: `k2val-glm`, `k2val-qwen36`, `k2val-qwencoder`, `k2val-deepseek`, `k2val-haiku-low`,
  `k2val-haiku-medium`, `k2val-haiku-high`, `k2val-sonnet`, and for wave 2 `k2val-gemini` and `k2val-gpt`. Plus
  `k2-live` (manual live checks, not used by the panel).
- Each space is owned by `nordic-marine-dc` (the *owner*) and supported by `vistula-dc` (the *local* provider, the
  agent's site).
- Config: `benchmark/k2val/k2val.toml.example` (committed) and `k2val.toml` (gitignored). It holds:
  - the Onezone host;
  - the local provider host and its token-file path;
  - the owner provider host and its token-file path;
  - the space name per config;
  - the two provider names;
  - each config's settings (below);
  - the key-file paths;
  - the Anthropic spend cap (`[spend] cap_usd`, default 50);
  - for wave 2, `[cli.jail] wrapper` and `[cli.scan]` (below).
- **Read tokens and keys from files at run time; never log, print or persist them.** Mask them in any error text.
- **Refuse to run** unless every federation host (zone and providers) is `127.0.0.1`, `localhost` or ends in
  `.datahub.test`.

## Models (as the agent study serves them)

Wave 1 is the eight API configs, and they must work end to end first. Wave 2 is the two CLI agent legs, disabled by
default.

| name | backend | api_id | sampling / extra |
|---|---|---|---|
| sonnet-medium (control) | Anthropic Messages API, key file `anthropic.key` | `claude-sonnet-5-5` | no sampling params (Sonnet 5.5 rejects them); `thinking = {type: "adaptive"}` (disabled is rejected); `output_config = {effort: "medium"}`. A capable reference that should pass every scenario |
| glm | openai_compat, base `https://llmlab.plgrid.pl/api/v1`, key file `lk` | `zai-org/GLM-5.3-Flash` | temperature 1.0, top_p 0.95; nothing else (reasoning effort stays at the server default) |
| qwen36 | openai_compat, same base and key | `Qwen/Qwen3.6-35B-A3B` | PPAM's settings: temperature 1.0, no other sampling fields |
| qwencoder | openai_compat, same base and key | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | temperature 0.7, top_p 0.8, top_k 20, repetition_penalty 1.05 (top_k and repetition_penalty go in `extra_body`, as vLLM takes them); non-thinking model |
| deepseek | openai_compat, same base and key | `deepseek-ai/DeepSeek-V4-Flash-0731` | temperature 1.0, top_p 0.95; nothing else (reasoning at the server default). Chosen because in an earlier study's traces it read "incomplete" as "absent" and claimed done too early, the failure modes V5 and V7 probe |
| haiku-low, haiku-medium, haiku-high | Anthropic Messages API, key file `anthropic.key` | `claude-haiku-5-5` | the same model at three efforts: `output_config = {effort: "low" / "medium" / "high"}`; no sampling params; `thinking = {type: "adaptive"}`; price tiers ≤200k prompt in/out $0.10/$0.50 per Mtok, above $0.50/$2.50 |
| gpt-luna-low (wave 2) | Codex CLI 0.169, `codex exec --json --ephemeral --dangerously-bypass-approvals-and-sandbox` (in the jail only) | `gpt-5.6-luna` | `-c model_reasoning_effort=low` |
| gemini-flash-low (wave 2) | Antigravity CLI 1.5.12, `agy --output-format stream-json --dangerously-skip-permissions --print=<prompt>` (in the jail only) | `gemini-3.8-flash-low` | `--effort low` |

- The openai_compat adapter exists (`benchmark/llm_adapters/openai_compat.py`). Extend it only as needed to pass
  `top_p`, `top_k`, `repetition_penalty` and `extra_body`. It already echoes `reasoning_content` back in the assistant
  turn when a model returns it. Keep current behaviour as the default.
- Add `benchmark/llm_adapters/anthropic_api.py`: a minimal Messages-API tool loop. Add the `anthropic` package to the
  **dev** dependency group only; the server must not depend on it. Follow the official SDK docs for tool use,
  adaptive thinking and effort, and verify the parameter names against the docs, not from memory. Thinking blocks
  must be passed back unchanged in the next assistant turn, as the API requires. Accepted effort values are `low`,
  `medium`, `high`, `xhigh` and `max` (SDK `OutputConfigParam`).
- Anthropic spend (the Haiku configs and the Sonnet control together) is tracked per call and per run; see Guards.
- Key files: `<key dir>/lk` and `<key dir>/anthropic.key`.
- **CLI legs (wave 2).** `gpt-luna-low` and `gemini-flash-low` drive the real agent CLIs, which send everything they see
  to OpenAI or Google. They are disabled by default and run only when ALL of these hold; otherwise the run skips them,
  says so, and continues with the API configs. They never run unjailed.
  - `[cli.jail] wrapper` is set in `k2val.toml` (`benchmark/k2val/jail/cli-jail.sh`). The CLI is started through one
    function, `jail_cmd(argv, workdir, cli=..., mcp_upstream=..., home_files=...) -> list[str]`, into the jail described
    in "The CLI jail" below.
  - A clean `k2val scan` record exists for the config. The scan collects sample tool answers through the same server
    (`list_space_providers`, `get_file_distribution`, `check_readiness` on a fixture), the tool definitions and the
    prompts, and checks them against a token list file (one token per line, case-insensitive substring) and a regex
    patterns file. It prints only match counts and categories, never the matched text, and writes a record to the run
    directory.
  - The MCP server for a CLI leg runs OUTSIDE the jail, as an HTTP server on `127.0.0.1` (an ephemeral port), started by
    the harness with the token in its own env. The CLI gets only the URL, `http://127.0.0.1:<P>/mcp`, which the jail
    serves inside its own network namespace (codex `-c mcp_servers.onedata.url=...`; agy an
    `mcpServers.onedata.serverUrl` entry in the fresh home's `~/.gemini/config/mcp_config.json`, since agy's
    `--settings` rejects `mcpServers`). The user's own codex and agy config is never read or modified. The trace
    separates MCP tool calls from the CLI's built-in tool calls and reports both.

## The CLI jail

Everything a CLI leg sees is sent to OpenAI or Google. The jail (`benchmark/k2val/jail/`) lets it see only an empty work
dir, a fresh home holding nothing but that CLI's login, and the MCP server's URL.

    cli-jail.sh --cli codex|agy --workdir <dir> [--mcp-upstream 127.0.0.1:<P>] [--home-file <rel>=<file>]...
                -- <cli argv...>

Files: `cli-jail.sh` (the jail), `fwd_out.py` and `fwd_in.py` (the two forwarders), `netns.py` (egress filter and
slirp attach), `leak-probe.sh` with `leak_probe.py` and `probe_in.py` (the proof). All
the Python is standard library only. No local path is written in any of them: paths come from arguments, `$HOME` and
`command -v`.

**Filesystem.** bwrap with `--unshare-user/pid/ipc/uts/net/cgroup-try`, `--disable-userns`, `--die-with-parent`,
`--new-session` (no controlling terminal) and `--hostname jail`. bwrap's own arguments arrive through a file
descriptor (`--args`), so no host path shows in the jail's `/proc/1/cmdline`. The jail holds:
- a read-only `/usr`, with `/usr/local` hidden under an empty tmpfs;
- generated `/etc/{resolv.conf,hosts,passwd,group,nsswitch.conf}`, plus read-only `/etc/ssl` and
  `/etc/ca-certificates`;
- a private `/tmp`, its own `/proc`, a minimal `/dev`, and a read-only root;
- `/home/agent`, a fresh home staged per run, holding only that CLI's login, copied from the invoking user:
  - codex: `~/.codex/auth.json`, `installation_id`;
  - agy: `~/.gemini/antigravity-cli/{antigravity-oauth-token,installation_id}` and
    `~/.gemini/{google_accounts.json,installation_id,settings.json}` if present;
  - plus any `--home-file` (the harness: agy's `mcp_config.json`);
- `/work`, the empty work dir;
- `/opt/cli`, a staged copy (`cp -L`: every symlink resolved, no inode shared with the host) of the CLI binary and,
  for codex, its sibling helper binaries;
- `/opt/k2val`, the inside forwarder;
- `/run/k2val`, the socket dir.

The work dir and the staging root must be neutral temp paths: under `/tmp`, `/var/tmp` or `/dev/shm`, naming neither
`$HOME`, the user, nor `Users`. The jail refuses anything else, because bind-mount sources show in its
`/proc/self/mountinfo`. The harness puts each trial's scratch under `$K2VAL_JAIL_TMPDIR` or `/tmp`, never `$TMPDIR`. The
CLI runs under `env -i` with exactly `PATH`, `LANG`, `HOME` (and `CODEX_HOME` for codex); bwrap's own `PWD` is
removed.

**Network: the one change from the bwrap jails used before.** A shared network namespace (`--share-net`) exposes every
service on the host's loopback (the Onedata providers, agent channel servers and so on). So:
- the jail gets its own netns (`--unshare-net`), and `slirp4netns --configure --disable-host-loopback --enable-seccomp`
  is attached to bwrap's child. Its pid comes from `--info-fd`. bwrap's child sits in a user namespace nested inside the
  one that owns the netns, so slirp is given the owning one by fd (`--userns-path=/proc/self/fd/N`, from
  `NS_GET_USERNS`). `--enable-sandbox` cannot work there and is not used;
- DNS goes to public resolvers (`1.1.1.1`, `8.8.8.8` in the generated `/etc/resolv.conf`) over the NATed egress.
  slirp's own resolver `10.0.2.3` is off (`--disable-dns`) and rejected by the filter: it relays to the HOST's
  resolver, which answered inside the jail for the host's own name and its tailnet name (seen before this change);
- slirp runs with `--disable-dns` and `--enable-seccomp`;
- **an egress filter, beyond the brief.** `--disable-host-loopback` only stops the host's `127.0.0.1`. slirp opens
  connections from the host, so without a filter the jail reached the host's own LAN, container-bridge and tailnet
  addresses, services bound to `0.0.0.0`, other machines on those networks, and the cloud metadata service at
  `169.254.169.254` (all verified open in a prototype). `netns.py filter` joins the netns' owning user namespace, holds
  `CAP_NET_ADMIN` as an ambient capability for `nft` alone, installs an OUTPUT-hook table, and reads it back to check it.
  The table accepts loopback. It rejects every private, shared (100.64/10), link-local,
  loopback, reserved and multicast IPv4 range, the whole subnet of every host interface (`local/prefixlen`, so the
  host's neighbours too) and any point-to-point peer, and all IPv6. The rest, public IPv4, is accepted. The host's
  networks come from `ip -j addr`; if `ip` is missing, fails or times out, the filter and so the jail fail (fail
  closed). The jailed process has no capability over that netns, so it can neither read nor flush the table;
- the MCP server is reached through a UNIX socket:
  1. the harness's MCP server listens on `127.0.0.1:<P>` outside;
  2. `fwd_out.py` serves it on `<staging>/sock/mcp.sock` (0600, in a 0700 dir; the upstream must be loopback);
  3. the socket dir is bind-mounted read-only at `/run/k2val`;
  4. `fwd_in.py`, under the jail's `/usr/bin/python3`, binds `127.0.0.1:<P>` inside the jail's netns, forks the relay,
     and execs the CLI. The port listens before the CLI starts, and the URL is the same inside and out (the server's
     `Host` check passes).

  Both forwarders tear a connection down whole on the first EOF or error on either side (no half-closed connection
  waits on the other), keep at most 16 open (more are closed on accept), and close one idle for 1500 s. That is above
  a trial's 20-minute wall cap on purpose: a tool call may run 300 s silently and an MCP GET stream may idle all trial.

**Start and teardown.**
- The CLI starts only once the filter, slirp and the forwarder are up. bwrap waits on `--block-fd` for one byte from a
  FIFO that it also holds open read-write, so EOF can never release it.
- slirp's `--exit-fd` is bwrap's `--sync-fd`, so slirp ends with the jail.
- The outside forwarder has `PR_SET_PDEATHSIG` and a parent check.
- bwrap sets a jailed process's parent-death signal only after the block-fd read. Before the release, a small reaper ends
  the jail if the script is SIGKILLed.
- On exit or SIGTERM/INT/HUP the script kills the jail's PID 1, which ends its PID namespace, then bwrap, slirp and the
  forwarder, and removes the staging dir. During that cleanup further signals are ignored, so a second SIGTERM cannot
  cut it short.
- The jail can strip the permissions of what it owns (its home, the login's dir, `/work`: `chmod 0`) so that a plain
  `rm -rf` fails and leaves the login copy behind. Both removals, the script's (staging dir) and the harness's
  (`remove_tree`, the trial's scratch with `/work`), first give every directory back `u+rwx`, top-down and never
  through a symlink, then remove, then check: anything left is an error (the script exits 4), never silent.
- A SIGKILL of the script skips that removal. The harness therefore stages inside the trial's scratch dir (its
  `K2VAL_JAIL_TMPDIR`), removes that dir itself, and cancels with SIGTERM, then SIGKILL after 10 s.

**Logins.** If the CLI rewrites a staged login file (a token refresh), the jail names the file on stderr, never its
content, and discards it with the home. That post-exit check treats the staged paths as jail-written: a path that is
no longer a regular non-symlink file (a FIFO, a symlink to `/dev/zero` or a host file) is named as replaced and never
opened, and each hash runs under `timeout 10`, so it can neither hang nor defer a SIGTERM for long. The host's own login is never written. Nothing of the jail's home is copied
out: the trace's MCP half is the server's own record, and the CLI's events come on its stdout.

**Harness wiring.**
- codex runs `exec --json --ephemeral --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox`, because bwrap
  is the sandbox. The last message is written to `/work/last_message.txt`.
- agy runs `--output-format stream-json --model ... --effort ... --dangerously-skip-permissions --print=<prompt>`, with
  no `--sandbox` and the MCP server in the fresh home. `--print` takes the prompt as its value, so a prompt can never be
  read as a flag. The old `--settings` and `agy mcp add` modes are gone: one rejects `mcpServers`, and the other would
  register the server in a home that is deleted. `mcp_attach = "mcp_add"` is a config error.
- No wrapper still means no CLI legs.
- Everything the jail writes is untrusted on the host. `/work/last_message.txt` is read only through `read_jail_file`
  (`jail/__init__.py`): opened `O_NOFOLLOW|O_NONBLOCK` relative to an `O_NOFOLLOW` directory fd, `fstat`-checked to be
  a regular file with one link, and at most 1 MiB. A symlink (to a host file, `/proc/self/environ`, `/dev/zero`), a
  FIFO, a device, a directory or a hard link there is refused unread, and the trial is an adapter error. The jail's
  stdout and stderr are read in chunks and capped at 64 MiB; past that the jail is stopped and the run marked
  truncated.

**Event streams, from the two real smokes** (one-word prompt, no MCP server; field names only):
- codex 0.169 `--json`: `thread.started` {thread_id}; `turn.started`; `item.completed` {item {id, type: agent_message,
  text}}; `turn.completed` {usage {input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens,
  reasoning_output_tokens}}. A non-JSON line, "Reading additional input from stdin...", comes first. The parser already
  matched this envelope.
- agy 1.5.12 `stream-json`: every line is `{"event": <kind>, <kind>: {...}}`:
  - `init` {conversation_id, cwd, model, permission_mode ("always-proceed"), tools};
  - `step_update` {conversation_id, step_index, step_type (`user_input`, `agent_response`, ...), state (`ACTIVE`,
    `DONE`), text_delta, usage, duration_seconds};
  - `result` {conversation_id, status ("SUCCESS"), response, num_turns, duration_seconds, usage {input_tokens,
    output_tokens, thinking_tokens, cache_read_tokens, total_tokens}}.

  agy reaches MCP servers through its built-in `call_mcp_tool`. The old parser read Gemini-CLI-style `type` events and
  found neither the answer nor the tokens. It now reads `result.response`, or else the last `agent_response` text, and
  `result.usage`, with output counted as output plus thinking tokens.
- Not seen yet: tool-call events. codex `mcp_tool_call`/`command_execution` items and agy tool steps remain
  UNVERIFIED. The MCP half of every trace is the server's own record, so those parsers only add the built-in calls.

**Residual risks.**
- The jail sends the vendor everything the CLI sees, by design: the prompts, the tool definitions and the MCP answers.
  `k2val scan` is the guard there.
- DNS goes to public resolvers, so every name the jail looks up is seen by them (as it would be by any resolver).
- `/usr` is the host's, read-only, and visible. `/proc` shows the kernel version and CPU and memory facts. The staging
  and work-dir temp paths show in mountinfo.
- slirp4netns runs on the host without `--enable-sandbox`; a libslirp flaw would act with the user's rights.
- The CLIs' account-level tools still act on the logged-in account. agy auto-approves tools such as knowledge, inbox
  and subagent management, which work on server-side account state. Use dedicated vendor accounts.
- A token refreshed inside the jail is discarded, and two concurrent trials share one login.
- The login sits in the jail's home, readable by the CLI's own shell and file tools, and public egress is open on every
  port. A misbehaving or prompt-injected agent could therefore send that token anywhere on the internet, not only to its
  vendor. This follows from running the vendor's CLI with its login and internet access; dedicated accounts limit what
  a stolen token reaches.

**How to verify.**
1. `make test`: the unit tests of the forwarders, `netns.py`, the bwrap arguments
   (`cli-jail.sh --print-args`) and the jail's refusals.
2. `make k2val-jail-probe` (or `make test-k2val-jail-live`): the leak probe runs `probe_in.py` INSIDE the same jail, with
   the harness's arguments (`--cli`, `--workdir`, `--mcp-upstream`) and no CLI. It prints a PASS/FAIL table and exits 0
   only if every row passes. Per CLI it checks:
   1. the home, `/mnt`, `/Users`, `~/.ssh`, the repository, the config and every token/key path are absent; no
      host-identifying string is in `/proc/{1,self}/{cmdline,environ}`, `mountinfo`, `/etc` or the hostname; `/`,
      `/etc` and `/home` hold exactly the jail's entries; `/opt/cli` holds own regular files (no symlink, one link);
   2. the env is exactly the allowed names;
   3. the home holds exactly that CLI's login files;
   4. the host's loopback (`127.0.0.1:31443/31444`, `10.0.2.2` on several ports, a canary on the host's
      `127.0.0.1`), a `0.0.0.0` canary via every host IPv4 address, port 22 on those addresses, and the metadata
      service are all unreachable. Each target IS reachable from the host at that moment (or is marked "not
      listening"), so no row passes vacuously; the canaries count zero connections. slirp's DNS relay `10.0.2.3`
      answers no query, and each internal-only name found at probe time (the host's name, that name under each search
      domain, its tailnet name; only those that resolve on the host) does not resolve inside;
   5. a verified TLS handshake each to api.openai.com, chatgpt.com and generativelanguage.googleapis.com (so public
      DNS works);
   6. `127.0.0.1:<P>` inside reaches a dummy upstream that answers with a nonce;
   7. host processes are not visible (pid 1 is bwrap; a host process with a marker is not seen); also no capabilities,
      no new user namespaces, nft can neither list nor flush, no `/dev/tty`, only fds 0-2;
   8. no staging dir, jail process or slirp is left after a normal exit and a SIGTERM; after a SIGKILL no process is
      left, and the staging dir is left for the caller to remove. Two hostile runs, where the probe plays a CLI that
      tampers before exiting: after a `chmod 0` of the home, the login's dir and `/work`, every login copy is still
      removed; after a FIFO and a `/dev/zero` symlink replace the login files, the script still ends at once and names
      them.
   Each row added after the first review (`/opt/cli`, the DNS rows, the two hostile runs) failed on the code before
   its fix, as the commit messages record.
3. A negative control proves the probe can fail: a copy of the jail without the filter and without
   `--disable-host-loopback` fails every reachability row of (4).
4. Teardown before the release, which the probe's signal cases do not reach (they signal a running probe), was checked
   by hand on two scratch copies:
   - a forced network failure (slirp with `--enable-sandbox`) returns exit 3 at once, through a pipe, with the staging
     dir removed and no bwrap or slirp process left;
   - a SIGKILL of the script while it waits before the release byte frees the caller's pipe in under a second (the
     reaper ends the jail). No jail, slirp or forwarder process is left; the staging dir stays for the caller.
5. `make k2val-jail-smoke` (after the probe): one fixed prompt per CLI ("Reply with the single word OK."), no MCP
   server. It prints the exit code, the parsed final message and the event shapes (field names only).

## Tools offered per scenario (as the agent study's sets)

- K1 set: `add_file_qos_requirement, get_file_attributes, get_file_distribution, get_file_metadata,
  get_file_qos_summary, get_qos_requirement, get_transfer, list_space_providers, list_space_transfers,
  remove_qos_requirement, schedule_file_replication`.
- K2 set: the K1 set plus `check_readiness, ensure_readiness`.
- The MCP app is the in-process server from this branch, as in the PPAM harness. Pass the allow-list like PPAM's
  `allowed_tools`.

## System prompt (neutral, the same for every scenario)

"You are an assistant with access to tools of a federated data management system (Onedata). Your site is the provider
`vistula-dc`. Use the tools to complete the task, then give a short final answer."

The agent study's sentences are not used.

## Fixtures

All files live under `/<space>/logs/`. They are synthetic 256 KiB files named `log_YYYYMMDD.dat`, with random bytes
from a fixed seed per file.

- **At owner only (S0):** write the file through the OWNER provider's REST, so its blocks are at nordic-marine-dc and
  none are local.
  - Create: `POST /data/{parentId}/children?name=...&type=REG`.
  - Write: `PUT /data/{id}/content`, with the whole body and no offset (this truncates to 0 first, which is fine for
    a new file).
  - Confirm with the owner's `/distribution` that the owner covers the file and the local provider holds 0 bytes.
- **Being written (S2), mirroring the preallocated mode of the reference ingest tool** (its `preallocate` and
  `ingestsparse` steps):
  1. Create the file at the owner.
  2. Extend it with an empty write at its final size: `PUT /data/{id}/content?offset=<size>`, empty body. On 25.0 this
     sets the size and writes no block.
  3. Write the prefix at offset 0.
  4. A background writer then writes the rest in parts at their offsets on a fixed schedule from the trial's first
     model turn.
  - Never write without an offset.
  - Confirm each state on the owner's distribution before going on.
- **Locally replicated:** after an S0 fixture, replicate to the local provider with a transfer, and confirm local
  coverage.
- **QoS rule present:** add `providerId=<local id>` on the file, and wait until it is fulfilled and locally covered.
- **Reset** before each trial, in the model's own space:
  - delete every QoS requirement on the space's files;
  - delete `/logs/`;
  - wait until the owner answers the old ids absent;
  - then build the fixture.
  - Delete-then-recreate under the same names can race on 25.0, so use a fresh subdirectory per trial,
    `/logs-<trial_id>/`, and put that path in the brief.

## Scenarios (7)

Each has a brief, a fixture, the tool set, and a two-axis oracle. `mcp_pass` = the answer is right (judged by string
and regex rules on the final answer, as in PPAM). `federation_pass` = the end-state is right, or `None` where nothing
should change. The briefs name the directory and the dates, and everything the agent cannot discover with the tools
offered (V1 names its six files, as the K1 set has no listing tool); they never name a tool.

1. **V1 attributes** (K1). Six S0 files; two of them also locally replicated.
   - Brief: "Which of these files in `<dir>` are fully stored at your site vistula-dc: log_20200101.dat, ...,
     log_20200106.dat? List the names of those that are." (all six names spelled out)
   - mcp: exactly the two names, no others.
   - federation: None.
2. **V2 replicate** (K1). One S0 file.
   - Brief: "Copy `<dir>/log_20200105.dat` to your site vistula-dc and tell me when its data are fully there."
   - federation: local covers the file at the end of the trial.
   - mcp: the answer says it is there AND the local copy covered the file when the answer was given. Read the
     distribution immediately after the final turn; a "done" with local not yet covering is a FAIL, "premature
     claim".
3. **V3 remove rule** (K1). One S0 file with a QoS rule present.
   - Brief: "A placement rule keeps `<dir>/log_20200107.dat` at vistula-dc. Remove that rule."
   - federation: the file has no QoS requirement.
   - mcp: the answer says it was removed.
4. **V4 check, remote** (K2). Five S0 files, 2020-01-01..05.
   - Brief: "Are the log files in `<dir>` for 1–5 January 2020 complete and readable at your site? If not, why not?"
   - mcp: the answer says not ready, with a reason meaning "at another site/provider", and never claims ready.
   - federation: None.
5. **V5 check, arriving** (K2). Five files, 2020-01-01..05: three S0, two S2 whose writing finishes 20 minutes after
   the first turn.
   - Same brief as V4.
   - mcp: not ready, with a reason meaning "still being written", naming or counting the two files. A plain "not
     ready" without the reason is a FAIL.
   - federation: None.
6. **V6 ensure, remote** (K2). Five S0 files.
   - Brief: "Make the log files in `<dir>` for 1–5 January 2020 complete and readable at your site, and tell me when
     they are."
   - federation: all five locally covered at the end.
   - mcp: the answer says ready, and the local copy covered all five when it was given (else "premature claim").
7. **V7 ensure, arriving** (K2). Five files: three S0, two S2 whose writing finishes 6 minutes after the first turn.
   That is longer than one 240 s `ensure_readiness` call, so the agent must call again.
   - Same brief as V6.
   - federation: all five covered locally and complete.
   - mcp: the answer says ready, given after the writer finished and after local coverage; else "premature claim".
   - Also record, from the trace, whether any `ensure_readiness` answer said `ready: true` before the writer finished.
     That would be a K2 violation, a tool bug, reported separately in large letters.

## Runner and report

- New: `python -m benchmark.k2val.run --config k2val.toml --models glm,qwen36,qwencoder,haiku --scenarios V1..V7 --k 3`, and a
  `make k2val` target (make is the interface). It reuses `benchmark/_runtime_types.py` (OracleResult, ToolCall,
  AgentTrace) and the artefact pattern of `benchmark/_trial_artefact.py`. It does not change the PPAM harness's
  behaviour; the PPAM tests must still pass.
- Per trial, record:
  - the full trace (every tool call with its arguments, answer and timing);
  - the final answer;
  - both oracle axes and the diagnosis;
  - wall time and tokens;
  - Haiku cost;
  - the serving fingerprint if the API returns one (`system_fingerprint`).
- Limits: max 40 tool rounds and a 20 min wall cap per trial; a cap reached counts as no result.
- Report `research/k2val-<run_id>.md`:
  - a pass^k table per model × scenario;
  - every FAIL with its diagnosis and the two or three trace lines that show why;
  - the tool-call sequences per scenario;
  - wrong arguments;
  - errors returned by tools.

  It must not count anything yet: leave a column for the manual three-layer classification. The pass^k figures are raw
  and unclassified.
  - The three Haiku configs sit side by side, so the effort comparison reads directly.
  - `sonnet-medium` is the control: it comes first, as a control column. Every scenario where the control fails (any
    trial that is not a pass) is flagged **DESIGN SUSPECT** (a wrong tool, scenario or oracle), before any weak-model
    result for that scenario is read.

## Tests (unit, no network)

- Adapters: request shape, including the sampling/extra fields; thinking blocks passed back; tool-call round trip.
  Use fakes.
- Fixtures: the REST call sequence for S0 and S2 against pytest-httpx fakes of Onedata 25.0's shapes, including the
  empty write at the final size, and no write without an offset in S2.
- Oracles: pass/fail cases for each scenario, including "premature claim".
- Runner: concurrency ≤2 per model; the Haiku budget refusal; secrets never in the artefacts (assert that the token
  string from a fake token file does not appear in any written file).
- The host guard refuses non-local hosts.
