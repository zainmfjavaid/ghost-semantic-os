#!/usr/bin/env bash
# Environment-feasibility gate for GCP OSWorld runs.
set -euo pipefail

pool="${1:?task-pool JSON required}"
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$repo_dir/infra/audit_pool_preflight.py" "$pool"
exec bash "$repo_dir/infra/gcp_run.sh" "$@"
