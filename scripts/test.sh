#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mode="${1:---full}"
case "$mode" in --fast|--full) ;; *) echo "Usage: $0 [--fast|--full]" >&2; exit 2 ;; esac

python_bin="${GHOST_TEST_PYTHON:-python3}"
"$python_bin" "$root/scripts/package-audit.py"
"$root/scripts/doctor.sh" --source-only
(cd "$root/harness" && npm run --silent check)
(cd "$root/harness" && npm run --silent semantic:schema:check)
(cd "$root/harness" && npm run --silent test:semantic)

if [ "$mode" = --full ]; then
  if [ -z "${GHOST_TEST_PYTHON:-}" ] && [ -x "$root/.venv/bin/python" ]; then
    python_bin="$root/.venv/bin/python"
  fi
  # Run every local Python contract in its own process. Several envserver
  # tests deliberately replace desktop_env in sys.modules; process isolation
  # prevents one test module from contaminating the next.
  while IFS= read -r test_file; do
    case "$(basename "$test_file")" in
      test_simple_facade_contract.py)
        # This module exports function contracts through unittest.load_tests;
        # running the file directly would define them without collecting them.
        echo "PYTEST ${test_file#$root/}"
        PYTHONPATH="$root:$root/OSWorld${PYTHONPATH:+:$PYTHONPATH}" \
          "$python_bin" -m unittest discover \
            -s "$(dirname "$test_file")" -p "$(basename "$test_file")"
        continue
        ;;
      test_close_leak.py)
        if [ "$(uname -s)" != "Darwin" ]; then
          echo "DEFER macOS local-Chrome integration: ${test_file#$root/}"
          continue
        fi
        ;;
      test_server_web_path.py|test_web_antiloop.py|test_web_find.py|\
      test_web_frames.py|test_web_js.py|test_web_offscreen.py|\
      test_web_provider.py|test_web_read.py|test_web_research.py|\
      test_web_tabs.py)
        echo "DEFER live-VM integration: ${test_file#$root/}"
        continue
        ;;
    esac
    echo "PYTEST ${test_file#$root/}"
    PYTHONPATH="$root:$root/OSWorld${PYTHONPATH:+:$PYTHONPATH}" \
      "$python_bin" "$test_file"
  done < <(find "$root/tests" -type f -name 'test_*.py' | sort)
fi
echo "PACKAGE_TESTS_OK mode=$mode"
