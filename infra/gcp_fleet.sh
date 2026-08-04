#!/usr/bin/env bash
# Safe lifecycle helpers for warm GCE hosts. Normal commands never stop or
# delete an outer VM. Each benchmark episode still owns an isolated nested VM.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
project="${GHOST_OSWORLD_GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
zone="${GHOST_OSWORLD_GCP_ZONE:-us-central1-a}"
machine="${GHOST_OSWORLD_GCP_MACHINE:-n2-standard-32}"
disk_size="${GHOST_OSWORLD_GCP_DISK_SIZE:-400GB}"
if [ -n "${GHOST_OSWORLD_GCP_FLEET:-}" ]; then
  IFS=',' read -r -a fleet <<< "$GHOST_OSWORLD_GCP_FLEET"
else
  fleet=(semantic-os-1)
fi
action="${1:-status}"

case "$project" in ""|"(unset)")
  echo "Set GHOST_OSWORLD_GCP_PROJECT or select a gcloud project." >&2
  exit 2
esac
test "${#fleet[@]}" -gt 0

gc() { gcloud "$@" --project="$project" --quiet; }
exists() { gc compute instances describe "$1" --zone="$zone" >/dev/null 2>&1; }

case "$action" in
  create)
    for vm in "${fleet[@]}"; do
      [[ "$vm" =~ ^[a-z][a-z0-9-]{0,61}[a-z0-9]$ ]] || {
        echo "Invalid GCE instance name: $vm" >&2; exit 2;
      }
      if exists "$vm"; then echo "$vm already exists"; continue; fi
      gc compute instances create "$vm" \
        --zone="$zone" \
        --machine-type="$machine" \
        --enable-nested-virtualization \
        --image-family=ubuntu-2204-lts \
        --image-project=ubuntu-os-cloud \
        --boot-disk-size="$disk_size" \
        --boot-disk-type=pd-ssd \
        --labels=purpose=ghost-semantic-os \
        --format='value(name,status)'
    done
    ;;

  sync)
    test -d "$repo_dir/.git"
    test -d "$repo_dir/OSWorld/.git"
    "$repo_dir/scripts/doctor.sh" --source-only >/dev/null
    bundle="$(mktemp /tmp/ghost-semantic-os-bundle.XXXXXX)"
    state_file="$(mktemp /tmp/ghost-semantic-os-state.XXXXXX)"
    trap 'rm -f "$bundle" "$state_file"' EXIT
    tar -C "$repo_dir" -czf "$bundle" \
      --exclude=.git --exclude=OSWorld --exclude=node_modules \
      --exclude=.venv --exclude=.ci-venv \
      --exclude=results --exclude=results_gcp --exclude=results_vm \
      --exclude='*.pyc' --exclude=__pycache__ --exclude=.env \
      --exclude=.DS_Store .
    commit="$(git -C "$repo_dir" rev-parse HEAD)"
    osworld_commit="$(git -C "$repo_dir/OSWorld" rev-parse HEAD)"
    osworld_tree="$(git -C "$repo_dir/OSWorld" rev-parse 'HEAD^{tree}')"
    runtime_sha="$(bash "$repo_dir/infra/compute_semantic_runtime_hash.sh")"
    printf 'commit=%s\nosworld_commit=%s\nosworld_tree=%s\nruntime_sha256=%s\n' \
      "$commit" "$osworld_commit" "$osworld_tree" "$runtime_sha" > "$state_file"

    for vm in "${fleet[@]}"; do
      exists "$vm" || { echo "$vm does not exist" >&2; exit 1; }
      gc compute scp "$bundle" "$state_file" "$vm:/tmp/" --zone="$zone"
      gc compute ssh "$vm" --zone="$zone" --command \
        "run_pid=\$(cat \"\$HOME/run.pid\" 2>/dev/null || echo 0); \
         if [[ \"\$run_pid\" =~ ^[1-9][0-9]*\$ ]] && kill -0 \"\$run_pid\" 2>/dev/null; then \
           echo 'refusing sync while scored run is active' >&2; exit 1; fi; \
         stage=\"\$HOME/ghost-semantic-os.next.\$\$\"; \
         mkdir -p \"\$stage\"; \
         tar -xzf '/tmp/$(basename "$bundle")' -C \"\$stage\"; \
         install -m 644 '/tmp/$(basename "$state_file")' \"\$stage/.source_state\"; \
         if [ -d \"\$HOME/ghost-semantic-os\" ]; then \
           mv \"\$HOME/ghost-semantic-os\" \"\$HOME/ghost-semantic-os.backup.\$(date +%s)\"; fi; \
         mv \"\$stage\" \"\$HOME/ghost-semantic-os\""
      echo "$vm synced runtime_sha256=$runtime_sha"
    done
    ;;

  setup)
    for vm in "${fleet[@]}"; do
      exists "$vm" || { echo "$vm does not exist" >&2; exit 1; }
      gc compute ssh "$vm" --zone="$zone" --command \
        'cd "$HOME/ghost-semantic-os" || exit 1
         setsid bash infra/gcp_setup.sh > "$HOME/semantic-os-setup.log" 2>&1 < /dev/null &
         echo $! > "$HOME/semantic-os-setup.pid"'
      echo "$vm setup started"
    done
    ;;

  warm)
    for vm in "${fleet[@]}"; do
      exists "$vm" || { echo "$vm does not exist" >&2; exit 1; }
      gc compute ssh "$vm" --zone="$zone" --command \
        'cd "$HOME/ghost-semantic-os" || exit 1
         . .venv/bin/activate || exit 1
         setsid python infra/gcp_warm.py > "$HOME/semantic-os-warm.log" 2>&1 < /dev/null &
         echo $! > "$HOME/semantic-os-warm.pid"'
      echo "$vm cache warm started"
    done
    ;;

  status)
    for vm in "${fleet[@]}"; do
      if ! exists "$vm"; then echo "$vm: absent"; continue; fi
      remote="$(gc compute ssh "$vm" --zone="$zone" --command \
        'setup=none; warm=none; run=none
         test -f "$HOME/semantic-os-setup.log" && setup=$(tail -1 "$HOME/semantic-os-setup.log" | cut -c1-70)
         test -f "$HOME/semantic-os-warm.log" && warm=$(tail -1 "$HOME/semantic-os-warm.log" | cut -c1-70)
         if test -f "$HOME/run.log"; then
           scored=$(grep -c "=> score" "$HOME/run.log" || true)
           expected=$(cat "$HOME/run.expected" 2>/dev/null || echo "?")
           pid=$(cat "$HOME/run.pid" 2>/dev/null || echo 0)
           if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" 2>/dev/null; then state=running; else state=stopped; fi
           run="$scored/$expected scored ($state)"
         fi
         echo "setup=$setup | warm=$warm | run=$run"' 2>/dev/null || true)"
      echo "$vm: ${remote:-SSH unavailable}"
    done
    ;;

  *)
    echo "Usage: $0 create|sync|setup|warm|status" >&2
    echo "Outer VM deletion is intentionally not part of this launcher." >&2
    exit 2
    ;;
esac
