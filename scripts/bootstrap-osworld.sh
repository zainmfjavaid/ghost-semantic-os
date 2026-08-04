#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
destination="${OSWORLD_DIR:-$root/OSWorld}"
repository="${OSWORLD_REPOSITORY:-https://github.com/xlang-ai/OSWorld.git}"
base_commit="fad6d07f0a3ad456e7d966dcc98a7fee2491afe0"
base_tree="243a6c338443da81c973bf280d033e5f76c08e80"
final_commit="d3781e929734efdc877fa6bfc5370e669570914c"
final_tree="76331b18181423ee60ec613c1475b2d8300b7b03"
patch_dir="$root/patches/osworld"
base_archive="${OSWORLD_BASE_ARCHIVE:-$patch_dir/osworld-base-fad6d07f.tar.gz}"
base_archive_sha="f6a10757019a7cf93bdf06960b488a77e011e3b03a064152504ffbaf2bdb1176"

if [ -e "$destination" ] && [ ! -d "$destination/.git" ]; then
  echo "Refusing to replace non-Git path: $destination" >&2
  exit 1
fi

created_checkout=0
if [ ! -d "$destination/.git" ]; then
  test -f "$base_archive"
  if command -v sha256sum >/dev/null 2>&1; then
    observed_archive_sha="$(sha256sum "$base_archive" | awk '{print $1}')"
  else
    observed_archive_sha="$(shasum -a 256 "$base_archive" | awk '{print $1}')"
  fi
  test "$observed_archive_sha" = "$base_archive_sha" || {
    echo "OSWorld base archive hash mismatch." >&2; exit 1;
  }
  mkdir -p "$destination"
  tar -xzf "$base_archive" --strip-components=1 -C "$destination"
  git -C "$destination" init --quiet
  # The snapshot contains only files tracked by the frozen upstream commit.
  # Force-add them because upstream intentionally tracks a handful of paths
  # that its own .gitignore also matches.
  git -C "$destination" add --force --all
  # git archive represents submodules as empty directories. Restore the two
  # frozen gitlink entries so the reconstructed tree is byte-for-byte equal
  # to the upstream base tree before applying our patch series.
  git -C "$destination" update-index --add --cacheinfo \
    160000,d61d5ae21880f4f8025099223fcb1ff415e3d634,mm_agents/surferH/agp_client
  git -C "$destination" update-index --add --cacheinfo \
    160000,dd19c27b68227bbe44f69688fe433c01028b0cb3,mm_agents/surferH/rdds
  observed_base_tree="$(git -C "$destination" write-tree)"
  test "$observed_base_tree" = "$base_tree" || {
    echo "OSWorld base tree mismatch: expected=$base_tree actual=$observed_base_tree" >&2
    exit 1
  }
  GIT_AUTHOR_NAME=zainmfjavaid GIT_AUTHOR_EMAIL=zainmfj@gmail.com \
  GIT_AUTHOR_DATE='2026-08-02T12:42:55-07:00' \
  GIT_COMMITTER_NAME=zainmfjavaid GIT_COMMITTER_EMAIL=zainmfj@gmail.com \
  GIT_COMMITTER_DATE='2026-08-02T12:42:55-07:00' \
    git -C "$destination" commit --quiet -m "OSWorld base snapshot $base_commit"
  git -C "$destination" remote add origin "$repository"
  created_checkout=1
fi

if [ "$created_checkout" -eq 0 ] \
  && [ -n "$(git -C "$destination" status --porcelain)" ]
then
  echo "Refusing to modify dirty OSWorld checkout: $destination" >&2
  exit 1
fi

current_tree="$(git -C "$destination" rev-parse 'HEAD^{tree}' 2>/dev/null || true)"
current="$(git -C "$destination" rev-parse HEAD 2>/dev/null || true)"
if [ "$current" = "$final_commit" ] || [ "$current_tree" = "$final_tree" ]; then
  echo "OSWorld semantic patch set already present: tree=$final_tree"
  exit 0
fi

if [ "$current_tree" != "$base_tree" ]; then
  if git -C "$destination" cat-file -e "$base_commit^{commit}" 2>/dev/null; then
    git -C "$destination" checkout --detach "$base_commit"
  else
    echo "OSWorld checkout is neither the packaged base nor final semantic tree." >&2
    echo "Move it aside and rerun the bootstrap." >&2
    exit 1
  fi
fi
git -C "$destination" am --abort >/dev/null 2>&1 || true

mapfile_cmd=mapfile
if ! command -v mapfile >/dev/null 2>&1; then
  # macOS Bash 3 is supported for source bootstrapping.
  patches=()
  while IFS= read -r patch; do patches+=("$patch"); done < <(find "$patch_dir" -maxdepth 1 -name '*.patch' -type f | sort)
else
  mapfile -t patches < <(find "$patch_dir" -maxdepth 1 -name '*.patch' -type f | sort)
fi
test "${#patches[@]}" -eq 6

GIT_COMMITTER_NAME=zainmfjavaid \
GIT_COMMITTER_EMAIL=zainmfj@gmail.com \
  git -C "$destination" am --committer-date-is-author-date "${patches[@]}"

observed_commit="$(git -C "$destination" rev-parse HEAD)"
observed_tree="$(git -C "$destination" rev-parse 'HEAD^{tree}')"
if [ "$observed_tree" != "$final_tree" ]; then
  echo "OSWorld tree mismatch: expected=$final_tree actual=$observed_tree" >&2
  exit 1
fi
if [ "$observed_commit" != "$final_commit" ]; then
  echo "OSWorld content verified, but commit identity differs: $observed_commit" >&2
  echo "Expected canonical commit: $final_commit" >&2
fi
echo "OSWorld ready: commit=$observed_commit tree=$observed_tree"
