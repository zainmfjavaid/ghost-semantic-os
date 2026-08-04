#!/usr/bin/env bash
# Run a no-action grader control across the prepared GCP fleet.
#
# Run this only after a scored arm has finished. It reuses the still-running
# env server on each host, creates fresh OSWorld episodes, and collects exact
# JSON artifacts locally.
set -euo pipefail

pool="${1:?task-pool JSON required}"
label="${2:-null-control}"
project="${GHOST_OSWORLD_GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
zone="${GHOST_OSWORLD_GCP_ZONE:-us-central1-a}"
if [ -n "${GHOST_OSWORLD_GCP_FLEET:-}" ]; then
  IFS=',' read -r -a fleet <<< "$GHOST_OSWORLD_GCP_FLEET"
else
  fleet=(semantic-os-1)
fi
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

test -f "$pool"
case "$project" in ""|"(unset)")
  echo "Set GHOST_OSWORLD_GCP_PROJECT or select a gcloud project." >&2
  exit 2
esac
jq -e 'type == "array" and length > 0' "$pool" >/dev/null
[[ "$label" =~ ^[A-Za-z0-9._-]+$ ]]

tmp_dir="$(mktemp -d /tmp/osworld-gcp-null.XXXXXX)"
stamp="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
output="$repo_dir/results_gcp/${stamp}_${label}"
mkdir -p "$output"
cleanup() {
  find "$tmp_dir" -type f -delete 2>/dev/null || true
  rmdir "$tmp_dir" 2>/dev/null || true
}
trap cleanup EXIT

pids=()
active_vms=()
for index in "${!fleet[@]}"; do
  vm="${fleet[$index]}"
  shard="$tmp_dir/null-shard-$index.json"
  jq --argjson n "${#fleet[@]}" --argjson i "$index" \
    '[to_entries[] | select((.key % $n) == $i) | .value]' "$pool" > "$shard"
  count="$(jq length "$shard")"
  if [ "$count" -eq 0 ]; then
    continue
  fi

  gcloud compute scp "$shard" "$vm:ghost-semantic-os/pools/" \
    --project="$project" --zone="$zone" --quiet
  remote_pool="pools/$(basename "$shard")"
  remote_output="\$HOME/null-${label}-shard${index}.json"
  local_log="$output/$vm.log"

  gcloud compute ssh "$vm" --project="$project" --zone="$zone" --quiet --command \
    "run_pid=\$(cat \"\$HOME/run.pid\" 2>/dev/null || echo 0)
     if [[ \"\$run_pid\" =~ ^[1-9][0-9]*$ ]] && kill -0 \"\$run_pid\" 2>/dev/null; then
       echo 'refusing null control while scored run is active' >&2
       exit 1
     fi
     curl -fsS --max-time 5 http://127.0.0.1:8079/health >/dev/null
     cd \"\$HOME/ghost-semantic-os\"
     . .venv/bin/activate
     python infra/gcp_null_control.py '$remote_pool' --output \"$remote_output\"" \
    > "$local_log" 2>&1 &
  pids+=("$!")
  active_vms+=("$vm:$index:$count")
done

failed=0
for pid in "${pids[@]}"; do
  wait "$pid" || failed=1
done
if [ "$failed" -ne 0 ]; then
  echo "null control failed; logs: $output" >&2
  exit 1
fi

for item in "${active_vms[@]}"; do
  IFS=: read -r vm index count <<< "$item"
  result="$output/$vm.json"
  gcloud compute scp "$vm:null-${label}-shard${index}.json" "$result" \
    --project="$project" --zone="$zone" --quiet
  jq -e --argjson expected "$count" \
    '.completed == $expected
     and .passingWithoutAction == 0
     and .errors == 0
     and ([.results[].score] | all(. == 0))' "$result" >/dev/null
  echo "$vm null control PASS ($count tasks)"
done

echo "Null-control artifacts: $output"
