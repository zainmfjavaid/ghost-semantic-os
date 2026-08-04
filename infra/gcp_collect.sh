#!/usr/bin/env bash
# Collect the latest completed result artifact and run log from each fleet host.
set -euo pipefail

project="${GHOST_OSWORLD_GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
zone="${GHOST_OSWORLD_GCP_ZONE:-us-central1-a}"
if [ -n "${GHOST_OSWORLD_GCP_FLEET:-}" ]; then
  IFS=',' read -r -a fleet <<< "$GHOST_OSWORLD_GCP_FLEET"
else
  fleet=(semantic-os-1)
fi
test "${#fleet[@]}" -gt 0
case "$project" in ""|"(unset)")
  echo "Set GHOST_OSWORLD_GCP_PROJECT or select a gcloud project." >&2
  exit 2
esac
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
output="${1:-$repo_dir/results_gcp/$stamp}"
mkdir -p "$output"
result_files=()

for vm in "${fleet[@]}"; do
  result="$output/$vm.json"
  log="$output/$vm.log"
  expected="$(
    gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet \
      --command 'cat "$HOME/run.expected"'
  )"
  if [ "$expected" = 0 ]; then
    echo "$vm skipped (no tasks in latest pool)"
    continue
  fi
  [[ "$expected" =~ ^[1-9][0-9]*$ ]]
  gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet --command \
    'test -s "$HOME/run.result_dir"
     d=$(cat "$HOME/run.result_dir")
     test -f "$d/results.json"
     cat "$d/results.json"' \
    > "$result"
  remote_manifest="$(
    gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet --command \
      'd=$(cat "$HOME/run.result_dir"); test -f "$d/runtime-manifest.json" && printf "%s" "$d/runtime-manifest.json"' \
      2>/dev/null || true
  )"
  if [ -z "$remote_manifest" ]; then
    echo "$vm result has no runtime manifest" >&2
    exit 1
  fi
  gcloud compute scp "$vm:$remote_manifest" "$output/$vm.runtime-manifest.json" \
    --project="$project" --zone="$zone" --quiet
  manifest_sha="$(jq -r .runtime_manifest_sha256 "$output/$vm.runtime-manifest.json")"
  embedded_manifest_sha="$(jq -r '.runtimeManifestSha256 // empty' "$result")"
  if [ -n "$embedded_manifest_sha" ]; then
    test "$manifest_sha" = "$embedded_manifest_sha"
  else
    # Older semantic artifacts carry the manifest identity in the harness
    # revision rather than the dedicated result field.
    jq -e --arg hash "$manifest_sha" \
      '.harnessRevision | contains("runtime_manifest_sha256=" + $hash)' \
      "$result" >/dev/null
  fi
  gcloud compute scp "$vm:run.log" "$log" \
    --project="$project" --zone="$zone" --quiet
  gcloud compute scp \
    "$vm:ghost-semantic-os/.source_state" \
    "$vm:ghost-semantic-os/.environment_state" \
    "$vm:ghost-semantic-os/.python-freeze.txt" \
    "$output/" --project="$project" --zone="$zone" --quiet
  mv "$output/.source_state" "$output/$vm.source_state"
  mv "$output/.environment_state" "$output/$vm.environment_state"
  mv "$output/.python-freeze.txt" "$output/$vm.python-freeze.txt"
  remote_shard="$(
    gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet \
      --command 'cat "$HOME/run.shard_path"'
  )"
  gcloud compute scp "$vm:$remote_shard" "$output/$vm.shard.json" \
    --project="$project" --zone="$zone" --quiet
  remote_pool_sha="$(
    gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet \
      --command 'cat "$HOME/run.pool_sha"'
  )"
  remote_shard_sha="$(
    gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet \
      --command 'cat "$HOME/run.shard_sha"'
  )"
  printf '%s\n' "$remote_pool_sha" > "$output/$vm.pool_sha"
  printf '%s\n' "$remote_shard_sha" > "$output/$vm.shard_sha"
  test "$(sha256sum "$output/$vm.shard.json" | awk '{print $1}')" = "$remote_shard_sha"
  jq -e --argjson expected "$expected" \
    '(.results | type == "array" and length == $expected)
     and .completed == $expected
     and ([.results[] |
       has("error") or has("evaluationError") or has("cleanupError")
     ] | all(. == false))' "$result" >/dev/null
  jq -s -e '
    (.[0].results | map(.taskId) | sort)
    == (.[1] | map(split("/")[-1] | sub("\\.json$"; "")) | sort)
  ' "$result" "$output/$vm.shard.json" >/dev/null
  result_files+=("$result")
  echo "$vm collected"
done

if [ "${#result_files[@]}" -eq 0 ]; then
  echo "no non-empty shard artifacts were collected" >&2
  exit 1
fi

# Prove the distributed run was one experiment, not a collage of stale shards:
# one source/environment fingerprint, one model/variant, and no duplicate task.
jq -s -e '
  ([.[].harnessRevision] | unique | length) == 1
  and ([.[].model] | unique | length) == 1
  and ([.[].variant] | unique | length) == 1
  and ([.[].results[].taskId] as $ids
       | ($ids | length) == ($ids | unique | length))
' "${result_files[@]}" >/dev/null
test "$(sort -u "$output"/*.pool_sha | wc -l)" -eq 1

python3 "$repo_dir/infra/analyze_traces.py" "${result_files[@]}"
echo "Artifacts: $output"
