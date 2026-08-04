#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker_image="${GHOST_OSWORLD_DOCKER_IMAGE:-happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9}"

[ "$(uname -s)" = Linux ] || { echo "Ubuntu Linux is required." >&2; exit 1; }
command -v apt-get >/dev/null || { echo "apt-get is required (Ubuntu 22.04 is the tested host)." >&2; exit 1; }
test -e /dev/kvm || {
  echo "/dev/kvm is missing. Enable nested virtualization on the VM host." >&2
  exit 1
}

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  docker.io python3-pip python3-venv git curl ca-certificates build-essential \
  bubblewrap poppler-utils jq rsync >/dev/null
sudo usermod -aG docker "$USER"

if ! command -v node >/dev/null || [ "$(node -p 'Number(process.versions.node.split(".")[0])' 2>/dev/null || echo 0)" -lt 22 ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - >/dev/null
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nodejs >/dev/null
fi

python3 -m venv "$root/.venv"
"$root/.venv/bin/python" -m pip install --quiet --upgrade pip
"$root/.venv/bin/python" -m pip install --quiet -r "$root/requirements/host.lock"
(cd "$root/harness" && npm ci --silent)

docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then docker_cmd=(sudo docker); fi
"${docker_cmd[@]}" pull "$docker_image"
"${docker_cmd[@]}" tag "$docker_image" happysixd/osworld-docker:latest

"$root/.venv/bin/python" -m pip freeze > "$root/.python-freeze.txt"
docker_identity="$("${docker_cmd[@]}" image inspect "$docker_image" --format '{{index .RepoDigests 0}}' 2>/dev/null || true)"
cat > "$root/.environment_state" <<EOF
docker_image=$docker_identity
node_version=$(node --version)
python_version=$("$root/.venv/bin/python" --version 2>&1)
python_freeze_sha256=$(sha256sum "$root/.python-freeze.txt" | awk '{print $1}')
package_lock_sha256=$(sha256sum "$root/harness/package-lock.json" | awk '{print $1}')
osworld_tree=$(git -C "$root/OSWorld" rev-parse 'HEAD^{tree}')
EOF

echo "Host installation complete."
if ! docker info >/dev/null 2>&1; then
  echo "Log out and back in once so Docker group membership takes effect."
fi
cat "$root/.environment_state"
