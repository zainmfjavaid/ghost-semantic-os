#!/usr/bin/env python3
"""Fail closed when an OSWorld pool needs unavailable setup dependencies.

This audit deliberately reads only task identity and setup *types*. It never
reads task instructions, evaluator definitions, reference answers or model
traces. The default profile is OSWorld's published ``test_nogdrive`` matrix,
which is the appropriate contract for workers without Google Drive OAuth
material.
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
NO_GDRIVE = REPO / "OSWorld/evaluation_examples/test_nogdrive.json"


def _published_compatible_ids() -> set[str]:
    payload = json.loads(NO_GDRIVE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("test_nogdrive.json must be an object of task lists")
    return {
        Path(task_path).stem
        for task_paths in payload.values()
        if isinstance(task_paths, list)
        for task_path in task_paths
        if isinstance(task_path, str)
    }


def _setup_types(task: dict[str, object]) -> list[str]:
    steps = task.get("config") or []
    if not isinstance(steps, list):
        return []
    return [
        str(step.get("type", "")).strip().casefold()
        for step in steps
        if isinstance(step, dict) and step.get("type")
    ]


def audit_pool(pool_path: Path) -> dict[str, object]:
    listed = json.loads(pool_path.read_text(encoding="utf-8"))
    if not isinstance(listed, list) or not listed:
        raise ValueError(f"{pool_path}: pool must be a non-empty JSON array")

    compatible_ids = _published_compatible_ids()
    setup_counts: collections.Counter[str] = collections.Counter()
    failures: collections.Counter[str] = collections.Counter()
    seen_ids: set[str] = set()

    for listed_path in listed:
        if not isinstance(listed_path, str):
            failures["non_string_path"] += 1
            continue
        task_path = Path(listed_path)
        if not task_path.is_absolute():
            task_path = (pool_path.parent / task_path).resolve()
        if not task_path.is_file():
            failures["missing_task_file"] += 1
            continue

        task = json.loads(task_path.read_text(encoding="utf-8"))
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            failures["missing_task_id"] += 1
            continue
        if task_id in seen_ids:
            failures["duplicate_task_id"] += 1
        seen_ids.add(task_id)

        setup_types = _setup_types(task)
        setup_counts.update(setup_types)
        if "googledrive" in setup_types:
            failures["googledrive_setup"] += 1
        if task_id not in compatible_ids:
            failures["outside_published_nogdrive_matrix"] += 1

    return {
        "pool": str(pool_path),
        "status": "pass" if not failures else "fail",
        "task_count": len(listed),
        "unique_task_ids": len(seen_ids),
        "setup_type_counts": dict(sorted(setup_counts.items())),
        "failure_counts": dict(sorted(failures.items())),
        "inspection_fields": ["id", "config[].type", "published_nogdrive_membership"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pools", nargs="+", type=Path)
    args = parser.parse_args()

    reports = [audit_pool(path.resolve()) for path in args.pools]
    print(json.dumps({"pools": reports}, indent=2, sort_keys=True))
    if any(report["status"] != "pass" for report in reports):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
