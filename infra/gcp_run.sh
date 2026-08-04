#!/usr/bin/env bash
# Shard a task pool across the prepared warm GCP fleet and launch one runtime.
#
# Usage:
#   OPENROUTER_API_KEY=... infra/gcp_run.sh POOL LABEL MAX_TOOLS [MODEL] [THINKING] [VARIANT] [MODE]
# MODE is semantic, semantic-plus, or semantic-simple. The public wrapper uses
# semantic-simple: the compact read/click/type tool surface.
set -euo pipefail

pool="${1:?task-pool JSON required}"
label="${2:-semantic-v1-dev}"
max_tools="${3:-40}"
model="${4:-qwen/qwen3.6-27b}"
thinking="${5:-medium}"
variant="${6:-semantic_runtime_v1}"
mode="${7:-semantic-simple}"
project="${GHOST_OSWORLD_GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
zone="${GHOST_OSWORLD_GCP_ZONE:-us-central1-a}"
if [ -n "${GHOST_OSWORLD_GCP_FLEET:-}" ]; then
  IFS=',' read -r -a fleet <<< "$GHOST_OSWORLD_GCP_FLEET"
else
  fleet=(semantic-os-1)
fi
concurrency_cap="${GHOST_OSWORLD_CONCURRENCY_CAP:-8}"
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

case "$project" in ""|"(unset)")
  echo "Set GHOST_OSWORLD_GCP_PROJECT or select a gcloud project." >&2
  exit 2
esac
test -n "${OPENROUTER_API_KEY:-}"
test -f "$pool"
jq -e 'type == "array" and length > 0' "$pool" >/dev/null
[[ "$label" =~ ^[A-Za-z0-9._-]+$ ]]
[[ "$max_tools" =~ ^[0-9]+$ ]]
[[ "$model" =~ ^[A-Za-z0-9._:/-]+$ ]]
[[ "$thinking" =~ ^(off|low|medium|high)$ ]]
[[ "$variant" =~ ^[A-Za-z0-9._-]+$ ]]
[[ "$mode" =~ ^(semantic|semantic-plus|semantic-simple)$ ]]
[[ "$concurrency_cap" =~ ^[1-9][0-9]*$ ]]
test "${#fleet[@]}" -gt 0
pool_sha="$(sha256sum "$pool" | awk '{print $1}')"

tmp_dir="$(mktemp -d /tmp/osworld-gcp-run.XXXXXX)"
cleanup() {
  find "$tmp_dir" -type f -delete 2>/dev/null || true
  rmdir "$tmp_dir" 2>/dev/null || true
}
trap cleanup EXIT

key_file="$tmp_dir/openrouter.key"
umask 077
printf '%s' "$OPENROUTER_API_KEY" > "$key_file"

for index in "${!fleet[@]}"; do
  vm="${fleet[$index]}"
  shard="$tmp_dir/shard-$index.json"
  jq --argjson n "${#fleet[@]}" --argjson i "$index" \
    '[to_entries[] | select((.key % $n) == $i) | .value]' "$pool" > "$shard"
  count="$(jq length "$shard")"
  shard_sha="$(sha256sum "$shard" | awk '{print $1}')"
  if [ "$count" -eq 0 ]; then
    gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet --command \
      "printf '0\\n' > \"\$HOME/run.expected\"
       printf '%s\\n' '$pool_sha' > \"\$HOME/run.pool_sha\"
       rm -f \"\$HOME/run.result_dir\" \"\$HOME/run.label\" \
         \"\$HOME/run.shard_path\" \"\$HOME/run.shard_sha\""
    echo "$vm: no tasks in shard"
    continue
  fi

  gcloud compute scp "$shard" \
    "$vm:ghost-semantic-os/pools/" --project="$project" --zone="$zone" --quiet
  gcloud compute scp "$key_file" \
    "$vm:/tmp/" --project="$project" --zone="$zone" --quiet
  # The remote command starts in the repository. Pass a relative path and let
  # gcp_remote_run resolve it before changing into harness/. Escaping "$HOME"
  # here used to deliver a literal dollar-sign path to the remote script.
  remote_shard="pools/$(basename "$shard")"
  remote_key="/tmp/$(basename "$key_file")"
  remote_label="${label}-shard${index}"
  concurrency="$count"
  if [ "$concurrency" -gt "$concurrency_cap" ]; then
    concurrency="$concurrency_cap"
  fi

  output="$tmp_dir/launch-$vm.log"
  gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet --command \
    "install -m 600 '$remote_key' \"\$HOME/.openrouter_key\" && rm -f '$remote_key' && \
     cd \"\$HOME/ghost-semantic-os\" && \
     bash infra/gcp_remote_run.sh \"$remote_shard\" '$remote_label' '$max_tools' \
       '$concurrency' '$model' '$thinking' '$variant' '$pool_sha' '$shard_sha' \
       '$mode'" \
    > "$output" 2>&1
  if grep -q "SERVER_OK" "$output" && grep -q "LAUNCH_VERIFIED" "$output"; then
    echo "$vm PASS: $(grep 'LAUNCH_VERIFIED' "$output" | tail -1)"
  else
    echo "$vm FAIL — launch output:"
    tail -30 "$output"
    exit 1
  fi
done
