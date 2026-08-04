#!/usr/bin/env python3
"""Build an honest paired comparison from two audited article arms.

The per-arm auditor decides whether an episode is valid.  This utility then
uses the *union* of invalid, missing, and duplicate task IDs from both arms as
the paired exclusion set.  It never converts an infrastructure/evaluator
failure in either arm into a model zero.

This is intentionally read-only: it consumes collected result JSON, the
frozen pool, and ``audit_article_arm.py`` reports and writes only the optional
comparison report requested by ``--output``.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any


IMAGE_COUNTERS = (
    "screenshotsCaptured",
    "imagePartsCreated",
    "imagePartsInSession",
    "imagePartsSent",
    "pixelsSentToPolicyModel",
    "visualSidecarCalls",
)
EPISODE_SCOPED_ARM_ISSUES = {
    "invalid_or_incomplete_episodes",
    "missing_task_ids",
    "duplicate_task_ids",
}


def is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return (
        ordered[lower] * (upper - position)
        + ordered[upper] * (position - lower)
    )


def result_files(path: Path) -> list[Path]:
    candidates = [path] if path.is_file() else sorted(path.glob("*.json"))
    files: list[Path] = []
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("results"), list):
            files.append(candidate)
    if not files:
        raise ValueError(f"no result payloads found in {path}")
    return files


def load_pool(path: Path) -> tuple[list[str], dict[str, dict[str, Any]], str]:
    listed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(listed, list) or not listed:
        raise ValueError("pool must be a non-empty JSON list")
    ordered_ids: list[str] = []
    metadata: dict[str, dict[str, Any]] = {}
    for relative in listed:
        task_path = (path.parent / str(relative)).resolve()
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task_id = str(task.get("id") or task_path.stem)
        if task_id in metadata:
            raise ValueError(f"duplicate task in pool: {task_id}")
        apps = sorted({str(value) for value in task.get("related_apps", [])})
        difficulty = (
            "one_app" if len(apps) == 1 else
            "two_app" if len(apps) == 2 else
            "three_app" if len(apps) >= 3 else
            "unknown"
        )
        ordered_ids.append(task_id)
        metadata[task_id] = {
            "apps": apps,
            "difficulty": difficulty,
            "instruction": str(task.get("instruction") or ""),
        }
    return (
        ordered_ids,
        metadata,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def load_results(path: Path) -> dict[str, Any]:
    rows_by_id: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    payloads: list[tuple[Path, dict[str, Any]]] = []
    for result_path in result_files(path):
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        payloads.append((result_path, payload))
        for row in payload["results"]:
            if isinstance(row, dict) and isinstance(row.get("taskId"), str):
                rows_by_id[row["taskId"]].append(row)

    maxima = {payload.get("maxToolCalls") for _, payload in payloads}
    if len(maxima) != 1 or not all(is_number(value) for value in maxima):
        raise ValueError(f"result payloads disagree on maxToolCalls in {path}")
    modes = {str(payload.get("runtime") or "hybrid-v15") for _, payload in payloads}
    models = sorted({str(payload.get("model") or "unknown") for _, payload in payloads})
    return {
        "payloads": payloads,
        "rows_by_id": dict(rows_by_id),
        "duplicate_ids": sorted(
            task_id for task_id, rows in rows_by_id.items() if len(rows) != 1
        ),
        "max_tool_calls": int(next(iter(maxima))),
        "modes": sorted(modes),
        "models": models,
    }


def load_audit(path: Path, *, pool_sha256: str, planned: int) -> dict[str, Any]:
    audit = json.loads(path.read_text(encoding="utf-8"))
    if audit.get("pool_sha256") != pool_sha256:
        raise ValueError(f"audit pool hash mismatch: {path}")
    if audit.get("planned_denominator") != planned:
        raise ValueError(f"audit denominator mismatch: {path}")
    return audit


def invalid_reasons(
    audit: dict[str, Any], results: dict[str, Any], expected_ids: set[str]
) -> dict[str, list[str]]:
    reasons: dict[str, list[str]] = collections.defaultdict(list)
    for item in audit.get("invalid_or_incomplete_episodes", []):
        if isinstance(item, dict) and isinstance(item.get("task_id"), str):
            reasons[item["task_id"]].extend(
                str(value) for value in item.get("reasons", [])
            )
    for task_id in audit.get("missing_task_ids", []):
        reasons[str(task_id)].append("missing_task_id")
    for task_id in audit.get("duplicate_task_ids", []):
        reasons[str(task_id)].append("duplicate_task_id")
    for task_id in results["duplicate_ids"]:
        reasons[task_id].append("duplicate_result_id")
    for task_id in expected_ids:
        count = len(results["rows_by_id"].get(task_id, []))
        if count == 0:
            reasons[task_id].append("missing_result_id")
        elif count > 1:
            reasons[task_id].append("duplicate_result_id")
    return {
        task_id: sorted(set(values))
        for task_id, values in reasons.items()
        if task_id in expected_ids
    }


def one_row(results: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    rows = results["rows_by_id"].get(task_id, [])
    return rows[0] if len(rows) == 1 else None


def score_summary(rows: list[dict[str, Any]], *, denominator: int) -> dict[str, Any]:
    scores = [float(row["score"]) for row in rows if is_number(row.get("score"))]
    return {
        "denominator": denominator,
        "observed_numeric_scores": len(scores),
        "score_total": sum(scores),
        "score_rate": sum(scores) / denominator if denominator else None,
        "nonzero_solves": sum(score > 1e-9 for score in scores),
        "full_solves": sum(score >= 1 - 1e-9 for score in scores),
        "partial_scores": sum(1e-9 < score < 1 - 1e-9 for score in scores),
        "zeroes": sum(score <= 1e-9 for score in scores),
    }


def usage_summary(rows: list[dict[str, Any]], *, max_tool_calls: int) -> dict[str, Any]:
    def numbers(field: str) -> list[float]:
        return [float(row[field]) for row in rows if is_number(row.get(field))]

    calls = numbers("toolCalls")
    attempts = numbers("toolAttempts")
    tokens_total = numbers("tokensTotal")
    tokens_input = numbers("tokensInput")
    tokens_output = numbers("tokensOutput")
    costs = numbers("costUsd")
    elapsed = numbers("elapsedMs")
    return {
        "episodes": len(rows),
        "tool_calls_total": int(sum(calls)),
        "tool_calls_mean": statistics.mean(calls) if calls else None,
        "tool_calls_median": statistics.median(calls) if calls else None,
        "tool_calls_p90": percentile(calls, 0.9),
        "tool_attempts_total": int(sum(attempts)),
        "budget_hits": sum(value >= max_tool_calls for value in calls),
        "tokens_total": int(sum(tokens_total)),
        "tokens_total_mean": statistics.mean(tokens_total) if tokens_total else None,
        "tokens_total_median": statistics.median(tokens_total) if tokens_total else None,
        "tokens_input": int(sum(tokens_input)),
        "tokens_output": int(sum(tokens_output)),
        "cost_usd": sum(costs),
        "episode_elapsed_sum_seconds": sum(elapsed) / 1_000,
        "episode_elapsed_mean_seconds": (
            statistics.mean(elapsed) / 1_000 if elapsed else None
        ),
    }


def parse_run_start(run_id: Any) -> dt.datetime | None:
    if not isinstance(run_id, str):
        return None
    prefix = run_id.split("_", 1)[0]
    try:
        return dt.datetime.strptime(prefix, "%Y-%m-%dT%H-%M-%S-%fZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return None


def wall_summary(results: dict[str, Any], audit: dict[str, Any]) -> dict[str, Any]:
    shards: list[dict[str, Any]] = []
    log_ends: list[dt.datetime] = []
    for path, payload in results["payloads"]:
        started = parse_run_start(payload.get("runId"))
        completed = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        log_path = path.with_suffix(".log")
        log_minutes: float | None = None
        if log_path.is_file():
            matches = re.findall(
                r"\bin ([0-9]+(?:\.[0-9]+)?) min\s*$",
                log_path.read_text(encoding="utf-8", errors="replace"),
                flags=re.MULTILINE,
            )
            if matches:
                log_minutes = float(matches[-1])
                if started:
                    log_ends.append(started + dt.timedelta(minutes=log_minutes))
        shards.append({
            "result_file": path.name,
            "run_id": payload.get("runId"),
            "started_at": started.isoformat() if started else None,
            "completed_run_log_minutes_rounded": log_minutes,
            "result_file_mtime": completed.isoformat(),
            "mtime_derived_duration_seconds": (
                max(0.0, (completed - started).total_seconds()) if started else None
            ),
        })
    starts = [parse_run_start(payload.get("runId")) for _, payload in results["payloads"]]
    known_starts = [value for value in starts if value is not None]
    ends = [
        dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        for path, _ in results["payloads"]
    ]
    return {
        "critical_path_seconds_approx": (
            max(0.0, (max(log_ends) - min(known_starts)).total_seconds())
            if known_starts and len(log_ends) == len(results["payloads"]) else None
        ),
        "critical_path_approx_derivation": (
            "earliest runId UTC timestamp to latest shard end reconstructed from "
            "the completed run log's duration rounded to 0.1 minute"
        ),
        "critical_path_seconds_from_json_mtime": (
            max(0.0, (max(ends) - min(known_starts)).total_seconds())
            if known_starts and ends else None
        ),
        "json_mtime_derivation_caveat": (
            "earliest runId UTC timestamp to latest collected result JSON mtime; "
            "do not use as wall time unless collection is independently proven to "
            "preserve remote mtime"
        ),
        "shards": shards,
        "single_arm_audit": audit.get("wall_time"),
    }


def zero_image_summary(
    rows: list[dict[str, Any]], *, mode: str, audit: dict[str, Any]
) -> dict[str, Any]:
    if mode != "semantic-v1":
        return {"applicable": False, "proof": "not_applicable"}
    totals = {key: 0 for key in IMAGE_COUNTERS}
    missing: dict[str, list[str]] = {}
    for row in rows:
        policy = row.get("semanticPolicy")
        absent = [
            key for key in IMAGE_COUNTERS
            if not isinstance(policy, dict) or not is_number(policy.get(key))
        ]
        if absent:
            missing[str(row.get("taskId"))] = absent
            continue
        for key in IMAGE_COUNTERS:
            totals[key] += int(policy[key])
    audit_totals = audit.get("zero_image_counters_all_attempted")
    audit_complete = (
        isinstance(audit_totals, dict)
        and all(is_number(audit_totals.get(key)) for key in IMAGE_COUNTERS)
    )
    all_zero = not missing and all(value == 0 for value in totals.values())
    audit_all_zero = audit_complete and all(
        int(audit_totals[key]) == 0 for key in IMAGE_COUNTERS
    )
    return {
        "applicable": True,
        "proof": "pass" if all_zero and audit_all_zero else "fail",
        "episode_counters": totals,
        "missing_episode_counters": missing,
        "single_arm_audit_all_attempted": audit_totals,
    }


def arm_summary(
    results: dict[str, Any], audit: dict[str, Any], expected_ids: list[str],
    paired_ids: list[str], invalid: dict[str, list[str]],
) -> dict[str, Any]:
    raw_rows = [
        row for task_id in expected_ids
        if (row := one_row(results, task_id)) is not None
    ]
    own_valid_ids = [
        task_id for task_id in expected_ids
        if task_id not in invalid and one_row(results, task_id) is not None
    ]
    own_valid_rows = [one_row(results, task_id) for task_id in own_valid_ids]
    paired_rows = [one_row(results, task_id) for task_id in paired_ids]
    assert all(row is not None for row in own_valid_rows + paired_rows)
    mode = str(audit.get("mode") or results["modes"][0])
    return {
        "identity": {
            "mode": mode,
            "models": results["models"],
            "max_tool_calls": results["max_tool_calls"],
            "audit_publishable_before_pairing": audit.get("publishable"),
            "audit_arm_issues": audit.get("arm_issues", []),
        },
        "raw": {
            "planned_denominator": len(expected_ids),
            "observed_unique_tasks": len(raw_rows),
            "invalid_or_missing_tasks": len(invalid),
            "score": score_summary(raw_rows, denominator=len(expected_ids)),
            "usage": usage_summary(raw_rows, max_tool_calls=results["max_tool_calls"]),
        },
        "own_valid": {
            "task_ids": own_valid_ids,
            "score": score_summary(own_valid_rows, denominator=len(own_valid_rows)),
        },
        "paired_valid": {
            "score": score_summary(paired_rows, denominator=len(paired_rows)),
            "usage": usage_summary(paired_rows, max_tool_calls=results["max_tool_calls"]),
        },
        "zero_image_all_observed": zero_image_summary(raw_rows, mode=mode, audit=audit),
        "wall": wall_summary(results, audit),
    }


def paired_comparison(
    *, left_results: dict[str, Any], left_audit: dict[str, Any],
    right_results: dict[str, Any], right_audit: dict[str, Any],
    expected_ids: list[str], metadata: dict[str, dict[str, Any]],
    left_label: str, right_label: str, pool_sha256: str,
) -> dict[str, Any]:
    expected_set = set(expected_ids)
    left_invalid = invalid_reasons(left_audit, left_results, expected_set)
    right_invalid = invalid_reasons(right_audit, right_results, expected_set)
    paired_ids = [
        task_id for task_id in expected_ids
        if task_id not in left_invalid and task_id not in right_invalid
    ]
    excluded_ids = [task_id for task_id in expected_ids if task_id not in paired_ids]

    left = arm_summary(
        left_results, left_audit, expected_ids, paired_ids, left_invalid
    )
    right = arm_summary(
        right_results, right_audit, expected_ids, paired_ids, right_invalid
    )
    difficulty: dict[str, Any] = {}
    for name in ("one_app", "two_app", "three_app", "unknown"):
        planned_ids = [
            task_id for task_id in expected_ids
            if metadata[task_id]["difficulty"] == name
        ]
        valid_ids = [task_id for task_id in paired_ids if task_id in planned_ids]
        if not planned_ids:
            continue
        left_rows = [one_row(left_results, task_id) for task_id in valid_ids]
        right_rows = [one_row(right_results, task_id) for task_id in valid_ids]
        assert all(row is not None for row in left_rows + right_rows)
        difficulty[name] = {
            "planned": len(planned_ids),
            "paired_valid": len(valid_ids),
            "excluded": len(planned_ids) - len(valid_ids),
            left_label: score_summary(left_rows, denominator=len(valid_ids)),
            right_label: score_summary(right_rows, denominator=len(valid_ids)),
        }

    task_deltas: list[dict[str, Any]] = []
    for task_id in expected_ids:
        left_row = one_row(left_results, task_id)
        right_row = one_row(right_results, task_id)
        valid = task_id in paired_ids

        def delta(field: str) -> float | None:
            if (
                not valid or left_row is None or right_row is None
                or not is_number(left_row.get(field))
                or not is_number(right_row.get(field))
            ):
                return None
            return float(right_row[field]) - float(left_row[field])

        task_deltas.append({
            "task_id": task_id,
            "difficulty": metadata[task_id]["difficulty"],
            "apps": metadata[task_id]["apps"],
            "paired_valid": valid,
            "exclusion_reasons": {
                left_label: left_invalid.get(task_id, []),
                right_label: right_invalid.get(task_id, []),
            },
            left_label: {
                "score": left_row.get("score") if left_row else None,
                "tool_calls": left_row.get("toolCalls") if left_row else None,
                "tokens_total": left_row.get("tokensTotal") if left_row else None,
                "cost_usd": left_row.get("costUsd") if left_row else None,
                "stop_reason": left_row.get("stopReason") if left_row else None,
            },
            right_label: {
                "score": right_row.get("score") if right_row else None,
                "tool_calls": right_row.get("toolCalls") if right_row else None,
                "tokens_total": right_row.get("tokensTotal") if right_row else None,
                "cost_usd": right_row.get("costUsd") if right_row else None,
                "stop_reason": right_row.get("stopReason") if right_row else None,
            },
            "delta_right_minus_left": {
                "score": delta("score"),
                "tool_calls": delta("toolCalls"),
                "tokens_total": delta("tokensTotal"),
                "cost_usd": delta("costUsd"),
                "elapsed_ms": delta("elapsedMs"),
            },
        })

    left_global = sorted(
        set(left_audit.get("arm_issues", [])) - EPISODE_SCOPED_ARM_ISSUES
    )
    right_global = sorted(
        set(right_audit.get("arm_issues", [])) - EPISODE_SCOPED_ARM_ISSUES
    )
    left_audit_publishable = left_audit.get("publishable") is True
    right_audit_publishable = right_audit.get("publishable") is True
    left_rate = left["paired_valid"]["score"]["score_rate"]
    right_rate = right["paired_valid"]["score"]["score_rate"]
    return {
        "pool_sha256": pool_sha256,
        "planned_denominator": len(expected_ids),
        "paired_valid_denominator": len(paired_ids),
        "paired_task_ids": paired_ids,
        "excluded_from_pair": [
            {
                "task_id": task_id,
                left_label: left_invalid.get(task_id, []),
                right_label: right_invalid.get(task_id, []),
            }
            for task_id in excluded_ids
        ],
        # Pairing can align denominators; it cannot launder a failed single-arm
        # integrity audit into a publishable comparison. Fail closed when an
        # audit is missing the explicit true value as well.
        "comparison_publishable": (
            bool(paired_ids)
            and left_audit_publishable
            and right_audit_publishable
            and not left_global
            and not right_global
        ),
        "arm_audit_publishable": {
            left_label: left_audit_publishable,
            right_label: right_audit_publishable,
        },
        "global_integrity_blockers": {
            left_label: left_global,
            right_label: right_global,
        },
        "delta_definition": f"{right_label} minus {left_label}",
        "paired_score_rate_delta": (
            right_rate - left_rate
            if is_number(left_rate) and is_number(right_rate) else None
        ),
        "arms": {left_label: left, right_label: right},
        "difficulty": difficulty,
        "task_deltas": task_deltas,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--left-arm", required=True, type=Path)
    parser.add_argument("--right-arm", required=True, type=Path)
    parser.add_argument("--left-audit", type=Path)
    parser.add_argument("--right-audit", type=Path)
    parser.add_argument("--left-label", default="left")
    parser.add_argument("--right-label", default="right")
    parser.add_argument("--pool", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.left_label == args.right_label:
        raise SystemExit("left and right labels must differ")

    expected_ids, metadata, pool_sha = load_pool(args.pool)
    left_results = load_results(args.left_arm)
    right_results = load_results(args.right_arm)
    left_audit_path = args.left_audit or args.left_arm / "audit.json"
    right_audit_path = args.right_audit or args.right_arm / "audit.json"
    left_audit = load_audit(
        left_audit_path, pool_sha256=pool_sha, planned=len(expected_ids)
    )
    right_audit = load_audit(
        right_audit_path, pool_sha256=pool_sha, planned=len(expected_ids)
    )
    report = paired_comparison(
        left_results=left_results,
        left_audit=left_audit,
        right_results=right_results,
        right_audit=right_audit,
        expected_ids=expected_ids,
        metadata=metadata,
        left_label=args.left_label,
        right_label=args.right_label,
        pool_sha256=pool_sha,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if report["comparison_publishable"] else 2)


if __name__ == "__main__":
    main()
