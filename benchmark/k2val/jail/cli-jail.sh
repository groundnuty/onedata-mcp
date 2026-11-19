#!/usr/bin/env bash
# cli-jail.sh: run ONE agent CLI (codex or agy) in a jail that shows it nothing private.
#
#   cli-jail.sh --cli codex|agy --workdir <dir> [--mcp-upstream 127.0.0.1:<P>] \
#               [--home-file <rel path>=<file>] -- <cli argv...>
#   cli-jail.sh --probe <probe.json> --cli ... --workdir ... [--mcp-upstream ...]   (leak-probe.sh)
#   cli-jail.sh --print-args --cli ... --workdir ... [--mcp-upstream ...] -- <argv>  (stages nothing)
#
# Whatever the CLI sees goes to its vendor, so the jail shows it only:
#   - an empty work dir, at /work (the --workdir, bind-mounted);
#   - a fresh home, /home/agent, holding nothing but this CLI's login files, copied per run
#     from $HOME/.codex or $HOME/.gemini of the invoking user, plus any --home-file the caller
#     writes (the harness: agy's MCP config, which holds only the URL below);
#   - with --mcp-upstream, the MCP server's URL: http://127.0.0.1:<P>/mcp inside the jail.
# and otherwise a read-only /usr (with /usr/local hidden), generated /etc files, a private /tmp,
# /proc of its own PID namespace, and a staged copy of the CLI's binaries at /opt/cli.
#
# Network: the jail has its OWN network namespace (bwrap --unshare-net). slirp4netns
# (--configure --disable-host-loopback --disable-dns) gives it outbound internet; DNS goes to
# public resolvers (1.1.1.1, 8.8.8.8), never to slirp's 10.0.2.3 relay to the host's resolver.
# The host's loopback is not reachable. `netns.py filter` installs an nft filter in the jail's
# netns, before the network comes up, that rejects every private, link-local (cloud metadata),
# shared (tailnet), loopback and reserved range and every address of the host itself; the jail
# refuses to start if the filter fails. The MCP server is reached through a UNIX socket:
#   harness MCP server 127.0.0.1:<P>  <-  fwd_out.py (outside)  <-  <staging>/sock/mcp.sock
#   bind-mounted at /run/k2val  <-  fwd_in.py (inside, listens 127.0.0.1:<P> in the jail's netns)
#
# The CLI starts only once the filter, slirp and the forwarder are up (bwrap --block-fd); slirp
# exits with the jail (bwrap --sync-fd -> slirp --exit-fd); the outside forwarder dies with this
# script (PR_SET_PDEATHSIG); the jail dies with this script (--die-with-parent). The fresh home
# (the login copies) is staged in a TMPFS dir, copied from read-only fds: no copy of a login
# ever touches a disk. The rest (binaries, /etc files, control FIFOs) is staged on disk. Both
# are removed on exit or signal; a SIGKILL of this script skips that removal: set
# K2VAL_JAIL_TMPDIR and K2VAL_JAIL_SHMDIR to directories the caller removes (the harness does).
#
# Environment: K2VAL_JAIL_TMPDIR (staging root, default /tmp; must be under /tmp, /var/tmp or
# /dev/shm), K2VAL_JAIL_SHMDIR (the home's root, default /dev/shm; must be a tmpfs, under
# /dev/shm or /tmp), K2VAL_JAIL_PYTHON (the Python for the outside helpers, default python3).
# Exit: the CLI's status; 2 for a usage or staging error; 3 if the network could not be set up;
# 4 if a staging dir (the home: the login copy) could not be removed.
set -u -o pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
PY=${K2VAL_JAIL_PYTHON:-python3}
JAIL_HOME=/home/agent

die() { echo "cli-jail: $*" >&2; exit "${2:-2}"; }

CLI="" WORKDIR="" UPSTREAM="" PROBE="" PRINT_ARGS=0
ARGV=() HOME_FILES=()
while [ $# -gt 0 ]; do
  case $1 in
    --cli|--workdir|--mcp-upstream|--probe|--home-file)
      [ $# -ge 2 ] || die "$1 needs a value"
      case $1 in
        --cli) CLI=$2 ;; --workdir) WORKDIR=$2 ;; --mcp-upstream) UPSTREAM=$2 ;;
        --probe) PROBE=$2 ;; --home-file) HOME_FILES+=("$2") ;;
      esac
      shift 2 ;;
    --print-args) PRINT_ARGS=1; shift ;;
    --) shift; ARGV=("$@"); break ;;
    *) die "unknown argument: $1" ;;
  esac
done

case $CLI in codex|agy) ;; *) die "--cli must be codex or agy" ;; esac
[ -n "$WORKDIR" ] || die "--workdir is required"
[ -n "$PROBE" ] || [ ${#ARGV[@]} -gt 0 ] || die "no CLI command after --"
PORT=""
if [ -n "$UPSTREAM" ]; then
  [[ $UPSTREAM =~ ^(127\.0\.0\.1|localhost):([0-9]{1,5})$ ]] || die "--mcp-upstream must be 127.0.0.1:<port>"
  PORT=${BASH_REMATCH[2]}
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "--mcp-upstream port out of range"
fi
# --home-file <rel>=<file>: a small regular file the caller wrote, copied into the fresh home at
# <rel> (a plain relative path) after the logins; it may not replace one of them.
for hf in ${HOME_FILES[@]+"${HOME_FILES[@]}"}; do
  rel=${hf%%=*} src=${hf#*=}
  [ "$rel" != "$hf" ] && [ -n "$rel" ] && [ -n "$src" ] || die "--home-file needs <rel path>=<file>"
  [[ $rel =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] || die "--home-file: not a plain relative path"
  case /$rel/ in */../*|*/./*) die "--home-file: not a plain relative path" ;; esac
  [ -f "$src" ] && [ ! -L "$src" ] || die "--home-file: not a regular file"
  [ "$(stat -c %s "$src")" -le 65536 ] || die "--home-file: larger than 64 KiB"
done

# A bind mount's source path shows in the jail's /proc/self/mountinfo, so the work dir and the
# staging root must be neutral temp paths: under /tmp, /var/tmp or /dev/shm, and naming no user.
: "${HOME:?cli-jail: HOME must be set (the CLI logins are read from it)}"
ME=${USER:-$(id -un 2>/dev/null || true)}
neutral_path() {
  local p=$1
  case $p in /tmp|/tmp/*|/var/tmp|/var/tmp/*|/dev/shm|/dev/shm/*) ;; *) return 1 ;; esac
  case $p in *Users*|*"$HOME"*) return 1 ;; esac
  if [ ${#ME} -ge 3 ]; then case $p in *"$ME"*) return 1 ;; esac; fi
  [[ $p =~ ^[A-Za-z0-9/._-]+$ ]]
}

if [ "$PRINT_ARGS" = 1 ]; then
  S="<staging>" H="<home>"
else
  [ -d "$WORKDIR" ] || die "the work dir does not exist"
  WORKDIR=$(cd "$WORKDIR" && pwd -P)
  neutral_path "$WORKDIR" || die "the work dir must be a neutral temp path (under /tmp, naming no user): its path shows inside the jail"
  [ -z "$(ls -A "$WORKDIR")" ] || die "the work dir must be empty"
  ROOT=${K2VAL_JAIL_TMPDIR:-/tmp}
  [ -d "$ROOT" ] || die "K2VAL_JAIL_TMPDIR does not exist"
  ROOT=$(cd "$ROOT" && pwd -P)
  neutral_path "$ROOT" || die "the staging root must be a neutral temp path (under /tmp, naming no user)"
  SHMROOT=${K2VAL_JAIL_SHMDIR:-/dev/shm}
  [ -d "$SHMROOT" ] || die "K2VAL_JAIL_SHMDIR does not exist"
  SHMROOT=$(cd "$SHMROOT" && pwd -P)
  neutral_path "$SHMROOT" || die "the home's root must be a neutral temp path (under /dev/shm, naming no user)"
  [ "$(stat -f -c %T "$SHMROOT")" = tmpfs ] || die "the home's root must be a tmpfs: a login copy may not touch a disk"
  BWRAP=$(command -v bwrap) || die "bwrap not found"
  SLIRP=$(command -v slirp4netns) || die "slirp4netns not found"
  command -v "$PY" >/dev/null || die "no Python for the helpers ($PY)"
fi

# ---- staging -------------------------------------------------------------------------------
BW_PID="" CHILD_PID="" SLIRP_PID="" FWD_PID="" REAPER_PID="" BLOCK_W="" SYNC_W="" SYNC_R=""

# The jail can strip the permissions of anything it owns (its home, the login's dir, /work) so
# that a plain rm -rf fails and leaves the login copy behind. Give every DIRECTORY lacking u+rwx
# that back, top-down and never through a symlink (find -P; `-execdir ... \;` runs, from the
# parent and by a relative name, before find reads the dir, so any depth works), then remove,
# then check. Files need no permission to be unlinked, and are left untouched.
remove_dir() {
  [ -n "$1" ] && [ -e "$1" ] || return 0
  find -P "$1" -type d ! -perm -u=rwx -execdir chmod u+rwx {} \; 2>/dev/null
  rm -rf "$1" 2>/dev/null
  if [ -e "$1" ]; then
    echo "cli-jail: ERROR: a staging dir could not be removed (it may hold a copy of the $CLI login): $1" >&2
    return 1
  fi
}
remove_staging() {
  local rc=0
  remove_dir "${H:-}" || rc=1
  remove_dir "${S:-}" || rc=1
  return "$rc"
}

cleanup() {
  local rc=$?
  # no signal may cut the cleanup short: a second TERM during it is ignored, not fatal
  trap - EXIT
  trap '' INT TERM HUP
  # The jail first: its PID-1 (killing it ends its whole PID namespace), then bwrap. Neither
  # was released if we got here early: the block FIFO is never released by EOF, only by a byte.
  for p in "$CHILD_PID" "$BW_PID" "$REAPER_PID" "$FWD_PID" "$SLIRP_PID"; do
    [ -n "$p" ] && kill -KILL "$p" 2>/dev/null
  done
  for p in "$BW_PID" "$FWD_PID" "$SLIRP_PID" "$REAPER_PID"; do [ -n "$p" ] && wait "$p" 2>/dev/null; done
  remove_staging || rc=4
  exit "$rc"
}

# One login file into the tmpfs home: read through a read-only fd (the `<` redirection), written
# owner-only; the source is never opened for writing, and must be a regular file.
copy_login() {
  [ -f "$1" ] || return 1
  (umask 077 && cat < "$1" > "$2")
}

stage_credentials() {
  local src
  case $CLI in
    codex)
      src=$HOME/.codex
      [ -f "$src/auth.json" ] || die "no codex login ($HOME/.codex/auth.json)"
      mkdir -m 700 "$H/.codex"
      copy_login "$src/auth.json" "$H/.codex/auth.json" || die "cannot stage the codex login"
      [ -f "$src/installation_id" ] && copy_login "$src/installation_id" "$H/.codex/installation_id"
      ;;
    agy)
      src=$HOME/.gemini
      [ -f "$src/antigravity-cli/antigravity-oauth-token" ] || die "no agy login ($HOME/.gemini/antigravity-cli/antigravity-oauth-token)"
      mkdir -m 700 -p "$H/.gemini/antigravity-cli"
      copy_login "$src/antigravity-cli/antigravity-oauth-token" "$H/.gemini/antigravity-cli/antigravity-oauth-token" \
        || die "cannot stage the agy login"
      [ -f "$src/antigravity-cli/installation_id" ] \
        && copy_login "$src/antigravity-cli/installation_id" "$H/.gemini/antigravity-cli/installation_id"
      for f in google_accounts.json installation_id settings.json; do
        [ -f "$src/$f" ] && copy_login "$src/$f" "$H/.gemini/$f"
      done
      ;;
  esac
  (cd "$H" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) > "$S/ctl/credentials.sha256"
}

stage_home_files() {
  local hf rel src
  for hf in ${HOME_FILES[@]+"${HOME_FILES[@]}"}; do
    rel=${hf%%=*} src=${hf#*=}
    [ ! -e "$H/$rel" ] || die "--home-file: $rel would replace a staged file"
    mkdir -p -m 700 "$(dirname "$H/$rel")" && copy_login "$src" "$H/$rel" \
      || die "--home-file: cannot stage $rel"
  done
}

# The CLI's binaries, COPIED with every symlink resolved (cp -L) to a neutral path: no host path
# shows in the jail's mountinfo, /proc/<pid>/exe or a symlink's target in /opt/cli, and no
# inode is shared with the host's own binaries (a hard link would be).
stage_binaries() {
  local src real dir f
  if [ ${#ARGV[@]} -gt 0 ]; then src=${ARGV[0]}; else src=$CLI; fi
  src=$(command -v "$src") || die "the $CLI binary was not found"
  real=$(readlink -f "$src") || die "cannot resolve the $CLI binary"
  [ -x "$real" ] || die "the $CLI binary is not executable"
  if [ "$CLI" = codex ]; then
    dir=$(dirname "$real")
    for f in "$dir"/codex*; do  # codex and its sibling helper binaries
      [ -f "$f" ] && [ -x "$f" ] || continue
      cp -L --reflink=auto "$f" "$S/bin/$(basename "$f")" || die "cannot stage $f"
    done
    [ -x "$S/bin/codex" ] || cp -L --reflink=auto "$real" "$S/bin/codex" || die "cannot stage codex"
  else
    cp -L --reflink=auto "$real" "$S/bin/agy" || die "cannot stage agy"
  fi
}

write_etc() {
  local uid gid
  uid=$(id -u) gid=$(id -g)
  # public resolvers over the NATed egress; never slirp's 10.0.2.3, which relays to the HOST's
  # resolver and so would answer for local, VPN and tailnet names (the filter drops it too, and
  # every other resolver the host is configured with). netns.JAIL_RESOLVERS names the same two.
  printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$S/etc/resolv.conf"
  # the jail's own name resolves locally, so not even it is ever sent to the public resolvers
  printf '127.0.0.1\tlocalhost\n127.0.1.1\tjail\n::1\tlocalhost\n' > "$S/etc/hosts"
  printf 'agent:x:%s:%s:agent:%s:/bin/sh\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n' \
    "$uid" "$gid" "$JAIL_HOME" > "$S/etc/passwd"
  printf 'agent:x:%s:\nnogroup:x:65534:\n' "$gid" > "$S/etc/group"
  printf 'passwd: files\ngroup: files\nhosts: files dns\n' > "$S/etc/nsswitch.conf"
  write_ca_bundle
}

# The jail's ONLY CA file: the distro's Mozilla set as /etc/ca-certificates.conf selects it (its
# `!` lines deselect), else every mozilla/*.crt. Never the host's /etc/ssl: its bundle holds
# locally added CAs, and its snakeoil certificate names the host.
write_ca_bundle() {
  local conf=/etc/ca-certificates.conf share=/usr/share/ca-certificates line f n=0
  : > "$S/etc/ca-certificates.crt"
  if [ -r "$conf" ]; then
    while IFS= read -r line; do
      case $line in mozilla/*) ;; *) continue ;; esac
      f=$share/$line
      [ -f "$f" ] || continue
      cat -- "$f" >> "$S/etc/ca-certificates.crt" && printf '\n' >> "$S/etc/ca-certificates.crt" && n=$((n + 1))
    done < "$conf"
  fi
  if [ "$n" = 0 ]; then
    for f in "$share"/mozilla/*.crt; do
      [ -f "$f" ] || continue
      cat -- "$f" >> "$S/etc/ca-certificates.crt" && printf '\n' >> "$S/etc/ca-certificates.crt" && n=$((n + 1))
    done
  fi
  [ "$n" -gt 0 ] || die "no Mozilla CA list ($share/mozilla): the jail would have no CA to trust"
}

# ---- the bwrap arguments (passed through fd 3: no host path in the jail's /proc/1/cmdline) --
bwrap_args() {
  printf '%s\0' \
    --unshare-user --unshare-pid --unshare-ipc --unshare-uts --unshare-net --unshare-cgroup-try \
    --disable-userns --die-with-parent --new-session --hostname jail \
    --ro-bind /usr /usr --tmpfs /usr/local --remount-ro /usr/local \
    --symlink usr/lib /lib --symlink usr/lib64 /lib64 --symlink usr/bin /bin --symlink usr/sbin /sbin \
    --ro-bind "$S/etc/resolv.conf" /etc/resolv.conf --ro-bind "$S/etc/hosts" /etc/hosts \
    --ro-bind "$S/etc/passwd" /etc/passwd --ro-bind "$S/etc/group" /etc/group \
    --ro-bind "$S/etc/nsswitch.conf" /etc/nsswitch.conf \
    --ro-bind "$S/etc/ca-certificates.crt" /etc/ssl/certs/ca-certificates.crt \
    --proc /proc --dev /dev --tmpfs /tmp \
    --bind "$H" "$JAIL_HOME" \
    --ro-bind "$S/bin" /opt/cli --ro-bind "$S/k2val" /opt/k2val
  [ -n "$UPSTREAM" ] && printf '%s\0' --ro-bind "$S/sock" /run/k2val
  printf '%s\0' --bind "$WORKDIR" /work --chdir /work --remount-ro / \
    --info-fd 4 --block-fd 5 --sync-fd 6
}

jail_command() {
  # bwrap sets PWD itself after its chdir, past any env option: drop it, the env stays exact
  CMD=(/usr/bin/env -u PWD)
  if [ -n "$UPSTREAM" ]; then
    CMD+=(/usr/bin/python3 -I /opt/k2val/fwd_in.py --listen "127.0.0.1:$PORT" --socket /run/k2val/mcp.sock --)
  fi
  if [ -n "$PROBE" ]; then
    CMD+=(/usr/bin/python3 -I /opt/k2val/probe_in.py /opt/k2val/probe.json)
  else
    CMD+=("/opt/cli/$CLI" "${ARGV[@]:1}")
  fi
}

jail_env() {
  ENV=(PATH=/usr/bin:/bin LANG=C.UTF-8 "HOME=$JAIL_HOME")
  [ "$CLI" = codex ] && ENV+=("CODEX_HOME=$JAIL_HOME/.codex")
  return 0
}

jail_command
jail_env
if [ "$PRINT_ARGS" = 1 ]; then
  bwrap_args | tr '\0' '\n'
  echo "--- env"
  printf '%s\n' "${ENV[@]}"
  echo "--- command"
  printf '%s\n' "${CMD[@]}"
  exit 0
fi

trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
trap 'exit 129' HUP

S=$(mktemp -d "$ROOT/k2val-jail.XXXXXXXX") || die "cannot create the staging dir"
chmod 700 "$S"
mkdir -m 700 "$S/bin" "$S/etc" "$S/k2val" "$S/sock" "$S/ctl" || die "cannot stage"
H=$(mktemp -d "$SHMROOT/k2val-home.XXXXXXXX") || die "cannot create the home in $SHMROOT"
chmod 700 "$H"
stage_credentials
stage_home_files
stage_binaries
write_etc
cp "$HERE/fwd_in.py" "$S/k2val/" || die "cannot stage the forwarder"
if [ -n "$PROBE" ]; then
  cp "$HERE/probe_in.py" "$S/k2val/" && cp "$PROBE" "$S/k2val/probe.json" || die "cannot stage the probe"
fi
bwrap_args > "$S/ctl/args"
mkfifo -m 600 "$S/ctl/info" "$S/ctl/block" "$S/ctl/slirp-ready" "$S/ctl/fwd-ready" || die "cannot stage"

# Read-write opens of a FIFO never block. The jail holds its own read-write copy of the block
# FIFO, so it never sees EOF there: it waits for the byte written below, or for the kill.
exec {BLOCK_W}<>"$S/ctl/block"
# The sync channel (bwrap --sync-fd -> slirp --exit-fd) is an ANONYMOUS pipe: bwrap's pid 1
# keeps it open for the jail's whole life, and a FIFO's host path would show in the jail's
# /proc/1/fd. One pipe, reopened through /proc as a write-only and a read-only end.
exec {SYNC_PIPE}<> <(:)
exec {SYNC_W}>"/proc/self/fd/$SYNC_PIPE" {SYNC_R}<"/proc/self/fd/$SYNC_PIPE"
exec {SYNC_PIPE}>&-

env -i "${ENV[@]}" "$BWRAP" --args 3 "${CMD[@]}" \
  3<"$S/ctl/args" 4>"$S/ctl/info" 5<&"$BLOCK_W" 6>&"$SYNC_W" {BLOCK_W}>&- {SYNC_W}>&- {SYNC_R}>&- &
BW_PID=$!
exec {SYNC_W}>&-  # only the jail holds the write end now: slirp ends when the jail does

INFO=$(timeout 30 cat "$S/ctl/info") || die "the jail did not start" 3
CHILD_PID=$(printf '%s' "$INFO" | sed -n 's/.*"child-pid": *\([0-9][0-9]*\).*/\1/p' | head -n 1)
[ -n "$CHILD_PID" ] || die "the jail did not report its pid" 3

# Until it is released, the jailed process has no parent-death signal (bwrap sets it after the
# block-fd read). If this script is SIGKILLed before the release, the reaper ends the jail.
(
  exec {BLOCK_W}>&- {SYNC_R}>&- </dev/null >/dev/null 2>&1
  trap '' TERM INT HUP
  while kill -0 "$$" 2>/dev/null; do sleep 0.5; done
  kill -KILL "$CHILD_PID" 2>/dev/null
) &
REAPER_PID=$!

# 1. the egress filter, in the still-offline netns; 2. slirp; 3. the outside forwarder.
"$PY" -I "$HERE/netns.py" filter --pid "$CHILD_PID" {BLOCK_W}>&- {SYNC_R}>&- \
  || die "no egress filter: the CLI is not started" 3

"$PY" -I "$HERE/netns.py" slirp --pid "$CHILD_PID" -- \
  "$SLIRP" --configure --mtu=65520 --disable-host-loopback --disable-dns --enable-seccomp \
  --exit-fd 7 --ready-fd 8 "$CHILD_PID" tap0 \
  7<&"$SYNC_R" 8>"$S/ctl/slirp-ready" {BLOCK_W}>&- {SYNC_R}>&- >"$S/ctl/slirp.log" 2>&1 &
SLIRP_PID=$!
exec {SYNC_R}>&-  # slirp holds the read end
[ "$(timeout 30 head -c 1 "$S/ctl/slirp-ready")" = 1 ] || die "slirp4netns did not come up: $(tail -n 3 "$S/ctl/slirp.log" 2>/dev/null | tr '\n' ' ')" 3

if [ -n "$UPSTREAM" ]; then
  "$PY" -I "$HERE/fwd_out.py" --socket "$S/sock/mcp.sock" --upstream "$UPSTREAM" \
    --ready-fd 9 --parent-pid $$ 9>"$S/ctl/fwd-ready" {BLOCK_W}>&- </dev/null &
  FWD_PID=$!
  [ "$(timeout 30 head -c 1 "$S/ctl/fwd-ready")" = 1 ] || die "the MCP forwarder did not come up" 3
fi

printf 1 >&"$BLOCK_W"  # release the jail: the CLI starts now
exec {BLOCK_W}>&-
# released, the jail has its parent-death signals (it dies with bwrap, bwrap with this script)
kill -KILL "$REAPER_PID" 2>/dev/null; wait "$REAPER_PID" 2>/dev/null
REAPER_PID=""

wait "$BW_PID"
RC=$?
BW_PID="" CHILD_PID=""  # gone: its pid must not be signalled again

# A login the CLI refreshed inside the jail is discarded with the home: name the file, show
# nothing of it. The staged paths are jail-written by now: each must still be a regular file, not
# a symlink, and at most 1 MiB before it is hashed (a FIFO there would block the read forever, a
# symlink would resolve to /dev/zero or a host file, a huge sparse file would take minutes). A
# login file is a few KiB, so each hash takes milliseconds; `timeout 2` is only the backstop that
# keeps a pending SIGTERM from being deferred for long (well under the harness's 10 s grace).
CHANGED="" REPLACED=""
while read -r sum rel; do
  path=$H/${rel#./}
  if [ -L "$path" ] || [ ! -f "$path" ] || [ "$(stat -c %s -- "$path")" -gt 1048576 ]; then
    REPLACED+="${rel#./} "
  elif [ "$(timeout 2 sha256sum < "$path" 2>/dev/null | cut -d' ' -f1)" != "$sum" ]; then
    CHANGED+="${rel#./} "
  fi
done < "$S/ctl/credentials.sha256"
if [ -n "$CHANGED" ]; then
  echo "cli-jail: warning: $CLI rewrote its login file(s) inside the jail: $CHANGED(a token" \
       "refresh?); the jail's copy is discarded, the host keeps its own" >&2
fi
if [ -n "$REPLACED" ]; then
  echo "cli-jail: warning: login path(s) removed, or replaced inside the jail by something other" \
       "than a small regular file (not read): $REPLACED" >&2
fi
exit "$RC"
