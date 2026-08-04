#!/usr/bin/env python3
"""Static validity checks for the frozen semantic runtime.

This does not claim to prove benchmark validity by itself. It enforces the
mechanical invariants that are easy to regress: no task IDs/recipes in the
model-facing runtime, no hidden GUI path in computer_exec, identical harness
mode for both model arms, and post-freeze metadata-only holdout selection.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RUNTIME_MANIFEST = REPO / os.environ.get(
    "GHOST_RUNTIME_MANIFEST", "infra/runtime_v2_files.txt",
)
MODEL_FACING = (
    REPO / "envserver/server.py",
    REPO / "harness/src/computerTools.ts",
    REPO / "harness/src/runEpisode.ts",
)

KNOWN_TASK_PHRASES = (
    "orchis",
    "google scholar",
    "unpacked extension",
    "week 0",
    "novel collection",
)
FORBIDDEN_PROMPT_ASSUMPTIONS = (
    "never switch to a competitor",
    "three tasks in the split",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"VALIDITY_AUDIT_FAILED: {message}")


def main() -> None:
    runtime_files = [
        line.strip()
        for line in RUNTIME_MANIFEST.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    require(len(runtime_files) == len(set(runtime_files)), "duplicate runtime manifest path")
    for relative in runtime_files:
        require((REPO / relative).is_file(), f"missing runtime file: {relative}")

    model_text = "\n".join(path.read_text(encoding="utf-8") for path in MODEL_FACING)
    lowered = model_text.casefold()
    require(not re.search(r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", lowered),
            "UUID/task ID embedded in model-facing runtime")
    for phrase in KNOWN_TASK_PHRASES + FORBIDDEN_PROMPT_ASSUMPTIONS:
        require(phrase not in lowered, f"task recipe/assumption embedded: {phrase}")

    server = (REPO / "envserver/server.py").read_text(encoding="utf-8")
    for boundary in (
        "_GUEST_UI_AUTOMATION_PATTERNS",
        "xdotool",
        "pyautogui|pyatspi|pynput",
        "playwright|selenium|puppeteer",
        "MAX_GUEST_EXEC_SECONDS",
        "MAX_GUEST_OUTPUT_CHARS",
        "start_new_session=True",
        "MAX_CONSECUTIVE_READONLY_JS",
        "_launch_cdp_browser_for_navigation",
        "semantic_refs",
        "became stale",
    ):
        require(boundary in server, f"computer_exec boundary missing: {boundary}")
    require('entry["steps"] += 1' in server, "computer_exec/actions are not step-accounted")

    selector = (REPO / "infra/build_valid_holdouts.py").read_text(encoding="utf-8")
    for forbidden_field in ('["instruction"]', '["evaluator"]', '["config"]'):
        require(forbidden_field not in selector,
                f"holdout selector reads forbidden task field {forbidden_field}")
    require("frozen_runtime_v2.sha256" in selector,
            "holdout selector is not gated on the frozen runtime")

    matrix = (REPO / "infra/gcp_parallel_browser_holdouts.sh").read_text(encoding="utf-8")
    require(matrix.count('"$variant" browser') == 1,
            "parallel matrix must route every arm through one shared browser mode")
    require("qwen/qwen3.6-27b" in matrix and "anthropic/claude-opus-5" in matrix,
            "parallel matrix model arms are incomplete")

    print(json.dumps({
        "ok": True,
        "runtime_files": len(runtime_files),
        "model_facing_uuid_count": 0,
        "known_task_phrase_count": 0,
        "guest_ui_escape_blocked": True,
        "holdout_selection_fields": ["id", "related_apps", "path/domain"],
        "same_harness_for_model_arms": True,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
