"""Null-agent control: every task must score 0 when the agent does NOTHING.

This is the check that decides whether any score from the local runner means
anything. A task that passes with no actions is either mis-set-up (its starting
state already satisfies the grader) or has a grader that cannot fail, and either
way it must be dropped from the pool rather than counted as a win.

Run AFTER the ladder -- it launches its own Chrome per task and would otherwise
compete for memory with the live runs.

Usage: python infra/null_agent_control.py pools/local_browser_dev.json
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))


def _forbidden(*_a, **_k):
    raise RuntimeError("easyocr stubbed; this task needs OCR and must not be scored here")


_ocr = types.ModuleType("easyocr")
_ocr.Reader = _forbidden
sys.modules.setdefault("easyocr", _ocr)

from local_chrome_env import LocalChromeEnv  # noqa: E402


def main() -> None:
    pool = json.load(open(sys.argv[1] if len(sys.argv) > 1
                          else REPO / "pools/local_browser_dev.json"))
    bad = []
    for path in pool:
        task = json.load(open(path))
        tid = task["id"][:8]
        infeasible = task.get("evaluator", {}).get("func") == "infeasible"
        env = LocalChromeEnv(headless=False)
        try:
            env.reset(task_config=task)
            score = env.evaluate()
        except Exception as exc:
            print(f"  ERROR  {tid} {type(exc).__name__}: {str(exc)[:70]}")
            continue
        finally:
            env.close()
        # An infeasible task scores 1 only if FAIL was called, and the null agent
        # calls nothing, so 0 is correct for every task in the pool.
        flag = "" if score == 0 else "  <-- PASSES WITH NO ACTIONS"
        if score != 0:
            bad.append((tid, score, infeasible))
        print(f"  {tid} score={score}{flag}  {task['instruction'][:52]}")

    print()
    if bad:
        print(f"FAIL: {len(bad)} task(s) satisfiable without acting — drop them:")
        for tid, score, inf in bad:
            print(f"  {tid} score={score} infeasible={inf}")
        sys.exit(1)
    print("PASS: every task scores 0 for a null agent; the graders can fail.")


if __name__ == "__main__":
    main()
