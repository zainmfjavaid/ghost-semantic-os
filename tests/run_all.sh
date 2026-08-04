#!/usr/bin/env bash
# Hermetic local regression for the browser + semantic desktop harness.
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chrome_bin="${CHROME_BIN:-}"
if [ -z "$chrome_bin" ]; then
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "$(command -v google-chrome 2>/dev/null || true)" \
    "$(command -v google-chrome-stable 2>/dev/null || true)" \
    "$(command -v chromium 2>/dev/null || true)" \
    "$(command -v chromium-browser 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      chrome_bin="$candidate"
      break
    fi
  done
fi
fixture_dir="$(mktemp -d /tmp/osworld-regression-chrome.XXXXXX)"
chrome_pids=()

cleanup() {
  # Bash 3.2 (the macOS default) treats an empty array expansion as unbound
  # under `set -u`. The `-` default yields one empty value, which is skipped.
  for pid in "${chrome_pids[@]-}"; do
    [ -n "$pid" ] || continue
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${chrome_pids[@]-}"; do
    [ -n "$pid" ] || continue
    wait "$pid" 2>/dev/null || true
  done
  find "$fixture_dir" -depth -delete 2>/dev/null || true
}
trap cleanup EXIT

if [ -z "$chrome_bin" ] || [ ! -x "$chrome_bin" ]; then
  echo "No Chrome/Chromium binary found; set CHROME_BIN explicitly" >&2
  exit 1
fi
for port in 9222 1337; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "CDP fixture port $port is already in use; refusing to touch that process" >&2
    exit 1
  fi
  "$chrome_bin" \
    --headless=new \
    --remote-debugging-address=127.0.0.1 \
    --remote-debugging-port="$port" \
    --user-data-dir="$fixture_dir/profile-$port" \
    --no-first-run \
    --no-default-browser-check \
    --disable-background-networking \
    about:blank \
    >"$fixture_dir/chrome-$port.log" 2>&1 &
  chrome_pids+=("$!")
done

for port in 9222 1337; do
  ready=0
  for _ in $(seq 1 60); do
    if curl -fsS --max-time 1 "http://127.0.0.1:$port/json/version" >/dev/null; then
      ready=1
      break
    fi
    sleep 0.25
  done
  test "$ready" -eq 1
done

cd "$repo_dir"
osworld_python="${OSWORLD_PYTHON:-}"
if [ -z "$osworld_python" ]; then
  if [ -x "$repo_dir/.venv-stub/bin/python" ]; then
    osworld_python="$repo_dir/.venv-stub/bin/python"
  else
    osworld_python="python3"
  fi
fi
python3 tests/test_antiloop.py
python3 tests/test_close_leak.py
python3 tests/test_create_cleanup.py
python3 tests/test_desktop_guard.py
python3 tests/test_frames.py
python3 tests/test_frames2.py
python3 tests/test_pool_preflight.py
PYTHONPATH="$repo_dir/OSWorld" "$osworld_python" tests/test_osworld_evaluator_contracts.py
python3 infra/audit_runtime_validity.py
python3 infra/audit_instruction_ngrams.py \
  pools/browser_holdout_a_v2_runnable.json \
  pools/browser_holdout_b_v2_runnable.json \
  pools/desktop_ood_v2.json
python3 tests/test_server_web_path.py
python3 tests/test_web_antiloop.py
python3 tests/test_web_connect_retry.py
python3 tests/test_web_navigation_recovery.py
python3 tests/test_web_find.py
python3 tests/test_web_frames.py
python3 tests/test_web_js.py
python3 tests/test_web_offscreen.py
python3 tests/test_web_provider.py
python3 tests/test_web_read.py
python3 tests/test_web_research.py
python3 tests/test_web_stall_recovery.py
python3 tests/test_web_tabs.py
harness/node_modules/.bin/tsx harness/tests/testKeySequence.ts
harness/node_modules/.bin/tsx harness/tests/testModelEndpoint.ts
harness/node_modules/.bin/tsx harness/tests/testRuntimePolicy.ts
harness/node_modules/.bin/tsx harness/tests/testTaskComplete.ts
npm --prefix harness run check
git diff --check
