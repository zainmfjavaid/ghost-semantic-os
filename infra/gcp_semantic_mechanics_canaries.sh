#!/usr/bin/env bash
# Run the focused semantic-v1 mechanics gate in parallel on warm outer hosts.
# This script may restart an idle benchmark server when its runtime hash differs,
# but it never stops or deletes an outer GCE VM.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="${GHOST_OSWORLD_GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
zone="${GHOST_OSWORLD_GCP_ZONE:-us-central1-a}"
runtime_hash="${1:-$(bash "$repo_dir/infra/compute_semantic_runtime_hash.sh")}"
[[ "$runtime_hash" =~ ^[0-9a-f]{64}$ ]]
case "$project" in ""|"(unset)")
  echo "Set GHOST_OSWORLD_GCP_PROJECT or select a gcloud project." >&2
  exit 2
esac

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
output_dir="$repo_dir/results_gcp/semantic-mechanics-canaries/${runtime_hash:0:12}-$timestamp"
mkdir -p "$output_dir"

test -n "${GHOST_OSWORLD_GCP_FLEET:-}" || {
  echo "Set GHOST_OSWORLD_GCP_FLEET to five comma-separated prepared hosts." >&2
  exit 2
}
IFS=',' read -r -a hosts <<< "$GHOST_OSWORLD_GCP_FLEET"
test "${#hosts[@]}" -eq 5 || {
  echo "The mechanics suite requires exactly five hosts." >&2
  exit 2
}
suites=(
  research
  office
  calc-artifact
  packages
  cross-app
)
tasks=(
  67890eb6-6ce5-4c00-9e3d-fb4972699b06
  236833a3-5704-47fc-888c-4f298f09f799
  67890eb6-6ce5-4c00-9e3d-fb4972699b06
  e1fc0df3-c8b9-4ee7-864c-d0b590d3aa56
  58565672-7bfe-48ab-b828-db349231de6b
)

pids=()
for index in "${!hosts[@]}"; do
  host="${hosts[$index]}"
  suite="${suites[$index]}"
  task="${tasks[$index]}"
  log="$output_dir/$host-$suite.log"
  remote_task="\$HOME/ghost-semantic-os/OSWorld/evaluation_examples/examples/multi_apps/$task.json"
  {
    gcloud compute ssh "$host" \
      --project="$project" \
      --zone="$zone" \
      --quiet \
      --command="cd \"\$HOME/ghost-semantic-os\" && \
        bash infra/gcp_remote_run.sh --ensure-semantic-server '$runtime_hash' && \
        . .venv/bin/activate && \
        python infra/gcp_semantic_canary.py \
          --task \"$remote_task\" --suite '$suite'"
  } >"$log" 2>&1 &
  pids+=("$!")
  echo "CANARY_STARTED host=$host suite=$suite log=$log"
done

failures=0
for index in "${!pids[@]}"; do
  host="${hosts[$index]}"
  suite="${suites[$index]}"
  log="$output_dir/$host-$suite.log"
  if wait "${pids[$index]}"; then
    echo "CANARY_PASSED host=$host suite=$suite"
    tail -1 "$log"
  else
    failures=$((failures + 1))
    echo "CANARY_FAILED host=$host suite=$suite log=$log" >&2
    tail -80 "$log" >&2 || true
  fi
done

if [ "$failures" -ne 0 ]; then
  echo "SEMANTIC_MECHANICS_GATE_FAILED failures=$failures output=$output_dir" >&2
  exit 1
fi

echo "SEMANTIC_MECHANICS_GATE_PASSED runtime_sha256=$runtime_hash output=$output_dir"
