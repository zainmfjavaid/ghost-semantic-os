#!/usr/bin/env python3
"""Strict paired summary for matched OSWorld model arms."""
from __future__ import annotations

import argparse
import collections
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any


CONFIG_KEYS = (
    "harnessRevision", "variant", "som", "semanticDesktop", "web",
    "webTextOnly", "webFirst", "verifyGate", "noDesktop", "compactWeb",
    "browserPrompt", "codeFirst", "budgetHints", "thinkingLevel",
    "maxToolCalls",
)


def pool_ids(path: Path) -> set[str]:
    listed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(listed, list) or not listed:
        raise ValueError("pool must be a non-empty JSON list")
    return {Path(item).stem for item in listed}


def load_arm(paths: list[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payloads = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    if not payloads:
        raise ValueError("arm has no artifacts")
    results = [result for payload in payloads for result in payload.get("results", [])]
    ids = [result.get("taskId") for result in results]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task IDs within arm")
    for payload in payloads:
        if payload.get("completed") != len(payload.get("results", [])):
            raise ValueError("artifact completed count does not match results")
    for result in results:
        bad = [key for key in ("error", "evaluationError", "cleanupError") if result.get(key)]
        if bad:
            raise ValueError(f"invalid result {result.get('taskId')}: {','.join(bad)}")
    config: dict[str, Any] = {}
    for key in CONFIG_KEYS:
        values = {json.dumps(payload.get(key), sort_keys=True) for payload in payloads}
        if len(values) != 1:
            raise ValueError(f"arm disagrees on {key}")
        config[key] = payloads[0].get(key)
    models = {str(payload.get("model")) for payload in payloads}
    if len(models) != 1:
        raise ValueError("arm contains multiple models")
    config["model"] = next(iter(models))
    return results, config


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lo, hi = math.floor(index), math.ceil(index)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - index) + ordered[hi] * (index - lo)


def wilson(successes: int, total: int, z: float = 1.959963984540054) -> list[float]:
    if total == 0:
        return [0.0, 0.0]
    p = successes / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [center - spread, center + spread]


def arm_summary(results: list[dict[str, Any]], max_tools: int) -> dict[str, Any]:
    scores = [float(result.get("score", 0)) for result in results]
    calls = [int(result.get("toolCalls", 0)) for result in results]
    attempts = [int(result.get("toolAttempts", result.get("toolCalls", 0))) for result in results]
    elapsed = [float(result.get("elapsedMs", 0)) / 1000 for result in results]
    full_wins = sum(score >= 1 - 1e-9 for score in scores)
    tools: collections.Counter[str] = collections.Counter()
    for result in results:
        for event in result.get("trace", []):
            if event.get("kind") == "tool_start" and event.get("toolName"):
                tools[str(event["toolName"])] += 1
    return {
        "tasks": len(results),
        "score_total": round(sum(scores), 6),
        "score_percent": round(100 * statistics.mean(scores), 4),
        "full_wins": full_wins,
        "partial_scores": sum(1e-9 < score < 1 - 1e-9 for score in scores),
        "zeroes": sum(score <= 1e-9 for score in scores),
        "full_win_wilson_95": [round(100 * value, 3) for value in wilson(full_wins, len(results))],
        "tool_calls_mean": round(statistics.mean(calls), 3),
        "tool_calls_median": round(statistics.median(calls), 3),
        "tool_calls_p90": round(percentile([float(value) for value in calls], 0.9), 3),
        "tool_attempts_mean": round(statistics.mean(attempts), 3),
        "invalid_tool_attempts": sum(
            max(0, attempt - call) for attempt, call in zip(attempts, calls)
        ),
        "budget_hits": sum(value >= max_tools for value in calls),
        "elapsed_mean_seconds": round(statistics.mean(elapsed), 3),
        "tokens_total": sum(int(result.get("tokensTotal", 0)) for result in results),
        "cost_usd": round(sum(float(result.get("costUsd", 0)) for result in results), 6),
        "nudges": sum(int(result.get("nudges", 0)) for result in results),
        "infrastructure_retries": sum(
            int(result.get("infraRetries", 0)) for result in results
        ),
        "stop_reasons": dict(collections.Counter(str(result.get("stopReason")) for result in results)),
        "tool_mix": dict(tools.most_common()),
    }


def paired_bootstrap(qwen: dict[str, float], frontier: dict[str, float], seed: int = 73126) -> list[float]:
    ids = sorted(qwen)
    deltas = [100 * (qwen[task_id] - frontier[task_id]) for task_id in ids]
    rng = random.Random(seed)
    samples = []
    for _ in range(20_000):
        samples.append(statistics.mean(rng.choice(deltas) for _ in ids))
    return [round(percentile(samples, 0.025), 3), round(percentile(samples, 0.975), 3)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--pool", required=True, type=Path)
    parser.add_argument("--qwen", required=True, nargs="+", type=Path)
    parser.add_argument("--frontier", required=True, nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    expected = pool_ids(args.pool)
    qwen_results, qwen_config = load_arm(args.qwen)
    frontier_results, frontier_config = load_arm(args.frontier)
    qwen_scores = {str(result["taskId"]): float(result.get("score", 0)) for result in qwen_results}
    frontier_scores = {str(result["taskId"]): float(result.get("score", 0)) for result in frontier_results}
    if set(qwen_scores) != expected or set(frontier_scores) != expected:
        raise ValueError("arm task IDs do not exactly match the declared pool")
    for key in CONFIG_KEYS:
        if qwen_config[key] != frontier_config[key]:
            raise ValueError(f"model arms differ on {key}")

    max_tools = int(qwen_config["maxToolCalls"])
    qwen_summary = arm_summary(qwen_results, max_tools)
    frontier_summary = arm_summary(frontier_results, max_tools)
    comparisons = collections.Counter()
    for task_id in sorted(expected):
        q_score, f_score = qwen_scores[task_id], frontier_scores[task_id]
        comparisons[
            "tie" if abs(q_score - f_score) <= 1e-9
            else "qwen_higher" if q_score > f_score
            else "frontier_higher"
        ] += 1

    report = {
        "name": args.name,
        "valid": True,
        "pool": str(args.pool),
        "pool_count": len(expected),
        "matched_config": {key: qwen_config[key] for key in CONFIG_KEYS},
        "models": {"qwen": qwen_config["model"], "frontier": frontier_config["model"]},
        "qwen": qwen_summary,
        "frontier": frontier_summary,
        "qwen_minus_frontier_points": round(
            qwen_summary["score_percent"] - frontier_summary["score_percent"], 4
        ),
        "paired_bootstrap_gap_95": paired_bootstrap(qwen_scores, frontier_scores),
        "paired_task_comparison": dict(comparisons),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
