#!/usr/bin/env bash
# leak-probe.sh: prove the CLI jail's properties with a probe run INSIDE the same jail (the same
# cli-jail.sh, the same arguments, no CLI), and print a PASS/FAIL table. Exit 0 only if all pass.
#
#   leak-probe.sh [--cli codex] [--cli agy] [--config benchmark/k2val/k2val.toml]
#
# No --cli: both. --config: the token and key files it names must be absent from the jail too.
# Needs bwrap, slirp4netns, nft and the CLIs' logins under $HOME. Sends nothing to any vendor:
# its only outbound traffic is one TLS handshake per vendor endpoint and the DNS lookups for
# them (to the public resolvers). The rows are listed in leak_probe.py.
set -u
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
exec "${K2VAL_JAIL_PYTHON:-python3}" -I "$HERE/leak_probe.py" "$@"
