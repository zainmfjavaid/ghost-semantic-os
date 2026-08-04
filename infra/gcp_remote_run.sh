#!/usr/bin/env bash
# Launch one scored arm on an already-warm GCP host. This script never changes
# the outer GCE lifecycle and preserves a healthy matching benchmark server.
set -euo pipefail

controller_repo="${HOME}/ghost-semantic-os"
repo_dir="$controller_repo"
server_url="http://127.0.0.1:8079"
runtime_hash_helper="$controller_repo/infra/compute_semantic_runtime_hash.sh"
scoring_lock="$HOME/osworld-scored-run.lock"
exact_osworld_image="${GHOST_OSWORLD_DOCKER_IMAGE:-happysixd/osworld-docker}"
orphan_ttl_seconds="${GHOST_OSWORLD_ORPHAN_TTL_SECONDS:-21600}"

[[ "$orphan_ttl_seconds" =~ ^[1-9][0-9]*$ ]]

scored_run_active() {
  local pid command state
  pid="$(cat "$HOME/run.pid" 2>/dev/null || echo 0)"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  state="$(awk '{print $3}' "/proc/$pid/stat" 2>/dev/null || true)"
  [ "$state" != "Z" ] || return 1
  command="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ "$command" == *"tsx src/cli.ts"* ]]
}

acquire_scoring_lease() {
  if [ "${OSWORLD_SCORING_LEASE_HELD:-0}" = 1 ]; then
    return
  fi
  exec 9>"$scoring_lock"
  if ! flock -n 9; then
    echo "SCORED_RUN_LEASE_ACTIVE host=$(hostname)" >&2
    exit 1
  fi
}

read_health() {
  curl -fsS --max-time 5 "$server_url/health"
}

health_field() {
  local health_json="$1" field="$2"
  python3 - "$field" "$health_json" <<'PY'
import json
import sys

field, raw = sys.argv[1:]
value = json.loads(raw).get(field)
if isinstance(value, (dict, list)):
    print(json.dumps(value, separators=(",", ":")))
elif value is not None:
    print(value)
PY
}

drain_registered_episodes() {
  local health_json episode_id remaining
  health_json="$(read_health)" || {
    echo "SERVER_DRAIN_UNAVAILABLE health endpoint failed" >&2
    return 1
  }
  mapfile -t episode_ids < <(
    python3 - "$health_json" <<'PY'
import json
import sys

for episode_id in json.loads(sys.argv[1]).get("episodes", []):
    print(episode_id)
PY
  )
  for episode_id in "${episode_ids[@]}"; do
    curl -fsS --max-time 180 -X DELETE \
      "$server_url/episodes/$episode_id" >/dev/null || {
        echo "EPISODE_DRAIN_FAILED episode=$episode_id" >&2
        return 1
      }
  done
  for _ in $(seq 1 120); do
    health_json="$(read_health)" || return 1
    remaining="$(python3 - "$health_json" <<'PY'
import json
import sys
print(len(json.loads(sys.argv[1]).get("episodes", [])))
PY
)"
    if [ "$remaining" -eq 0 ]; then
      [ "${#episode_ids[@]}" -eq 0 ] || \
        echo "EPISODES_DRAINED count=${#episode_ids[@]}"
      return
    fi
    sleep 1
  done
  echo "EPISODE_DRAIN_TIMEOUT" >&2
  return 1
}

stop_benchmark_server() {
  local pid command listener_pid
  pid="$(cat "$HOME/srv.pid" 2>/dev/null || echo 0)"
  if ! [[ "$pid" =~ ^[1-9][0-9]*$ ]] || ! kill -0 "$pid" 2>/dev/null; then
    listener_pid="$(
      ss -ltnp 'sport = :8079' 2>/dev/null \
        | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | sort -u
    )"
    if ! [[ "$listener_pid" =~ ^[1-9][0-9]*$ ]]; then
      echo "SERVER_PID_MISSING refusing to kill an unresolved listener" >&2
      return 1
    fi
    pid="$listener_pid"
  fi
  command="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ "$command" != *"infra.gcp_bench_server"* \
    && "$command" != *"infra/gcp_bench_server.py"* ]]
  then
    echo "SERVER_PID_UNOWNED pid=$pid command=$command" >&2
    return 1
  fi
  kill "$pid"
  for _ in $(seq 1 80); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid"
  fi
  rm -f "$HOME/srv.pid" "$HOME/srv.runtime_sha256" \
    "$HOME/srv.runtime_repo" "$HOME/srv.runtime_name"
}

start_benchmark_server() {
  local runtime_hash="$1" runtime_repo="$2" runtime_name="$3"
  local server_ok=0 health_json guest_image_digest outer_vm_name
  cd "$controller_repo"
  . "$controller_repo/.venv/bin/activate"
  guest_image_digest="$(
    docker image inspect --format '{{index .RepoDigests 0}}' "$exact_osworld_image" \
      2>/dev/null || true
  )"
  if [ -z "$guest_image_digest" ] || [ "$guest_image_digest" = "<no value>" ]; then
    guest_image_digest="$(
      docker image inspect --format '{{.Id}}' "$exact_osworld_image" 2>/dev/null || true
    )"
  fi
  if [ -z "$guest_image_digest" ]; then
    echo "GUEST_IMAGE_IDENTITY_MISSING image=$exact_osworld_image" >&2
    return 1
  fi
  outer_vm_name="$(hostname)"
  GHOST_OSWORLD_RUNTIME_REPO="$runtime_repo" \
  OSWORLD_SERVER_RUNTIME_HASH="$runtime_hash" \
  OSWORLD_OUTER_PROVIDER="gcp" \
  OSWORLD_OUTER_VM_NAME="$outer_vm_name" \
  OSWORLD_GUEST_IMAGE_DIGEST="$guest_image_digest" \
    setsid python "$controller_repo/infra/gcp_bench_server.py" \
      > "$HOME/srv.log" 2>&1 < /dev/null 9>&- &
  echo "$!" > "$HOME/srv.pid"
  for _ in $(seq 1 60); do
    if health_json="$(read_health)"; then
      server_ok=1
      break
    fi
    sleep 2
  done
  if [ "$server_ok" -ne 1 ]; then
    echo "SERVER_FAILED" >&2
    tail -30 "$HOME/srv.log" >&2 || true
    return 1
  fi
  printf '%s\n' "$runtime_hash" > "$HOME/srv.runtime_sha256"
  printf '%s\n' "$runtime_repo" > "$HOME/srv.runtime_repo"
  printf '%s\n' "$runtime_name" > "$HOME/srv.runtime_name"
}

cleanup_expired_orphans() {
  local health_json live_count now container_id image created created_epoch age cleaned=0
  health_json="$(read_health)" || return 0
  live_count="$(python3 - "$health_json" <<'PY'
import json
import sys
print(len(json.loads(sys.argv[1]).get("episodes", [])))
PY
)"
  if [ "$live_count" -ne 0 ]; then
    echo "ORPHAN_CLEANUP_SKIPPED live_registered_episodes=$live_count"
    return
  fi
  now="$(date +%s)"
  mapfile -t container_ids < <(docker ps -aq)
  for container_id in "${container_ids[@]}"; do
    image="$(docker inspect --format '{{.Config.Image}}' "$container_id" 2>/dev/null || true)"
    [ "$image" = "$exact_osworld_image" ] || continue
    created="$(docker inspect --format '{{.Created}}' "$container_id" 2>/dev/null || true)"
    created_epoch="$(date -d "$created" +%s 2>/dev/null || true)"
    [[ "$created_epoch" =~ ^[0-9]+$ ]] || continue
    age=$((now - created_epoch))
    [ "$age" -ge "$orphan_ttl_seconds" ] || continue
    # Re-check immediately before deletion. A new registered episode means the
    # host is no longer in an orphan-safe state, even if the earlier snapshot
    # was empty.
    health_json="$(read_health)" || return 0
    live_count="$(python3 - "$health_json" <<'PY'
import json
import sys
print(len(json.loads(sys.argv[1]).get("episodes", [])))
PY
)"
    if [ "$live_count" -ne 0 ]; then
      echo "ORPHAN_CLEANUP_STOPPED live_registered_episodes=$live_count"
      return
    fi
    docker stop --time 20 "$container_id" >/dev/null 2>&1 || true
    docker rm "$container_id" >/dev/null 2>&1 || true
    cleaned=$((cleaned + 1))
  done
  [ "$cleaned" -eq 0 ] || \
    echo "EXPIRED_OSWORLD_ORPHANS_CLEANED count=$cleaned ttl_seconds=$orphan_ttl_seconds"
}

ensure_runtime_server() {
  local runtime_name="$1" expected_hash="${2:-}" runtime_repo runtime_hash
  local health_json observed_hash observed_repo observed_name protocol pid command
  case "$runtime_name" in
    semantic-v1|semantic-plus-v1|semantic-simple-v1)
      runtime_repo="$controller_repo"
      runtime_hash="$(bash "$controller_repo/infra/compute_semantic_runtime_hash.sh")"
      ;;
    *)
      echo "UNKNOWN_RUNTIME $runtime_name" >&2
      return 1
      ;;
  esac
  if [ -n "$expected_hash" ] && [ "$runtime_hash" != "$expected_hash" ]; then
    echo "REMOTE_RUNTIME_HASH_MISMATCH expected=$expected_hash actual=$runtime_hash" >&2
    return 1
  fi

  if health_json="$(read_health)"; then
    observed_hash="$(cat "$HOME/srv.runtime_sha256" 2>/dev/null || true)"
    observed_repo="$(cat "$HOME/srv.runtime_repo" 2>/dev/null || true)"
    observed_name="$(cat "$HOME/srv.runtime_name" 2>/dev/null || true)"
    protocol="$(health_field "$health_json" semantic_protocol_version)"
    if [ "$observed_hash" = "$runtime_hash" ] \
      && [ "$observed_repo" = "$runtime_repo" ] \
      && [ "$observed_name" = "$runtime_name" ] \
      && { [[ "$runtime_name" != semantic-* ]] || [ "$protocol" = "1.0" ]; }
    then
      drain_registered_episodes
      cleanup_expired_orphans
      echo "SERVER_REUSED runtime_sha256=$runtime_hash"
      echo "SERVER_OK"
      return
    fi
    echo "SERVER_RUNTIME_MISMATCH running=$observed_hash required=$runtime_hash"
    # The registry must be empty before the owning server process is touched.
    drain_registered_episodes
    stop_benchmark_server
    start_benchmark_server "$runtime_hash" "$runtime_repo" "$runtime_name"
    cleanup_expired_orphans
    echo "SERVER_RESTARTED runtime_sha256=$runtime_hash"
    echo "SERVER_OK"
    return
  fi

  pid="$(cat "$HOME/srv.pid" 2>/dev/null || echo 0)"
  if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null; then
    command="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    echo "SERVER_UNHEALTHY_REFUSING_RESTART pid=$pid command=$command" >&2
    echo "Cannot prove the episode registry is drained; manual recovery is required." >&2
    return 1
  fi
  start_benchmark_server "$runtime_hash" "$runtime_repo" "$runtime_name"
  cleanup_expired_orphans
  echo "SERVER_STARTED runtime_sha256=$runtime_hash"
  echo "SERVER_OK"
}

if [ "${1:-}" = "--ensure-semantic-server" ]; then
  expected_runtime_hash="${2:?expected semantic runtime hash required}"
  acquire_scoring_lease
  if scored_run_active; then
    echo "RUN_ALREADY_ACTIVE pid=$(cat "$HOME/run.pid")" >&2
    exit 1
  fi
  ensure_runtime_server semantic-v1 "$expected_runtime_hash"
  exit
fi

if [ "${1:-}" = "--ensure-runtime-server" ]; then
  requested_runtime="${2:?runtime name required}"
  expected_runtime_hash="${3:?expected runtime hash required}"
  acquire_scoring_lease
  if scored_run_active; then
    echo "RUN_ALREADY_ACTIVE pid=$(cat "$HOME/run.pid")" >&2
    exit 1
  fi
  ensure_runtime_server "$requested_runtime" "$expected_runtime_hash"
  exit
fi

shard="${1:?shard path required}"
label="${2:?label required}"
max_tools="${3:-40}"
concurrency="${4:-2}"
model="${5:-qwen/qwen3.6-27b}"
thinking="${6:-medium}"
variant="${7:-semantic_runtime_v1}"
pool_sha="${8:?source pool SHA-256 required}"
expected_shard_sha="${9:?shard SHA-256 required}"
mode="${10:-semantic}"

[[ "$label" =~ ^[A-Za-z0-9._-]+$ ]]
[[ "$max_tools" =~ ^[0-9]+$ ]]
[[ "$concurrency" =~ ^[1-9][0-9]*$ ]]
[[ "$model" =~ ^[A-Za-z0-9._:/-]+$ ]]
[[ "$thinking" =~ ^(off|low|medium|high)$ ]]
[[ "$variant" =~ ^[A-Za-z0-9._-]+$ ]]
[[ "$pool_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$expected_shard_sha" =~ ^[0-9a-f]{64}$ ]]
[[ "$mode" =~ ^(semantic|semantic-plus|semantic-simple)$ ]]

case "$mode" in
  semantic)
    runtime_name=semantic-v1
    runtime_repo="$controller_repo"
    runtime_hash_helper="$controller_repo/infra/compute_semantic_runtime_hash.sh"
    parent_commit="$(awk -F= '$1 == "commit" {print $2}' "$controller_repo/.source_state" 2>/dev/null || true)"
    nested_commit="$(awk -F= '$1 == "osworld_commit" {print $2}' "$controller_repo/.source_state" 2>/dev/null || true)"
    ;;
  semantic-plus)
    runtime_name=semantic-plus-v1
    runtime_repo="$controller_repo"
    runtime_hash_helper="$controller_repo/infra/compute_semantic_runtime_hash.sh"
    parent_commit="$(awk -F= '$1 == "commit" {print $2}' "$controller_repo/.source_state" 2>/dev/null || true)"
    nested_commit="$(awk -F= '$1 == "osworld_commit" {print $2}' "$controller_repo/.source_state" 2>/dev/null || true)"
    ;;
  semantic-simple)
    runtime_name=semantic-simple-v1
    runtime_repo="$controller_repo"
    runtime_hash_helper="$controller_repo/infra/compute_semantic_runtime_hash.sh"
    parent_commit="$(awk -F= '$1 == "commit" {print $2}' "$controller_repo/.source_state" 2>/dev/null || true)"
    nested_commit="$(awk -F= '$1 == "osworld_commit" {print $2}' "$controller_repo/.source_state" 2>/dev/null || true)"
    ;;
esac
test -d "$runtime_repo"

acquire_scoring_lease
if scored_run_active; then
  echo "RUN_ALREADY_ACTIVE pid=$(cat "$HOME/run.pid")" >&2
  exit 1
fi
rm -f "$HOME/run.pid"

cd "$controller_repo"
test -f "$shard"
shard="$(cd "$(dirname "$shard")" && pwd)/$(basename "$shard")"
actual_shard_sha="$(sha256sum "$shard" | awk '{print $1}')"
if [ "$actual_shard_sha" != "$expected_shard_sha" ]; then
  echo "SHARD_HASH_MISMATCH expected=$expected_shard_sha actual=$actual_shard_sha" >&2
  exit 1
fi
expected="$(python3 - "$shard" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    tasks = json.load(handle)
if not isinstance(tasks, list) or not tasks:
    raise SystemExit("shard must be a non-empty JSON array")
print(len(tasks))
PY
)"
test -s "$HOME/.openrouter_key"

ensure_runtime_server "$runtime_name" "$(bash "$runtime_hash_helper")"

source_state="$controller_repo/.source_state"
export PARENT_COMMIT NESTED_OSWORLD_COMMIT TASK_POOL_SHA256 SEMANTIC_SERVER_RUNTIME_SHA256
PARENT_COMMIT="$parent_commit"
NESTED_OSWORLD_COMMIT="$nested_commit"
TASK_POOL_SHA256="$pool_sha"
SEMANTIC_SERVER_RUNTIME_SHA256="$(bash "$runtime_hash_helper")"
runtime_manifest="$HOME/runtime-manifest-$label.json"
model_config="$HOME/model-config-$label.json"
model_input='["text"]'
jq -n \
  --arg provider openrouter \
  --arg model "$model" \
  --arg thinking "$thinking" \
  --argjson input "$model_input" \
  '{provider:$provider,model:$model,input:$input,thinking:$thinking,endpoint:"https://openrouter.ai/api/v1"}' \
  > "$model_config"
manifest_args=(
  --runtime "$runtime_name"
  --source-root "$runtime_repo"
  --environment-root "$controller_repo"
  --parent-commit "$parent_commit"
  --nested-osworld-commit "$nested_commit"
  --task-pool-sha256 "$pool_sha"
  --task-pool-name distributed-frozen-pool
  --model-config "$model_config"
  --output "$runtime_manifest"
)
python3 "$controller_repo/infra/build_semantic_runtime_manifest.py" "${manifest_args[@]}"
export RUNTIME_MANIFEST_SHA256
RUNTIME_MANIFEST_SHA256="$(jq -r .runtime_manifest_sha256 "$runtime_manifest")"

cd "$runtime_repo/harness"
OPENROUTER_API_KEY="$(cat "$HOME/.openrouter_key")"
rm -f "$HOME/.openrouter_key"
export OPENROUTER_API_KEY
export HARNESS_REVISION
HARNESS_REVISION="$(
  {
    cat "$controller_repo/.source_state" 2>/dev/null || echo source=unknown
    cat "$controller_repo/.environment_state" 2>/dev/null || echo environment=unknown
    printf 'semantic_server_runtime_sha256=%s\n' "$(bash "$runtime_hash_helper")"
    printf 'runtime_manifest_sha256=%s\n' "$RUNTIME_MANIFEST_SHA256"
  }
)"
case "$mode" in
  semantic)
    runtime_flags=(--runtime semantic-v1 --model-input text)
    ;;
  semantic-plus)
    runtime_flags=(--runtime semantic-plus-v1 --model-input text)
    ;;
  semantic-simple)
    runtime_flags=(--runtime semantic-simple-v1 --model-input text)
    ;;
esac
setsid ./node_modules/.bin/tsx src/cli.ts \
  --env-url "$server_url" \
  --tasks-file "$shard" \
  --provider openrouter \
  --model "$model" \
  --thinking "$thinking" \
  --max-tool-calls "$max_tools" \
  --concurrency "$concurrency" \
  "${runtime_flags[@]}" \
  --variant "$variant" \
  --label "$label" \
  --output-root "$controller_repo/results_vm" \
  > "$HOME/run.log" 2>&1 < /dev/null &
run_pid="$!"
echo "$run_pid" > "$HOME/run.pid"
printf '%s\n' "$label" > "$HOME/run.label"
printf '%s\n' "$expected" > "$HOME/run.expected"
printf '%s\n' "$pool_sha" > "$HOME/run.pool_sha"
printf '%s\n' "$expected_shard_sha" > "$HOME/run.shard_sha"
printf '%s\n' "$shard" > "$HOME/run.shard_path"

sleep 8
if ! kill -0 "$run_pid" 2>/dev/null; then
  echo "HARNESS_DIED"
  tail -40 "$HOME/run.log"
  exit 1
fi
log_bytes="$(wc -c < "$HOME/run.log")"
if [ "$log_bytes" -lt 20 ]; then
  echo "HARNESS_NO_LOG_GROWTH bytes=$log_bytes"
  exit 1
fi
result_dir=""
for _ in $(seq 1 20); do
  result_dir="$(
    find "$controller_repo/results_vm" -mindepth 1 -maxdepth 1 -type d \
      -name "*_${label}" -print | sort | tail -1
  )"
  [ -n "$result_dir" ] && break
  sleep 1
done
if [ -z "$result_dir" ]; then
  echo "HARNESS_RESULT_DIR_MISSING label=$label"
  exit 1
fi
printf '%s\n' "$result_dir" > "$HOME/run.result_dir"
if [ -f "$runtime_manifest" ]; then
  install -m 644 "$runtime_manifest" "$result_dir/runtime-manifest.json"
fi
if [ -f "$model_config" ]; then
  install -m 644 "$model_config" "$result_dir/model-endpoint-config.json"
fi
echo "LAUNCH_VERIFIED pid=$run_pid bytes=$log_bytes expected=$expected model=$model result=$result_dir"
