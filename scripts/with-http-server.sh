#!/usr/bin/env bash
# scripts/with-http-server.sh
#
# Spawn onedata-mcp on an ephemeral HTTP port, wait until it accepts
# TCP connections, run the supplied command with $MCP_URL exported
# (and substituted via the literal token __MCP_URL__ in any arg),
# capture the exit code, and tear down the server cleanly.
#
# Usage:
#     scripts/with-http-server.sh [--port N] -- <command> [args...]
#
# Example:
#     scripts/with-http-server.sh --port 3037 -- \
#         npx @modelcontextprotocol/inspector --cli __MCP_URL__ \
#             --method tools/list
#
# Designed to be called from Makefile recipes. Exit code reflects the
# inner command's exit code, not the server's. Server logs go to
# /tmp/onedata-mcp-<pid>.log; printed on tail of failure.

set -euo pipefail

PORT=3037
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT="$2"
            shift 2
            ;;
        --)
            shift
            break
            ;;
        *)
            echo "unknown arg: $1 (use --port N -- cmd...)" >&2
            exit 2
            ;;
    esac
done

if [[ $# -lt 1 ]]; then
    echo "usage: $0 [--port N] -- <command> [args...]" >&2
    exit 2
fi

LOG="/tmp/onedata-mcp-http-${PORT}-$$.log"
URL="http://127.0.0.1:${PORT}/mcp"

echo "Starting onedata-mcp on http://127.0.0.1:${PORT}/ (log: ${LOG})..."
MCP_TRANSPORT=http MCP_PORT="${PORT}" uv run onedata-mcp >"${LOG}" 2>&1 &
SERVER_PID=$!

cleanup() {
    if kill -0 "${SERVER_PID}" 2>/dev/null; then
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

# Wait up to 30s for TCP port to accept connections.
ready=0
for _ in $(seq 1 60); do
    if (echo > "/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
        ready=1
        break
    fi
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        echo "FATAL: onedata-mcp died during startup. Server log:" >&2
        cat "${LOG}" >&2 || true
        exit 1
    fi
    sleep 0.5
done
if [[ "${ready}" -ne 1 ]]; then
    echo "FATAL: onedata-mcp did not bind to :${PORT} within 30s. Log:" >&2
    tail -40 "${LOG}" >&2 || true
    exit 1
fi
echo "onedata-mcp ready at ${URL}"

# Substitute __MCP_URL__ token in args, also export MCP_URL env.
export MCP_URL="${URL}"
SUBSTITUTED=()
for arg in "$@"; do
    SUBSTITUTED+=("${arg/__MCP_URL__/${URL}}")
done

set +e
"${SUBSTITUTED[@]}"
RC=$?
set -e

if [[ "${RC}" -ne 0 ]]; then
    echo "" >&2
    echo "Inner command exited ${RC}. Last 20 lines of server log:" >&2
    tail -20 "${LOG}" >&2 || true
fi

rm -f "${LOG}"
exit "${RC}"
