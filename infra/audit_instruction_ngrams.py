#!/usr/bin/env python3
"""Detect copied evaluation wording in the model-facing runtime.

This is an anti-overfitting audit, not a pool-selection mechanism. It runs only
after pools have been frozen, reads their task instructions, and compares long
normalized word n-grams with the prompt/tool/server files visible to the model.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
MODEL_FACING = (
    REPO / "envserver/server.py",
    REPO / "harness/src/computerTools.ts",
    REPO / "harness/src/runEpisode.ts",
)
TOKEN = re.compile(r"[a-z0-9]+(?:['’-][a-z0-9]+)?", re.IGNORECASE)


def words(text: str) -> list[str]:
    return [token.casefold() for token in TOKEN.findall(text)]


def resolve_tasks(pool: Path) -> list[Path]:
    listed = json.loads(pool.read_text(encoding="utf-8"))
    if not isinstance(listed, list):
        raise ValueError(f"{pool}: expected a JSON array")
    tasks: list[Path] = []
    for value in listed:
        if not isinstance(value, str):
            raise ValueError(f"{pool}: task path must be a string")
        path = Path(value)
        if not path.is_absolute():
            path = (pool.parent / path).resolve()
        if not path.is_file():
            raise FileNotFoundError(path)
        tasks.append(path)
    return tasks


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pools", nargs="+", type=Path)
    parser.add_argument("--sizes", nargs="+", type=int, default=(8, 10, 12))
    args = parser.parse_args()
    sizes = sorted(set(args.sizes))
    if not sizes or sizes[0] < 4:
        raise SystemExit("ngram sizes must all be at least 4")

    task_paths: list[Path] = []
    for pool in args.pools:
        task_paths.extend(resolve_tasks(pool.resolve()))
    unique_tasks = list(dict.fromkeys(task_paths))

    grams: set[str] = set()
    for task_path in unique_tasks:
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        instruction = payload.get("instruction")
        if not isinstance(instruction, str):
            raise ValueError(f"{task_path}: missing instruction")
        tokens = words(instruction)
        for size in sizes:
            grams.update(
                " ".join(tokens[index:index + size])
                for index in range(max(0, len(tokens) - size + 1))
            )

    runtime = " ".join(
        words("\n".join(path.read_text(encoding="utf-8") for path in MODEL_FACING))
    )
    matches = sorted(gram for gram in grams if gram in runtime)
    report = {
        "ok": not matches,
        "task_count": len(unique_tasks),
        "ngram_sizes": sizes,
        "unique_instruction_ngrams": len(grams),
        "model_facing_files": [str(path.relative_to(REPO)) for path in MODEL_FACING],
        "match_count": len(matches),
        "matches": matches[:20],
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    if matches:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
