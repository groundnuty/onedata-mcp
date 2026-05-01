"""Federation-specific constants for the benchmark scenarios.

These values are tied to the live SPICE deployment as of 2026-05-01. If
the federation changes (a provider is rebound, a new provider joins, the
benchmark space is recreated), update these constants and re-run the
scenario smoke.

See `papers/ppam-2026/research/27-benchmark-space-snapshot.md` for the
authoritative federation snapshot and `research/empirical-onedata-25.0-findings.md`
entry #14 for why we're forced to use providerId-based QoS expressions
rather than user-attribute-based ones (admin tags `country=`, `geo=`,
`type=` are unset on this federation).
"""

from __future__ import annotations

# Provider IDs (live federation 2026-05-01). Both bound to the benchmark
# space `ppam_2026_mcp_tests` with 10 GiB POSIX support each.
PROVIDER_ID_CLOUD_PL = "27c0f483c4e451e1cf45fd2a5f5640b9chd591"
PROVIDER_ID_CLOUD_SK = "736092c5e769bf7bef4354712ac8b2b5ch1411"

# Convenience aliases for QoS expressions.
QOS_PL = f"providerId={PROVIDER_ID_CLOUD_PL}"
QOS_SK = f"providerId={PROVIDER_ID_CLOUD_SK}"
QOS_BOTH_OR = f"{QOS_PL} | {QOS_SK}"  # any of the two providers
QOS_ANY = "anyStorage"
