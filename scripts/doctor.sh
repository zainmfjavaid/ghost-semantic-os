#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_only=0
json=0
for arg in "$@"; do
  case "$arg" in
    --source-only) source_only=1 ;;
    --json) json=1 ;;
    *) echo "Usage: $0 [--source-only] [--json]" >&2; exit 2 ;;
  esac
done

failures=()
passes=()
failure_count=0
check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    passes+=("$name")
  else
    failures+=("$name")
    failure_count=$((failure_count + 1))
  fi
}

check git command -v git
check node_22 bash -c 'command -v node && test "$(node -p '\''Number(process.versions.node.split(".")[0])'\'')" -ge 22'
check npm_lock test -f "$root/harness/package-lock.json"
check node_modules test -d "$root/harness/node_modules"
check osworld_checkout test -d "$root/OSWorld/.git"
check osworld_tree bash -c 'test "$(git -C "$1" rev-parse "HEAD^{tree}" 2>/dev/null)" = 76331b18181423ee60ec613c1475b2d8300b7b03' _ "$root/OSWorld"
check protocol_schema test -f "$root/protocol/semantic-v1.schema.json"

if [ "$source_only" -eq 0 ]; then
  check linux bash -c 'test "$(uname -s)" = Linux'
  check kvm test -e /dev/kvm
  check docker command -v docker
  check docker_access docker info
  check python_venv test -x "$root/.venv/bin/python"
  check python_imports "$root/.venv/bin/python" -c 'import fastapi,uvicorn,docker,playwright,jsonschema'
  check osworld_image docker image inspect 'happysixd/osworld-docker:latest'
fi

if [ "$json" -eq 1 ]; then
  ROOT="$root" SOURCE_ONLY="$source_only" PASSES="$(IFS=,; echo "${passes[*]-}")" FAILURES="$(IFS=,; echo "${failures[*]-}")" python3 - <<'PY'
import json, os
print(json.dumps({
  "ok": not bool(os.environ.get("FAILURES")),
  "mode": "source" if os.environ["SOURCE_ONLY"] == "1" else "host",
  "root": os.environ["ROOT"],
  "passes": [x for x in os.environ.get("PASSES", "").split(",") if x],
  "failures": [x for x in os.environ.get("FAILURES", "").split(",") if x],
}, indent=2))
PY
else
  for item in "${passes[@]}"; do printf 'PASS  %s\n' "$item"; done
  if [ "$failure_count" -gt 0 ]; then
    for item in "${failures[@]}"; do printf 'FAIL  %s\n' "$item"; done
  fi
fi

[ "$failure_count" -eq 0 ]
