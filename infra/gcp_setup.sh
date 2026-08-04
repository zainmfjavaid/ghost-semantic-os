#!/usr/bin/env bash
# Idempotent provisioning inside a nested-virtualization GCE host.
set -euo pipefail

repo_dir="${GHOST_SEMANTIC_OS_REPO:-$HOME/ghost-semantic-os}"
test -d "$repo_dir"
"$repo_dir/scripts/bootstrap-osworld.sh"
exec "$repo_dir/scripts/install-host.sh"
