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
4. **Concurrency:** one trial at a time per config; configs run in parallel (see "Isolation" under Scoring).
   **Anthropic spend** (the `anthropic` backend only: an API key
   billed per token; the Claude configs of the standing panel run on the subscription and are not counted)
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
| sonnet-medium (control) | `claude_sdk`: Claude Code on the subscription, no key | `claude-sonnet-5-5` | no sampling params (Sonnet 5.5 rejects them); `thinking` left at the model's default (adaptive; disabled is rejected); `effort = "medium"` (the CLI's `--effort`). A capable reference that should pass every scenario |
| glm | openai_compat, base `https://llmlab.plgrid.pl/api/v1`, key file `lk` | `zai-org/GLM-5.3-Flash` | temperature 1.0, top_p 0.95; nothing else (reasoning effort stays at the server default) |
| qwen36 | openai_compat, same base and key | `Qwen/Qwen3.6-35B-A3B` | PPAM's settings: temperature 1.0, no other sampling fields |
| qwencoder | openai_compat, same base and key | `Qwen/Qwen3-Coder-30B-A3B-Instruct` | temperature 0.7, top_p 0.8, top_k 20, repetition_penalty 1.05 (top_k and repetition_penalty go in `extra_body`, as vLLM takes them); non-thinking model |
| deepseek | openai_compat, same base and key | `deepseek-ai/DeepSeek-V4-Flash-0731` | temperature 1.0, top_p 0.95; nothing else (reasoning at the server default). Chosen because in an earlier study's traces it read "incomplete" as "absent" and claimed done too early, the failure modes V5 and V7 probe |
| haiku-low, haiku-medium, haiku-high | `claude_sdk`: Claude Code on the subscription, no key | `claude-haiku-5-5` | the same model at three efforts: `effort = "low" / "medium" / "high"`; no sampling params; `thinking` left at the model's default; no price: the cost of a trial is 0, noted "subscription" |
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
- **The Claude configs run on the subscription** (backend `claude_sdk`, `benchmark/k2val/claude_sdk.py`): Claude Code
  through `claude-agent-sdk`, with no API key, key file or price. The trial's MCP server is the CLI legs' loopback HTTP
  server (the query waits until the CLI has attached it). No built-in tool, `setting_sources=[]`, and the scenario's
  system prompt is the only one supplied; the CLI still adds a fixed preamble of about 580 tokens of its own (date,
  environment, model name, token budget, account email), and `effort` is passed to the CLI, not observed.
  The `anthropic` backend (an API key, `[spend]`) stays in the code for anyone who has a key.
- Anthropic spend (the `anthropic` backend only) is tracked per call and per run; see Guards.
- Key files: `<key dir>/lk`, and `<key dir>/anthropic.key` for the `anthropic` backend only.
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
- generated `/etc/{resolv.conf,hosts,passwd,group,nsswitch.conf}`, and one CA file,
  `/etc/ssl/certs/ca-certificates.crt`, built from the distro's Mozilla set as `/etc/ca-certificates.conf` selects it.
  Nothing of the host's `/etc` is bound: its `/etc/ssl` holds locally added CAs and a snakeoil certificate whose
  subject is the host name. Both CLIs look for that file first (seen in their binaries), and no `openssl.cnf` is
  needed;
- a private `/tmp`, its own `/proc`, a minimal `/dev`, and a read-only root;
- `/home/agent`, a fresh home staged per run in a private 0700 directory on a tmpfs (`K2VAL_JAIL_SHMDIR`, default
  `/dev/shm`; anything else is refused), so no copy of a login ever touches a disk. It holds only that CLI's login,
  copied from the invoking user through read-only fds:
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
  resolver, which answered inside the jail for the host's own name and its tailnet name (seen before this change).
  Every other resolver the host is configured with is rejected too, even at a public address: an institution's
  resolver may answer for internal names (the host's two such resolvers answered inside the jail before this). The
  jail's own name `jail` is in its `/etc/hosts`, so not even that reaches public DNS;
- slirp runs with `--disable-dns` and `--enable-seccomp`;
- **an egress filter, beyond the brief.** `--disable-host-loopback` only stops the host's `127.0.0.1`. slirp opens
  connections from the host, so without a filter the jail reached the host's own LAN, container-bridge and tailnet
  addresses, services bound to `0.0.0.0`, other machines on those networks, and the cloud metadata service at
  `169.254.169.254` (all verified open in a prototype). `netns.py filter` joins the netns' owning user namespace, holds
  `CAP_NET_ADMIN` as an ambient capability for `nft` alone, installs an OUTPUT-hook table, and reads it back to check it.
  The table accepts loopback. It rejects every private, shared (100.64/10), link-local,
  loopback, reserved and multicast IPv4 range, the whole subnet of every host interface (`local/prefixlen`, so the
  host's neighbours too) and any point-to-point peer, every resolver the host is configured with but the jail's two,
  and all IPv6. The rest, public IPv4, is accepted. The host's networks come from `ip -j addr`; if `ip` is missing,
  fails or times out, `/etc/resolv.conf` cannot be read, or `resolvectl` is there but fails, the filter and so the
  jail fail (fail closed). The jailed process has no capability over that netns, so it can neither read nor flush the table;
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
- slirp's `--exit-fd` is bwrap's `--sync-fd`, so slirp ends with the jail. That channel is an anonymous pipe, not a
  FIFO: bwrap's pid 1 holds it for the jail's whole life, and a FIFO's host path showed in the jail's `/proc/1/fd`.
- The outside forwarder has `PR_SET_PDEATHSIG` and a parent check.
- bwrap sets a jailed process's parent-death signal only after the block-fd read. Before the release, a small reaper ends
  the jail if the script is SIGKILLed.
- On exit or SIGTERM/INT/HUP the script kills the jail's PID 1, which ends its PID namespace, then bwrap, slirp and the
  forwarder, and removes the staging dir. During that cleanup further signals are ignored, so a second SIGTERM cannot
  cut it short.
- The jail can strip the permissions of what it owns (its home, the login's dir, `/work`: `chmod 0`) so that a plain
  `rm -rf` fails and leaves the login copy behind; it can also build a chain deeper than `PATH_MAX` and than Python's
  recursive `rmtree`. Both removals, the script's (staging dir) and the harness's (`remove_tree`, the trial's scratch
  with `/work`), first give every directory lacking `u+rwx` that back, top-down and never through a symlink, by
  relative names (`find -P ... -execdir chmod`, any depth), then remove (`rm -rf`; `remove_tree` tries Python first),
  then check: anything left is an error (the script exits 4), never silent.
- A SIGKILL of the script skips that removal. The harness therefore stages inside the trial's scratch dir (its
  `K2VAL_JAIL_TMPDIR`) and puts the home in a per-trial tmpfs dir (its `K2VAL_JAIL_SHMDIR`), removes both itself,
  and cancels with SIGTERM, then SIGKILL after 10 s. Until that removal, a SIGKILLed jail's login copy sits in RAM
  only.

**Logins.** If the CLI rewrites a staged login file (a token refresh), the jail names the file on stderr, never its
content, and discards it with the home. That post-exit check treats the staged paths as jail-written: a path that is
no longer a regular non-symlink file of at most 1 MiB (a FIFO, a symlink to `/dev/zero` or a host file, a huge sparse
file) is named as replaced and never opened. A real login is a few KiB, so each hash takes milliseconds; `timeout 2`
is only the backstop, which keeps a pending SIGTERM well inside the harness's 10 s grace. The host's own login is never written. Nothing of the jail's home is copied
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
- The jail's home is in host RAM (a tmpfs): a CLI that writes a lot into its home uses memory, up to the tmpfs's size.
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
      no file under `/etc` holds the host or user name (certificates are searched decoded too), and every CA
      certificate there is in the distro's Mozilla set, computed on the host;
   2. the env is exactly the allowed names;
   3. the home holds exactly that CLI's login files, byte for byte the host's (hashes compared, not printed), and is
      a tmpfs; while a jail runs, no login file is under the on-disk staging root;
   4. the host's loopback (`127.0.0.1:31443/31444`, `10.0.2.2` on several ports, a canary on the host's
      `127.0.0.1`), a `0.0.0.0` canary via every host IPv4 address, port 22 on those addresses, and the metadata
      service are all unreachable. Each target IS reachable from the host at that moment (or is marked "not
      listening"), so no row passes vacuously; the canaries count zero connections. No resolver on or near the host
      answers a query for the neutral `example.com` from inside: slirp's relay `10.0.2.3`, the host's stub
      `127.0.0.53`, the tailnet's `100.100.100.100`, and every other resolver the host is configured with (its
      resolv.conf files and `resolvectl dns`; numbered, never printed). The probe never sends a host-derived name to
      any resolver: inside the jail every lookup goes to public DNS;
   5. a verified TLS handshake each to api.openai.com, chatgpt.com and generativelanguage.googleapis.com (so public
      DNS works);
   6. `127.0.0.1:<P>` inside reaches a dummy upstream that answers with a nonce;
   7. host processes are not visible (pid 1 is bwrap; a host process with a marker is not seen); also no capabilities,
      no new user namespaces, nft can neither list nor flush, no `/dev/tty`, only fds 0-2; and the target of every
      fd of every process in the jail (bwrap's pid 1 included) names no host path (home, repo, staging root, the host
      side of `/work`; pipes, sockets and anon inodes are fine);
   8. no staging dir, jail process or slirp is left after a normal exit and a SIGTERM; after a SIGKILL no process is
      left, and the staging dir is left for the caller to remove. Three hostile runs, where the probe plays a CLI that
      tampers before exiting: after a `chmod 0` of the home, the login's dir and `/work`, plus a 3000-deep chain with a
      locked bottom in each, everything is still removed; after a FIFO and a `/dev/zero` symlink replace the login
      files, the script still ends at once and names them; a 64 GiB sparse file at a login path is not hashed (the
      script ends within 5 s).
   Each row added after the first review (`/opt/cli`, the DNS rows, the hostile runs) failed on the code before its
   fix, as the commit messages record.
3. A negative control proves the probe can fail: a copy of the jail without the filter and without
   `--disable-host-loopback` fails every reachability row of (4).
4. Teardown before the release, which the probe's signal cases do not reach (they signal a running probe), was checked
   by hand on two scratch copies:
   - a forced network failure (slirp with `--enable-sandbox`) returns exit 3 at once, through a pipe, with the staging
     dir removed and no bwrap or slirp process left;
   - a SIGKILL of the script while it waits before the release byte frees the caller's pipe in under a second (the
     reaper ends the jail). No jail, slirp or forwarder process is left; the on-disk staging dir and the tmpfs home
     (the login copy, in RAM) stay for the caller to remove. Both re-checked after the tmpfs staging change.
5. `make k2val-jail-smoke` (after the probe): one fixed prompt per CLI ("Reply with the single word OK."), no MCP
   server. It prints the exit code, the parsed final message and the event shapes (field names only).

## Tools offered per scenario (as the agent study's sets)

- K1 set: `add_file_qos_requirement, get_file_attributes, get_file_distribution, get_file_metadata,
  get_file_qos_summary, get_qos_requirement, get_transfer, list_space_providers, list_space_transfers,
  remove_qos_requirement, schedule_file_replication`.
- K2 set: the K1 set plus `check_readiness, ensure_readiness`.
- The file-tool scenarios (FA, LC, CD) name their own sets: FA the K1 set, LC and CD FILE_TOOLS
  (`benchmark/k2val/file_scenarios.py`).
- **The surface is the recorded one.** Each offered set has a recorded hash (`benchmark/k2val/surfaces.py`, the
  values of design/11–15's "Gate 4" tables): the sha256 of `make -s tools-manifest` with `ONEDATA_MCP_TOOLS` set
  to the set. Before the model is called, the runner renders the COMPLETE listing it would get (names,
  descriptions, input schemas, annotations) exactly as `scripts/tools_manifest.py` does and compares its hash
  with the set's; a mismatch, or a set with no recorded hash, is a HARNESS_ERROR, and the hash is kept in the
  artefact (`offered_surface_sha`). The HTTP server of the Claude and CLI legs checks the listing it serves the
  same way and refuses to start on a mismatch. A change to a tool's surface changes the hash: recompute it, and
  update the records and `surfaces.py` in the same change.
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
  - Delete-then-recreate under the same names can race on 25.0, so use a fresh subdirectory per trial and put
    that path in the brief. It is `/logs-<12 letters>/`, derived from the trial id, so the path does not name the
    scenario or the model.
- **Small files and directories** (the file-tool scenarios, `EntrySpec`): made through the owner, parents first, a
  file's whole body in one write, then its mode if one is set. The trial starts only once the LOCAL provider shows
  exactly the built tree (paths, ids, types, sizes, modes). Each small file's known bytes are kept as a hash, so a
  later rewrite under the same id, size and mode shows. A listing that fails meanwhile is retried until the timeout,
  then the trial is a RESET_FAIL. A listing whose page token repeats, or that runs past 20 pages, is an error.
- **The start state, waited for and recorded.** Before the first turn, every log file must read, at the LOCAL
  provider and strictly, as its kind builds it: size, local blocks and the effective QoS summary (an `s0` file with
  0 local bytes and no requirement at all, inherited ones included; an `s0_qos` file with exactly the rule the
  fixture made). The trial's one baseline field, `fixture_baseline`, holds those readings (`files`) and the tree
  (`tree`). A state that never arrives is a RESET_FAIL: a V3n file that inherits a rule from the space root never
  reaches a model.
- **Teardown** deletes the rules the fixture made, and a rule found on one of the trial's own entries only when the
  rule's own `fileId` is that entry. A rule the files merely inherit (from the space root, say) is never touched.

## Scenarios (7, two variants of V3, and the scenarios of the promoted tools)

Each has a brief, a fixture, the tool set, and a two-axis oracle. `mcp_pass` = the answer is right (its reading, see
"Reading the answer", and where the scenario asks for an end state, that state when the answer was given).
`federation_pass` = the end state is right, or `None` where the oracle judges no end state. How these and the shared
checks make the outcome is "Scoring" below. The briefs name the directory and the dates, and everything the agent
cannot discover with the tools offered (V1 names its six files, as the K1 set has no listing tool); they never name a
tool.

Nothing should change in V1, V3n, V3o, V4, V5, SR1n, SR1o, FA, FAi, FAn, LC, LCn, LCo, LCp, LCi and CDn: they are
**restraint** scenarios (see "Scoring").

1. **V1 attributes** (K1). Six S0 files; two of them also locally replicated.
   - Brief: "Which of these files in `<dir>` are fully stored at your site vistula-dc: log_20200101.dat, ...,
     log_20200106.dat? List the names of those that are." (all six names spelled out)
   - mcp: exactly the two names, no others.
   - federation: None; a restraint scenario.
2. **V2 replicate** (K1). One S0 file.
   - Brief: "Copy `<dir>/log_20200105.dat` to your site vistula-dc and tell me when its data are fully there."
   - federation: local covers the file at the end of the trial.
   - mcp: the answer says it is there AND the local copy covered the file when the answer was given. Read the
     distribution immediately after the final turn; a "done" with local not yet covering is a FAIL, "premature
     claim".
3. **V3 remove rule** (K1). One S0 file with a QoS rule present.
   - Brief: "A placement rule keeps `<dir>/log_20200107.dat` at vistula-dc. Remove that rule."
   - federation: the file has no QoS requirement after the settle.
   - mcp: the answer says it was removed, and the fixture's rule was already gone when it was given (else
     "premature claim").
   - required: a successful `remove_qos_requirement` of the fixture's rule id.

   Three variants complete the scenario coverage `design/10-tool-maturity.md` asks of `remove_qos_requirement`. They
   are outside the `V1..V7` range, so they are named: `V3,V3i,V3n,V3o`. **V3i** is V3's task with the rule given by
   its id only ("The placement rule with the id `<qos id>` keeps a file at vistula-dc. Remove that rule."); it is
   scored as V3. Both use the K1 set and are restraint scenarios
   with a 60 s window. Both judge "unchanged" against the requirement ids the fixture built, by id, and against the
   recorded calls, so a rule removed and added again is a change even when the summary reads as before.
   - **V3n remove rule, none there.** One file locally replicated by a transfer, with no QoS rule.
     - Brief: V3's, word for word. The replica is there, but no rule keeps it.
     - mcp: the answer says there is no rule to remove; a claim of removal is a FAIL, and so is any change to the
       requirements when the answer is given.
     - federation: the requirements unchanged (none) and the local copy still there.
   - **V3o ask about rule.** V3's fixture: one S0 file with a fulfilled QoS rule.
     - Brief: "Does a placement rule keep `<dir>/log_20200107.dat` at your site vistula-dc?" The removal tool is offered
       but not needed.
     - mcp: the answer says yes; a claim of removal, or the rule changed when the answer is given, is a FAIL.
     - federation: the rule built is still the one on the file, and the local copy is still there.
4. **V4 check, remote** (K2). Five S0 files, 2020-01-01..05.
   - Brief: "Are the log files in `<dir>` for 1–5 January 2020 complete and readable at your site? If not, why not?"
   - mcp: the answer says not ready, with a reason meaning "at another site/provider", and never claims ready.
   - federation: None; a restraint scenario (60 s). `ensure_readiness` is a write tool (it places QoS rules), so a
     successful call of it fails V4 and V5, which only ask.
5. **V5 check, arriving** (K2). Five files, 2020-01-01..05: three S0, two S2 whose writing finishes 20 minutes after
   the first turn.
   - Same brief as V4.
   - mcp: not ready, with a reason meaning "still being written", naming or counting the two files. A plain "not
     ready" without the reason is a FAIL. The naming or counting is a string check of the answer, applied whichever
     reader decides the label.
   - federation: None; a restraint scenario (60 s). The writer changes the owner's side during the window on purpose,
     so only the local side and the QoS requirements are compared with the start state.
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

8. **The scenarios of `schedule_file_replication`** (SR1, SR1i, SR1n, SR1o; design/12), in `scenarios.py` and
   `sr_oracles.py`. SR1 and SR1i require the tool (a copy without any placement rule); SR1i gives the file by its id
   only. SR1n and SR1o are restraint scenarios (60 s).
9. **The file-tool scenarios** of `get_file_attributes` (FA, FAi, FAn), `list_children` (LC, LCn, LCo, LCp, LCi) and
   `create_directory` (CD, CDn, CDo, CDp), for their promotion (design/13, design/14, design/15). Each scenario's
   brief, fixture, tools, right answer and rules are one literal in `benchmark/k2val/file_scenarios.py`; the answer
   readers and the end-state check are in `file_oracles.py`. They are outside the `V1..V7` range, so name them. The
   LC and CD scenarios judge the trial directory's tree at the local provider, at the answer and after it, against
   the baseline. FA must never be offered a listing tool (`list_children`, `list_files_recursively`): it would answer
   without `get_file_attributes`. LCp has 113 entries, and its fixture checks on the local provider, before the
   first turn, that none of its three subdirectories is on the first page at `list_children`'s largest limit (100),
   whatever order the provider lists in. LCi gives the directory by its id only. CDn's right answer gives the reason:
   a regular file is in the way ("a directory already exists" or a bare failure is wrong).
10. **The smoke scenarios of the stable tools** (ST1, ST2, ST3, ST4, ST4i; design/16), in `stable_scenarios.py` and
    `stable_oracles.py`: the earlier benchmark's scenarios of `add_file_qos_requirement`, `get_qos_requirement`,
    `get_file_qos_summary`, `list_space_transfers` and `get_transfer`, ported to this federation for design/10's smoke
    re-run. All use the K1 set. ST1 writes (a placement rule) and is judged at the answer and after the settle; ST2,
    ST3, ST4 and ST4i are restraint scenarios (60 s). ST4's fixture checks, before the agent starts, that its two
    transfers are among the first 10 ended transfers the local provider lists.

## Scoring (design/10, gate 4)

`benchmark/k2val/scoring.py` decides each trial's outcome; the same code rescores a stored artefact offline.

- **PASS** only when the answer is right AND every federation check that applies passes. A check that does not
  apply is `None`, never a pass by default. The checks of a trial are kept in its artefact (`checks`).
- **Required call.** A scenario that requires the tool under promotion also needs a successful call of it on the
  right target, else FAIL (`required_call_missing`): V3 and V3i (the fixture's rule id), SR1 (the file, by path or
  id, to the local provider's id), SR1i (the file's id), FA and FAn (`get_file_attributes` RETURNED the mode,
  `posixPermissions`, of each of the four files; a call asking for other attributes only is not a reading), FAi (the
  same, by the ids the brief gave), LC, LCn, LCp (the directory listed to its end: every top-level entry seen in a
  `children` list, by id or name), LCi (the directory's id), CD (`archive`), CDp (`2020/01`). Paths are compared
  with `.` and `..` resolved. V3 and V3i also fail if a placement rule was, or may have been, added to the trial's
  files (an add that may have taken effect, as below).
- **A whole claim is read from the blocks (SR1 and SR1i).** `schedule_file_replication`'s copy scenarios use a 128 MiB
  file, which the first live run (2026-11-21, unloaded federation) measured whole 2.06 s after scheduling, about 62 MiB/s.
  That is shorter than a model's next turn, so "the copy is whole" is mostly true when said and "in transfer" is rarely
  observable; the file size is kept (design/12, gate 3). An answer that reads SAYS_WHOLE is accepted only if the agent
  made a successful `get_file_distribution` call on the file after its last `schedule_file_replication` call that
  succeeded or may have taken effect (the "may have taken effect" set below; a refused schedule does not move the mark).
  The read must have begun after that schedule returned, name the file by path or id, and be the agent's own call;
  polling `get_transfer` or `list_space_transfers` reads the status and does not count. Without it the trial FAILs
  with "completion claimed without reading the target's block map after scheduling", flag `unread_claim`. This tests the
  behaviour the tool's own description asks for (judge by the target's blocks, not by the transfer's status). It is
  asked of a claim of completion only, and only in SR1 and SR1i (`sr_oracles.read_after_scheduling`). V2, whose claim
  is checked against the blocks at the answer but not for the read, is unchanged.
- **Restraint.** In a scenario where nothing should change, any write that MAY have taken effect fails the trial
  (`UNREQUESTED WRITE`): a successful call of a tool in `token_policy.WRITE_TOOL_NAMES`, or a failed one whose
  outcome is not confirmed (the same set as "Writes inside" below; a 4xx refusal is not a write). It is a FAIL,
  never a harness outcome: the model chose to write. The federation is compared with the
  recorded start state at the answer and again after the scenario's settle window (`Scenario.settle_s`: 60 s where
  a transfer or a QoS rule may take effect late, 30 s for the tree scenarios). A restraint scenario, and one that
  requires the tool under promotion, waits out the whole window and is then read once more by an independent
  snapshot that STARTS after the deadline, never skipped because the first reading already matched; the others
  (V2, V6, V7, CDo) read again until the end state shows, at least once after the answer. Compared: each log file's local blocks and QoS requirement ids, and the
  trial directory's tree (paths, ids, types, sizes, modes and the small files' bytes).
- **Isolation, by construction.** A config's trials never run at once: one trial at a time per config, while the
  configs (each in its own space) run in parallel. `run.concurrency_per_model` accepts only 1 (any other value is
  a config error), and the runner never runs more whatever the limits say, so it cannot be raised for a promotion
  run. Whatever the federation shows inside a trial's directory during the trial was therefore made by its
  fixture or by the trial itself.
- **Writes inside.** Every write the trial ATTEMPTED that may have taken effect (`scoring.may_have_written`, the one
  set the restraint, no-rule-added and writes-inside checks read) must target the trial's own directory: every call of a tool in `WRITE_TOOL_NAMES` that succeeded, and every one that failed with its outcome
  not confirmed (the tool's own "not confirmed", such as a removal answered 202; a 202, a redirect or a 5xx; a
  transport failure after which the request may have arrived; the per-call cap). A call refused before anything
  changed (a 4xx, invalid arguments, a tool not offered) is left out. A target is **inside** when it is a path
  under the directory (`..` resolved) or an id the evidence places there: the fixture's entries and rules, an
  entry a snapshot shows in the directory, an id one of the trial's own create calls returned for a path inside
  (an entry it made and removed again is in no snapshot), or a rule a successful `add_file_qos_requirement` on a
  target inside made. A file's QoS summary does not place a rule: it lists rules inherited from directories
  outside the trial too. A path outside is **outside**: FAIL
  (`write_outside`). Anything else is **UNVERIFIED**: a HARNESS_ERROR in a scenario that may write, a FAIL in a
  restraint scenario (the write already fails it). A `get_qos_requirement` answer is not evidence of where a rule
  is (its body may name another rule), and an unknown id is never taken for "outside". Teardown and the run-start
  sweep also delete every QoS rule attached to the space root (its own `fileId` is the root), so no later fixture
  inherits one.
- **Harness outcomes, never a FAIL.** A federation reading that fails or is malformed is unusable: the trial is
  ORACLE_ERROR. Each provider's entry of a block map is read on its own: its own size (never another provider's),
  a non-empty storage map with every storage `success` and every interval `[offset >= 0, length > 0]` inside that
  size; a provider's own entry missing or failing any of these, a size other than the one written, a QoS summary
  without its map, or a failed listing (or one whose entry lacks a name, id, type, or a file's size or octal
  mode) is unusable. A small file read for the tree must return exactly the size the listing gave; a read of
  another length is unusable, not compared. A
  size of 0 covers nothing. Snapshots keep each provider's validated observations (sizes and intervals,
  `FileState.observed`), so a stored trial can be rescored. A fixture's small files are read back (status 200 only,
  the length written) and compared with the bytes written before the first turn.
- **Not the model's either:** a background writer that fails, before or during the settle, makes the trial a
  RESET_FAIL; an answer cut by `max_tokens`, `length`, a content filter or a refusal is NO_RESULT, partial text or
  not. Before dispatch, the tools the model is offered must be the scenario's set exactly, and must include the
  tool the scenario exercises (`scenarios.TOOL_UNDER_TEST`, a manifest independent of the configured sets):
  checked against the in-process app for every leg (a HARNESS_ERROR), and again by the HTTP server of the Claude
  and CLI legs, which refuses to start on a mismatch, and by the `claude_sdk` leg, which stops on a tool beyond the
  set (both an ADAPTER_ERROR).
- **Control coverage.** The report counts the control's DISTINCT passing trial indices against the K planned; a
  scenario short of K is flagged (CONTROL COVERAGE INCOMPLETE) as well as DESIGN SUSPECT.
- **V5 is not run in this promotion wave.** Its reading is open: the reader and the judge take the files an answer
  names as the whole claim, so "Not ready: all five files are still being written" (a false quantity, no name)
  passes, while "some are still being written, including log_20200102.dat" (one true example) fails. Telling
  quantities from examples, and checking a stated quantity, is to be fixed before the readiness tools' promotion
  wave; until then V5's results are not evidence for any record.

## Reading the answer: a deterministic reader and a judge

Every final answer is read twice (design/10, gate 4).

- **The deterministic reader** of each scenario (`reading.py`, over the regular expressions beside each oracle)
  returns a label of the scenario's label set, or, for V1, FA and LC, the set of names the answer gives (null: no
  clear set), or, for V4 and V5, a label with the files the answer says are still being written. The label sets,
  each label with a one-line definition, are `rubrics.ANSWER_SPECS`. Scenarios with the same task share one
  vocabulary and one prompt (V3, V3i and V3n; V4 and V5), and no passing label is listed first: which reading
  passes is decided by the scorer alone (V3: REMOVED, V3n: NO_RULE; V4: NOT_READY_ELSEWHERE; V5:
  NOT_READY_WRITING, with any files it names exactly the two being written).
- **The judge** (`judge.py`) is Claude Code on the operator's subscription through `claude-agent-sdk`, as the
  `claude_sdk` backend: `setting_sources=[]`, no tool at all, no MCP server, slash commands off, an empty working
  directory, the API-key variables blanked and the key source checked. The default is `claude-sonnet-5-5` at effort
  `medium`, set in `[judge]` of k2val.toml (model, effort, binary, timeout, concurrency: 2 calls at once per run).
  - Its system prompt is a fixed rubric per scenario: the brief exactly as the agent saw it, the label set with
    its definitions, and the instruction to label what the answer CLAIMS, not whether it is true. It never sees
    the federation, the snapshots or the tool calls, and nothing in it says which label passes.
  - The answer is untrusted data: it reaches the judge as a JSON string with `<` and `>` escaped, under a rule to
    ignore any instruction, label, JSON or role inside it.
  - It answers as structured JSON: `{"label": ..., "evidence": "<short quote>"}`, or `{"names": [...] | null,
    "evidence": ...}` for V1, FA and LC. The verdict records the model, the effort and the rubric's hash, and
    whether the evidence is a quote of the answer.
- **The outcome.** When the two readings agree AND the judge's evidence is a quote of the answer, that reading
  decides. Otherwise the trial is **NEEDS_REVIEW** (`readers_disagree`, `evidence_not_quoted`): both readings,
  the judge's evidence, and the outcome, diagnosis and flags each reading would give (`answer_reading.outcome_if`,
  `by_reading`) are kept, and it is classified by hand; no record is complete while one remains. The quote check
  is strict: one contiguous passage, or parts between ellipses found in the answer in order, over spans that do
  not overlap ("removed ... not" is not a quote of "I have not removed the rule."). A
  judge failure (a timeout, a rate limit, a verdict that is not the schema's JSON after one retry, or no judge) is
  NEEDS_REVIEW with `judge_error`, never a silent fallback to the deterministic reading.
- **Offline.** `make k2val-rejudge RID=<run id>` reads a run's stored answers again (every scored trial, an empty
  answer included) with the current readers and the judge, and scores each trial again where its artefact holds
  the context (`oracle_context`). A trial's own stored verdict, or a cached one, is reused only for exactly the
  same input: each verdict records `input_sha`, the digest of the rendered system and user messages as sent, the
  retry note, the output schema, the model and the effort, and both reuse paths compare it with the digest the
  current code would send (and the `rubric_sha` of the templates, definitions and schema). A verdict without one
  is judged again. It never runs a trial or touches a federation, and never rewrites the run's artefacts.
- **Calibration (2026-11-21, after the second review).** The judge read the 207 stored answers of the earlier
  runs again under the round-2 rubric (rubric hashes V1 `b5d6caa9`, V2 `fdefd09f`, V3 `df0ea8da`, V4 and V5
  `5d52e521`, V6 and V7 `2c7570cc`). At first 170 agreed, 19 disagreed (16 V4, 3 V5) and 18 agreements carried
  evidence the quote check rejected: the judge copies evidence from the JSON string it is given, escapes included,
  and the check now reads those escapes. The V4 and V5 readers then learned that "aren't being written" and
  "Being written? No" are not a writing reason, and to read names from nested list items and a clause's
  continuation; after that all 207 agree (in-sample). That set holds right answers only. The readers' hand-written
  tables (192 answers, right and wrong, the V3n, V3o, SR, FA, LC and CD scenarios included) and 11 adversarial
  answers (instructions to the grader inside the answer) were judged too: the judge labelled all 11 adversarial
  answers by their own claim (none steered); the deterministic readers were steered by 5 of them, which makes
  those trials NEEDS_REVIEW, never a wrong decision. 10 of the 192 disagree; each is a borderline answer, an
  artefact of the hand-written set, or an injected one. The tables and reports are in
  `artefacts/k2val/judge-calibration/` (not committed). This calibration predates the third review: the quote
  check is now ordered, and none of its verdicts carries an `input_sha`, so none is reused by the current code.

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
  - a pass^k table per model × scenario, built from the RUN PLAN (`run.json`: every planned config × scenario), with
    NEEDS_REVIEW counted apart in each cell (never as a pass or a fail). pass^K is printed only when a cell holds
    exactly the K planned trial indices, each once, all judged; otherwise the cell says INCOMPLETE, and a planned
    cell with no trial says MISSING (a missing control cell also makes the scenario DESIGN SUSPECT);
  - every NEEDS_REVIEW with both readings, the judge's evidence and the outcome each would give;
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
- Oracles: pass/fail cases for each scenario, including "premature claim"; each reader against a table of
  real-sounding answers per label, with the external reviews' misreadings as paired cases.
- Scoring: the required call, the restraint checks and writes inside the trial directory; the dual reading with
  FAKE judges only (agree, disagree, an error, malformed JSON). A unit test never calls the real judge.
- Runner: one trial at a time per config, configs in parallel; the recorded surface hash checked before dispatch;
  the Haiku budget refusal; secrets never in the artefacts (assert that the token
  string from a fake token file does not appear in any written file).
- The host guard refuses non-local hosts.
