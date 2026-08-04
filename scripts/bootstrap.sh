#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_only=0
case "${1:-}" in
  --source-only) source_only=1 ;;
  "") ;;
  *) echo "Usage: $0 [--source-only]" >&2; exit 2 ;;
esac

"$root/scripts/bootstrap-osworld.sh"

command -v node >/dev/null || {
  echo "Node.js 22+ is required. See docs/installation.md." >&2
  exit 1
}
node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
[ "$node_major" -ge 22 ] || {
  echo "Node.js 22+ is required; found $(node --version)." >&2
  exit 1
}
(cd "$root/harness" && npm ci --silent)

if [ "$source_only" -eq 1 ]; then
  echo "Source bootstrap complete (OSWorld + Node dependencies)."
  exit 0
fi

if [ "$(uname -s)" != Linux ]; then
  echo "Full host installation requires Ubuntu Linux."
  echo "Use --source-only here, or clone this repository inside the target VM." >&2
  exit 1
fi
exec "$root/scripts/install-host.sh"
