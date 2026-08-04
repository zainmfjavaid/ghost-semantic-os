#!/usr/bin/env python3
"""Read-only validity and metric audit for one article benchmark arm.

The script deliberately distinguishes an invalid/incomplete episode from a
valid scored failure. Missing telemetry is never converted to zero.
"""
from __future__ import annotations

import argparse
import collections
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
SEMANTIC_TOOLS = {
    "computer.query",
    "computer.act",
    "computer.verify",
    "computer.run",
    "task_complete",
}
# These fields define one comparable model/runtime arm and therefore must be
# identical across shards.  ``concurrency`` is deliberately absent: it is a
# per-shard execution parameter, commonly equal to that shard's task count.
# Requiring 6/6/5/5 shards to claim one concurrency value creates a false
# integrity failure without changing model behavior or runtime identity.
ARM_IDENTITY_FIELDS = (
    "harnessRevision", "provider", "model", "variant", "thinkingLevel",
    "maxToolCalls", "som", "semanticDesktop", "visionOnly", "web",
    "webTextOnly", "webFirst", "verifyGate", "noDesktop", "compactWeb",
    "browserPrompt", "codeFirst", "budgetHints",
)
SEMANTIC_IDENTITY_FIELDS = (
    "runtime", "semanticProtocolVersion", "parentCommit", "nestedOSWorldCommit",
    "runtimeManifestSha256", "runtimeFilesSha256", "taskPoolSha256",
)
UNKNOWN_VALUES = {"", "unknown", "none", "null", "<no value>"}
V15_PARENT_COMMIT = "7917f695314c8fe3249a374dd16701d4451fe897"
V15_OSWORLD_COMMIT = "fad6d07f0a3ad456e7d966dcc98a7fee2491afe0"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_sha256_without_self(payload: dict[str, Any]) -> str:
    value = dict(payload)
    value.pop("runtime_manifest_sha256", None)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256_bytes(encoded)


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_integer(value: Any) -> bool:
    return is_number(value) and int(value) == value


def present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in UNKNOWN_VALUES
    return True


def payload_identity_issues(
    payloads: list[dict[str, Any]], *, mode: str
) -> list[str]:
    """Return cross-shard identity disagreements, excluding execution shape."""

    issues: list[str] = []
    for key in ARM_IDENTITY_FIELDS:
        values = {json.dumps(payload.get(key), sort_keys=True) for payload in payloads}
        if len(values) != 1:
            issues.append(f"payload_disagreement:{key}")
    if mode == "semantic-v1":
        for key in SEMANTIC_IDENTITY_FIELDS:
            values = {
                json.dumps(payload.get(key), sort_keys=True) for payload in payloads
            }
            if len(values) != 1:
                issues.append(f"payload_disagreement:{key}")
    for payload in payloads:
        concurrency = payload.get("concurrency")
        if not is_integer(concurrency) or int(concurrency) < 1:
            issues.append("invalid_shard_concurrency")
    return sorted(set(issues))


def normalize_app(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    aliases = {
        "calc": "libreoffice_calc",
        "writer": "libreoffice_writer",
        "impress": "libreoffice_impress",
        "vscode": "vs_code",
        "libreoffice": "libreoffice_suite",
    }
    return aliases.get(normalized, normalized)


def load_pool(path: Path) -> tuple[list[str], dict[str, dict[str, Any]]]:
    listed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(listed, list) or not listed:
        raise ValueError("pool must be a non-empty JSON list")
    ids: list[str] = []
    metadata: dict[str, dict[str, Any]] = {}
    for relative in listed:
        task_path = (path.parent / str(relative)).resolve()
        task = json.loads(task_path.read_text(encoding="utf-8"))
        task_id = str(task.get("id") or task_path.stem)
        if task_id in metadata:
            raise ValueError(f"duplicate task in pool: {task_id}")
        apps = sorted({normalize_app(str(app)) for app in task.get("related_apps", [])})
        difficulty = (
            "one_app" if len(apps) == 1 else
            "two_app" if len(apps) == 2 else
            "three_app" if len(apps) >= 3 else
            "unknown"
        )
        ids.append(task_id)
        metadata[task_id] = {
            "apps": apps,
            "difficulty": difficulty,
            "instruction": task.get("instruction", ""),
        }
    return ids, metadata


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


def parse_result(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    # Harness traces retain a model-facing JSON result followed by a private
    # diagnostic trailer.  Only the first document is the typed tool result;
    # the trailer is deliberately outside the protocol envelope.
    text = text.partition("\nDETAILS: ")[0]
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def episode_issues(
    episode: dict[str, Any], *, mode: str, max_tool_calls: int,
    duplicate_ids: set[str], expected_ids: set[str], source_host: str,
) -> list[str]:
    issues: list[str] = []
    task_id = episode.get("taskId")
    if not isinstance(task_id, str):
        issues.append("missing_task_id")
    elif task_id not in expected_ids:
        issues.append("extra_task_id")
    elif task_id in duplicate_ids:
        issues.append("duplicate_task_id")
    for key in ("error", "evaluationError", "cleanupError"):
        if episode.get(key):
            issues.append(key)
    if episode.get("stopReason") == "error":
        issues.append("stop_reason_error")
    score = episode.get("score")
    if not is_number(score) or not 0 <= float(score) <= 1:
        issues.append("invalid_score")
    calls = episode.get("toolCalls")
    attempts = episode.get("toolAttempts")
    if not is_integer(calls) or not 0 <= int(calls) <= max_tool_calls:
        issues.append("invalid_tool_calls")
    if not is_integer(attempts) or int(attempts) < 0:
        issues.append("invalid_tool_attempts")
    elif is_integer(calls) and int(attempts) < int(calls):
        issues.append("tool_attempts_below_calls")
    if not is_number(episode.get("elapsedMs")) or float(episode["elapsedMs"]) < 0:
        issues.append("invalid_elapsed_ms")
    if not is_number(episode.get("costUsd")) or float(episode["costUsd"]) < 0:
        issues.append("invalid_cost")
    trace = episode.get("trace")
    if not isinstance(trace, list):
        issues.append("missing_trace")
        return issues
    starts = [
        event.get("toolCallId") for event in trace
        if isinstance(event, dict) and event.get("kind") == "tool_start"
    ]
    ends = [
        event.get("toolCallId") for event in trace
        if isinstance(event, dict) and event.get("kind") == "tool_end"
    ]
    if any(not present(value) for value in starts + ends):
        issues.append("missing_tool_call_id")
    if collections.Counter(starts) != collections.Counter(ends):
        issues.append("unpaired_tool_trace")
    if len(starts) != len(set(starts)):
        issues.append("duplicate_tool_call_id")
    if is_integer(attempts) and int(attempts) != len(starts):
        issues.append("tool_attempt_trace_mismatch")

    if mode != "semantic-v1":
        return issues
    if episode.get("runtime") != "semantic-v1":
        issues.append("wrong_episode_runtime")
    if episode.get("semanticProtocolVersion") != "1.0":
        issues.append("wrong_episode_protocol")
    policy = episode.get("semanticPolicy")
    if not isinstance(policy, dict):
        issues.append("missing_semantic_policy")
    else:
        for key in IMAGE_COUNTERS:
            value = policy.get(key)
            if not is_integer(value):
                issues.append(f"missing_image_counter:{key}")
            elif value != 0:
                issues.append(f"nonzero_image_counter:{key}={value}")
        operations = policy.get("semanticOperations")
        max_operations = min(max_tool_calls * 10, 1_000)
        if not is_integer(operations) or not 0 <= int(operations) <= max_operations:
            issues.append("invalid_semantic_operations")
    identity = episode.get("environmentIdentity")
    if not isinstance(identity, dict):
        issues.append("missing_environment_identity")
    else:
        expected = {
            "outer_provider": "gcp",
            "guest_platform": "linux",
        }
        for key, value in expected.items():
            if str(identity.get(key, "")).lower() != value:
                issues.append(f"invalid_identity:{key}")
        for key in (
            "outer_vm_name", "nested_guest_machine_id", "guest_os_release_hash",
            "guest_image_digest", "display_identity", "semantic_guest_bundle_hash",
        ):
            if not present(identity.get(key)):
                issues.append(f"missing_identity:{key}")
        if present(identity.get("outer_vm_name")) and identity["outer_vm_name"] != source_host:
            issues.append("outer_vm_source_mismatch")
    for event in trace:
        if not isinstance(event, dict) or event.get("kind") != "tool_end":
            continue
        tool = event.get("toolName")
        if isinstance(tool, str) and tool not in SEMANTIC_TOOLS and not event.get("isError"):
            issues.append(f"unexpected_executed_tool:{tool}")
        if isinstance(tool, str) and tool.startswith("computer."):
            parsed = parse_result(event.get("resultText"))
            # Strict semantic-v1 promises that every computer result, including
            # malformed model arguments rejected before adapter dispatch, uses
            # the canonical typed error envelope.  A Pi framework error is
            # therefore an arm-integrity failure rather than an allowed policy
            # outcome.
            if parsed is None:
                issues.append("untyped_semantic_tool_result")
            elif parsed is not None and parsed.get("protocol_version") != "1.0":
                issues.append("semantic_result_protocol_mismatch")
    return sorted(set(issues))


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    position = (len(values) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    if lo == hi:
        return values[lo]
    return values[lo] * (hi - position) + values[hi] * (position - lo)


def usage(rows: list[dict[str, Any]], *, semantic: bool) -> dict[str, Any]:
    calls = [int(row["toolCalls"]) for row in rows if is_integer(row.get("toolCalls"))]
    attempts = [int(row["toolAttempts"]) for row in rows if is_integer(row.get("toolAttempts"))]
    elapsed = [float(row["elapsedMs"]) for row in rows if is_number(row.get("elapsedMs"))]
    costs = [float(row["costUsd"]) for row in rows if is_number(row.get("costUsd"))]
    operations = [
        int(row["semanticPolicy"]["semanticOperations"])
        for row in rows
        if semantic and isinstance(row.get("semanticPolicy"), dict)
        and is_integer(row["semanticPolicy"].get("semanticOperations"))
    ]
    return {
        "tool_calls_total": sum(calls),
        "tool_calls_mean": statistics.mean(calls) if calls else None,
        "tool_calls_median": statistics.median(calls) if calls else None,
        "tool_calls_p90": percentile([float(value) for value in calls], 0.9),
        "tool_attempts_total": sum(attempts),
        "semantic_operations_total": sum(operations) if semantic else None,
        "episode_elapsed_sum_seconds": sum(elapsed) / 1_000,
        "episode_elapsed_mean_seconds": statistics.mean(elapsed) / 1_000 if elapsed else None,
        "cost_usd": sum(costs),
    }


def score_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [float(row["score"]) for row in rows]
    return {
        "valid_tasks": len(rows),
        "score_total": sum(scores),
        "score_mean": statistics.mean(scores) if scores else None,
        "nonzero_solves": sum(score > 1e-9 for score in scores),
        "full_solves": sum(score >= 1 - 1e-9 for score in scores),
        "partial_scores": sum(1e-9 < score < 1 - 1e-9 for score in scores),
        "zeroes": sum(score <= 1e-9 for score in scores),
    }


def trace_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tools: collections.Counter[str] = collections.Counter()
    typed_errors: collections.Counter[str] = collections.Counter()
    statuses: collections.Counter[str] = collections.Counter()
    completion_rejections: collections.Counter[str] = collections.Counter()
    untyped = 0
    tool_errors = 0
    for row in rows:
        for event in row.get("trace", []):
            if not isinstance(event, dict):
                continue
            tool = str(event.get("toolName") or "unknown")
            if event.get("kind") == "tool_start":
                tools[tool] += 1
                continue
            if event.get("kind") != "tool_end":
                continue
            if event.get("isError"):
                tool_errors += 1
            parsed = parse_result(event.get("resultText"))
            if tool.startswith("computer."):
                if parsed is None:
                    untyped += 1
                    continue
                status = str(parsed.get("status") or "missing")
                if status != "ok":
                    statuses[status] += 1
                error = parsed.get("error")
                if isinstance(error, dict) and present(error.get("code")):
                    typed_errors[str(error["code"])] += 1
            elif tool == "task_complete" and isinstance(parsed, dict) and not parsed.get("accepted"):
                error = parsed.get("error")
                code = error.get("code") if isinstance(error, dict) else "unknown"
                completion_rejections[str(code or "unknown")] += 1
    return {
        "tool_mix": dict(tools.most_common()),
        "typed_error_codes": dict(typed_errors.most_common()),
        "non_ok_semantic_statuses": dict(statuses.most_common()),
        "completion_rejections": dict(completion_rejections.most_common()),
        "tool_results_with_is_error": tool_errors,
        "untyped_semantic_results": untyped,
    }


def episode_failure_detail(row: dict[str, Any], metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
    typed_errors: collections.Counter[str] = collections.Counter()
    statuses: collections.Counter[str] = collections.Counter()
    completion_rejections = 0
    for event in row.get("trace", []):
        if not isinstance(event, dict) or event.get("kind") != "tool_end":
            continue
        tool = str(event.get("toolName") or "")
        parsed = parse_result(event.get("resultText"))
        if tool.startswith("computer.") and parsed is not None:
            if parsed.get("status") != "ok":
                statuses[str(parsed.get("status") or "missing")] += 1
            error = parsed.get("error")
            if isinstance(error, dict) and present(error.get("code")):
                typed_errors[str(error["code"])] += 1
        elif tool == "task_complete" and isinstance(parsed, dict) and not parsed.get("accepted"):
            completion_rejections += 1
    task_id = str(row.get("taskId"))
    policy = row.get("semanticPolicy")
    return {
        "task_id": task_id,
        "difficulty": metadata.get(task_id, {}).get("difficulty", "unknown"),
        "apps": metadata.get(task_id, {}).get("apps", []),
        "score": row.get("score"),
        "stop_reason": row.get("stopReason"),
        "tool_calls": row.get("toolCalls"),
        "tool_attempts": row.get("toolAttempts"),
        "semantic_operations": (
            policy.get("semanticOperations") if isinstance(policy, dict) else None
        ),
        "elapsed_seconds": (
            float(row["elapsedMs"]) / 1_000 if is_number(row.get("elapsedMs")) else None
        ),
        "cost_usd": row.get("costUsd"),
        "typed_error_codes": dict(typed_errors),
        "semantic_statuses": dict(statuses),
        "completion_rejections": completion_rejections,
    }


def log_wall_metrics(arm: Path) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    pattern = re.compile(r"\bin ([0-9]+(?:\.[0-9]+)?) min\s*$", re.MULTILINE)
    for path in sorted(arm.glob("*.log")):
        matches = pattern.findall(path.read_text(encoding="utf-8", errors="replace"))
        if matches:
            values.append({"log": path.name, "minutes": float(matches[-1])})
    minutes = [row["minutes"] for row in values]
    return {
        "source": "rounded shard completion lines; not exact arm wall",
        "shards": values,
        "critical_path_minutes_approx": max(minutes) if minutes else None,
        "sum_shard_minutes_approx": sum(minutes) if minutes else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", required=True, type=Path)
    parser.add_argument("--pool", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("semantic-v1", "hybrid-v15"))
    parser.add_argument("--max-tool-calls", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    expected_list, metadata = load_pool(args.pool)
    expected_ids = set(expected_list)
    pool_sha = sha256_bytes(args.pool.read_bytes())
    files = result_files(args.arm)
    payloads: list[tuple[Path, dict[str, Any]]] = [
        (path, json.loads(path.read_text(encoding="utf-8"))) for path in files
    ]
    arm_issues: list[str] = []
    rows: list[tuple[Path, dict[str, Any]]] = []
    for path, payload in payloads:
        results = payload.get("results", [])
        if payload.get("completed") != len(results):
            arm_issues.append(f"{path.name}:completed_count_mismatch")
        if payload.get("maxToolCalls") != args.max_tool_calls:
            arm_issues.append(f"{path.name}:max_tool_calls_mismatch")
        if args.mode == "semantic-v1":
            if payload.get("runtime") != "semantic-v1":
                arm_issues.append(f"{path.name}:wrong_runtime")
            if payload.get("semanticProtocolVersion") != "1.0":
                arm_issues.append(f"{path.name}:wrong_protocol")
            if payload.get("taskPoolSha256") != pool_sha:
                arm_issues.append(f"{path.name}:task_pool_hash_mismatch")
            for key in (
                "parentCommit", "nestedOSWorldCommit", "runtimeManifestSha256",
                "runtimeFilesSha256",
            ):
                if not present(payload.get(key)):
                    arm_issues.append(f"{path.name}:missing_{key}")
            top_policy = payload.get("imagePolicy")
            if not isinstance(top_policy, dict):
                arm_issues.append(f"{path.name}:missing_top_image_policy")
            else:
                for key in IMAGE_COUNTERS:
                    if not is_integer(top_policy.get(key)) or top_policy[key] != 0:
                        arm_issues.append(f"{path.name}:invalid_top_image_counter:{key}")
                    expected_sum = sum(
                        episode.get("semanticPolicy", {}).get(key, 0)
                        for episode in results
                        if isinstance(episode.get("semanticPolicy"), dict)
                        and is_integer(episode["semanticPolicy"].get(key))
                    )
                    if is_integer(top_policy.get(key)) and top_policy[key] != expected_sum:
                        arm_issues.append(f"{path.name}:top_image_sum_mismatch:{key}")
        rows.extend((path, episode) for episode in results if isinstance(episode, dict))

    arm_issues.extend(payload_identity_issues(
        [payload for _, payload in payloads], mode=args.mode
    ))

    row_ids = [str(row.get("taskId")) for _, row in rows if isinstance(row.get("taskId"), str)]
    counts = collections.Counter(row_ids)
    duplicate_ids = {task_id for task_id, count in counts.items() if count > 1}
    missing_ids = sorted(expected_ids - set(row_ids))
    extra_ids = sorted(set(row_ids) - expected_ids)
    if duplicate_ids:
        arm_issues.append("duplicate_task_ids")
    if missing_ids:
        arm_issues.append("missing_task_ids")
    if extra_ids:
        arm_issues.append("extra_task_ids")

    # gcp_collect emits independent pool/shard/manifest evidence. Require it for
    # article arms rather than trusting only fields embedded by the runtime.
    for path, payload in payloads:
        prefix = path.with_suffix("")
        pool_sha_path = Path(f"{prefix}.pool_sha")
        shard_path = Path(f"{prefix}.shard.json")
        manifest_path = Path(f"{prefix}.runtime-manifest.json")
        source_state_path = Path(f"{prefix}.source_state")
        environment_state_path = Path(f"{prefix}.environment_state")
        python_freeze_path = Path(f"{prefix}.python-freeze.txt")
        log_path = Path(f"{prefix}.log")
        if not pool_sha_path.is_file() or pool_sha_path.read_text().strip() != pool_sha:
            arm_issues.append(f"{path.name}:missing_or_wrong_external_pool_hash")
        if not shard_path.is_file():
            arm_issues.append(f"{path.name}:missing_shard_manifest")
        else:
            shard = json.loads(shard_path.read_text(encoding="utf-8"))
            shard_ids = sorted(Path(str(value)).stem for value in shard)
            result_ids = sorted(str(value.get("taskId")) for value in payload["results"])
            if shard_ids != result_ids:
                arm_issues.append(f"{path.name}:shard_result_mismatch")
        if not manifest_path.is_file():
            arm_issues.append(f"{path.name}:missing_runtime_manifest")
        else:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_sha = manifest.get("runtime_manifest_sha256")
            if manifest_sha != json_sha256_without_self(manifest):
                arm_issues.append(f"{path.name}:runtime_manifest_hash_invalid")
            embedded = payload.get("runtimeManifestSha256")
            if args.mode == "semantic-v1" and embedded != manifest_sha:
                arm_issues.append(f"{path.name}:runtime_manifest_result_mismatch")
            if args.mode == "semantic-v1":
                expected_manifest_fields = {
                    "runtime": "semantic-v1",
                    "semantic_protocol_version": "1.0",
                    "parent_commit": payload.get("parentCommit"),
                    "nested_osworld_commit": payload.get("nestedOSWorldCommit"),
                    # Despite the legacy result-field name, this is the
                    # server/guest runtime fingerprint.  The manifest's
                    # runtime_files_sha256 covers the larger full harness
                    # inventory and is independently protected by the signed
                    # manifest hash.
                    "server_runtime_sha256": payload.get("runtimeFilesSha256"),
                }
                for key, expected in expected_manifest_fields.items():
                    if manifest.get(key) != expected:
                        arm_issues.append(f"{path.name}:manifest_result_mismatch:{key}")
                if manifest.get("task_pool", {}).get("sha256") != pool_sha:
                    arm_issues.append(f"{path.name}:manifest_pool_hash_mismatch")
            if args.mode == "hybrid-v15" and manifest_sha not in str(payload.get("harnessRevision", "")):
                arm_issues.append(f"{path.name}:runtime_manifest_not_in_harness_revision")
            if args.mode == "hybrid-v15":
                if manifest.get("parent_commit") != V15_PARENT_COMMIT:
                    arm_issues.append(f"{path.name}:wrong_v15_parent_commit")
                if manifest.get("nested_osworld_commit") != V15_OSWORLD_COMMIT:
                    arm_issues.append(f"{path.name}:wrong_v15_osworld_commit")
            if not source_state_path.is_file():
                arm_issues.append(f"{path.name}:missing_source_state")
            else:
                source_state = dict(
                    line.split("=", 1)
                    for line in source_state_path.read_text(encoding="utf-8").splitlines()
                    if "=" in line
                )
                # For semantic-v1 the controller checkout is the runtime. For
                # v15 it is only the orchestrator; the frozen runtime lives in
                # a separate checkout and is identified by its manifest/hash.
                if args.mode == "semantic-v1":
                    if source_state.get("commit") != manifest.get("parent_commit"):
                        arm_issues.append(f"{path.name}:source_parent_commit_mismatch")
                    if source_state.get("osworld_commit") != manifest.get("nested_osworld_commit"):
                        arm_issues.append(f"{path.name}:source_osworld_commit_mismatch")
            if not environment_state_path.is_file():
                arm_issues.append(f"{path.name}:missing_environment_state")
            elif sha256_bytes(environment_state_path.read_bytes()) != manifest.get("environment_state_sha256"):
                arm_issues.append(f"{path.name}:environment_state_hash_mismatch")
            if not python_freeze_path.is_file():
                arm_issues.append(f"{path.name}:missing_python_freeze")
            elif sha256_bytes(python_freeze_path.read_bytes()) != manifest.get("python", {}).get("freeze_sha256"):
                arm_issues.append(f"{path.name}:python_freeze_hash_mismatch")
        if not log_path.is_file() or not re.search(
            r"\bin [0-9]+(?:\.[0-9]+)? min\s*$",
            log_path.read_text(encoding="utf-8", errors="replace"),
            flags=re.MULTILINE,
        ):
            arm_issues.append(f"{path.name}:missing_completed_run_log")

    audited: list[dict[str, Any]] = []
    for path, episode in rows:
        source_host = path.stem
        issues = episode_issues(
            episode, mode=args.mode, max_tool_calls=args.max_tool_calls,
            duplicate_ids=duplicate_ids, expected_ids=expected_ids, source_host=source_host,
        )
        audited.append({"source": path.name, "episode": episode, "issues": issues})
    invalid = [row for row in audited if row["issues"]]
    valid_rows = [row["episode"] for row in audited if not row["issues"]]
    if invalid:
        arm_issues.append("invalid_or_incomplete_episodes")

    by_difficulty: dict[str, Any] = {}
    for difficulty in ("one_app", "two_app", "three_app", "unknown"):
        expected = {task_id for task_id in expected_ids if metadata[task_id]["difficulty"] == difficulty}
        observed = [row for row in audited if row["episode"].get("taskId") in expected]
        valid = [row["episode"] for row in observed if not row["issues"]]
        if expected or observed:
            by_difficulty[difficulty] = {
                "expected": len(expected),
                "observed": len(observed),
                "valid": len(valid),
                "invalid": sum(bool(row["issues"]) for row in observed),
                "missing": len(expected - {str(row["episode"].get("taskId")) for row in observed}),
                **score_summary(valid),
            }

    image_totals: dict[str, int] | None = None
    attempted_image_totals: dict[str, int] | None = None
    if args.mode == "semantic-v1":
        image_totals = {
            key: sum(int(row["semanticPolicy"][key]) for row in valid_rows)
            for key in IMAGE_COUNTERS
        }
        attempted_image_totals = {
            key: sum(
                int(row["episode"]["semanticPolicy"][key])
                for row in audited
                if isinstance(row["episode"].get("semanticPolicy"), dict)
                and is_integer(row["episode"]["semanticPolicy"].get(key))
            )
            for key in IMAGE_COUNTERS
        }
    report = {
        "arm": str(args.arm),
        "mode": args.mode,
        "pool": str(args.pool),
        "pool_sha256": pool_sha,
        "planned_denominator": len(expected_ids),
        "publishable": not arm_issues,
        "arm_issues": sorted(set(arm_issues)),
        "missing_task_ids": missing_ids,
        "extra_task_ids": extra_ids,
        "duplicate_task_ids": sorted(duplicate_ids),
        "invalid_or_incomplete_episodes": [
            {
                "task_id": row["episode"].get("taskId"),
                "source": row["source"],
                "reasons": row["issues"],
            }
            for row in invalid
        ],
        "score": score_summary(valid_rows),
        "difficulty": by_difficulty,
        "usage_valid_only": usage(valid_rows, semantic=args.mode == "semantic-v1"),
        "usage_all_attempted": usage([row["episode"] for row in audited], semantic=args.mode == "semantic-v1"),
        "zero_image_counters_valid_only": image_totals,
        "zero_image_counters_all_attempted": attempted_image_totals,
        "failures_all_attempted": trace_metrics([row["episode"] for row in audited]),
        "valid_zero_score_episodes": [
            episode_failure_detail(row, metadata)
            for row in valid_rows if float(row["score"]) <= 1e-9
        ],
        "valid_partial_score_episodes": [
            episode_failure_detail(row, metadata)
            for row in valid_rows if 1e-9 < float(row["score"]) < 1 - 1e-9
        ],
        "stop_reasons_valid_only": dict(collections.Counter(
            str(row.get("stopReason")) for row in valid_rows
        )),
        "budget_hits_valid_only": sum(
            row.get("toolCalls") == args.max_tool_calls for row in valid_rows
        ),
        "wall_time": {
            "episode_elapsed_sum_is_exact_but_not_parallel_arm_wall": True,
            **log_wall_metrics(args.arm),
        },
        "identity": {
            "models": sorted({str(payload.get("model")) for _, payload in payloads}),
            "variants": sorted({str(payload.get("variant")) for _, payload in payloads}),
            "harness_revisions": sorted({str(payload.get("harnessRevision")) for _, payload in payloads}),
            "runtime_manifest_hashes": sorted({
                str(payload.get("runtimeManifestSha256")) for _, payload in payloads
                if present(payload.get("runtimeManifestSha256"))
            }),
            "shard_execution": [
                {
                    "source": path.name,
                    "concurrency": payload.get("concurrency"),
                    "planned_tasks": len(payload.get("results", ())),
                    "completed": payload.get("completed"),
                }
                for path, payload in payloads
            ],
        },
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if report["publishable"] else 2)


if __name__ == "__main__":
    main()
