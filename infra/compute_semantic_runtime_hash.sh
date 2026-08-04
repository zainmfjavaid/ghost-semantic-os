#!/usr/bin/env bash
# Read-only fingerprint for the server and guest components of semantic-v1.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$repo_dir/infra/semantic_server_runtime_files.txt"
test -s "$manifest"

while IFS= read -r file; do
  test -n "$file" || continue
  test -f "$repo_dir/$file"
  printf '%s  %s\n' "$(sha256sum "$repo_dir/$file" | awk '{print $1}')" "$file"
done < "$manifest" | sha256sum | awk '{print $1}'
