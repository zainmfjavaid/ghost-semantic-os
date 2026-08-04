#!/usr/bin/env python3
"""Model-free quality canary for the semantic-simple-v1 facade.

The runner replays explicit read/click/type trajectories against a real
OSWorld episode, preserves every model-visible character, audits the text-only
surface, proves zero-image state, then cleans up.  It never calls a model
provider.  These are interaction-surface canaries rather than attempts to solve
the source OSWorld tasks, so evaluator execution is skipped unless the caller
explicitly opts in with ``--evaluate``.

Trajectory fixture shape::

    {
      "name": "browser form",
      "task_path": "/home/zain/osworld/OSWorld/evaluation_examples/...json",
      "steps": [
        {"op": "read", "query": "Search"},
        {"op": "click", "match": {"contains": "Search flights"}},
        {"op": "type", "match": {"contains": "Destination"}, "text": "hello"}
      ]
    }

Optional per-step ``expect`` supports ``ok``, ``contains``, ``not_contains``,
``active_surface``, ``active_header_contains``, ``active_header_not_contains``,
``min_surface_count``, ``min_element_count``, and ``min_returned_elements``.

Click/type may instead provide an explicit ``element`` ID. Dynamic ``match``
never queries hidden state: it requires one literal match among capability
lines in the immediately preceding exact model-visible render.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests


MAX_RENDERED_CHARS = 10_000
ZERO_IMAGE_FIELDS = (
    "screenshots_captured",
    "image_parts_created",
    "image_parts_in_session",
    "image_parts_sent",
    "pixels_sent_to_policy_model",
    "visual_sidecar_calls",
)
FORBIDDEN_JARGON: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("adapter identifier", re.compile(r"\badapter_id\b|\badapter=", re.IGNORECASE)),
    ("resource protocol", re.compile(r"\bunknown_resource\b|\bresource=", re.IGNORECASE)),
    ("opaque ref", re.compile(r"\b(?:entity|native|parent)_ref\b|\bref=", re.IGNORECASE)),
    ("semantic revision", re.compile(r"\brevision_conflict\b|\brevision=", re.IGNORECASE)),
    ("kernel receipt", re.compile(r"\b(?:observation|receipt|verification)_id\b", re.IGNORECASE)),
    ("data handle", re.compile(r"\b(?:data|overflow|collection)_handle\b", re.IGNORECASE)),
    ("kernel identity", re.compile(r"semantic\.kernel|\b[a-z0-9_.-]+@[0-9]+(?:\.[0-9]+)+\b", re.IGNORECASE)),
)
RENDERED_CAPABILITY = re.compile(r"^\s*\[([A-Z]+(?:[1-9][0-9]*)?)\]\s+(.+)$")
ACTIVE_SURFACE_HEADER = re.compile(r"^Active Surface(?:\s+(.+))?$", re.MULTILINE)


class CanaryFailure(RuntimeError):
    """A typed, human-readable canary failure."""


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def audit_rendered_text(text: Any) -> dict[str, Any]:
    """Return deterministic quality metrics for exactly what the model sees."""

    rendered = text if isinstance(text, str) else ""
    lines = rendered.splitlines()
    nonempty = [line.strip() for line in lines if line.strip()]
    frequencies: dict[str, int] = {}
    for line in nonempty:
        frequencies[line] = frequencies.get(line, 0) + 1
    duplicates = [
        {"line": line, "count": count}
        for line, count in sorted(frequencies.items())
        if count > 1
    ]
    jargon = [
        {"kind": label, "matches": sorted(set(match.group(0) for match in pattern.finditer(rendered)))}
        for label, pattern in FORBIDDEN_JARGON
        if pattern.search(rendered)
    ]
    problems: list[str] = []
    if not rendered.strip():
        problems.append("rendered text is empty")
    if len(rendered) > MAX_RENDERED_CHARS:
        problems.append(
            f"rendered text is oversized ({len(rendered)} > {MAX_RENDERED_CHARS} characters)"
        )
    if duplicates:
        problems.append(f"rendered text contains {len(duplicates)} duplicated non-empty lines")
    if jargon:
        problems.append(f"rendered text exposes {len(jargon)} forbidden protocol-jargon classes")
    return {
        "characters": len(rendered),
        "estimated_tokens": math.ceil(len(rendered) / 4),
        "lines": len(lines),
        "nonempty_lines": len(nonempty),
        "duplicate_lines": duplicates,
        "forbidden_jargon": jargon,
        "empty": not bool(rendered.strip()),
        "oversized": len(rendered) > MAX_RENDERED_CHARS,
        "problems": problems,
    }


def _expect_strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise CanaryFailure(f"expect.{field} must be a string or list of strings")


def check_expectations(result: Mapping[str, Any], expected: Any) -> list[str]:
    if expected is None:
        return []
    if not isinstance(expected, Mapping):
        raise CanaryFailure("step expect must be an object")
    failures: list[str] = []
    text = result.get("text") if isinstance(result.get("text"), str) else ""
    if "ok" in expected and result.get("ok") is not expected["ok"]:
        failures.append(f"expected ok={expected['ok']!r}, got {result.get('ok')!r}")
    for needle in _expect_strings(expected.get("contains"), "contains"):
        if needle not in text:
            failures.append(f"rendered text does not contain {needle!r}")
    for needle in _expect_strings(expected.get("not_contains"), "not_contains"):
        if needle in text:
            failures.append(f"rendered text unexpectedly contains {needle!r}")
    active_match = ACTIVE_SURFACE_HEADER.search(text)
    active_header = active_match.group(0) if active_match is not None else ""
    for needle in _expect_strings(
        expected.get("active_header_contains"), "active_header_contains",
    ):
        if needle not in active_header:
            failures.append(
                f"active surface header {active_header!r} does not contain {needle!r}"
            )
    for needle in _expect_strings(
        expected.get("active_header_not_contains"), "active_header_not_contains",
    ):
        if needle in active_header:
            failures.append(
                f"active surface header {active_header!r} unexpectedly contains {needle!r}"
            )
    if "active_surface" in expected and result.get("active_surface") != expected["active_surface"]:
        failures.append(
            f"expected active_surface={expected['active_surface']!r}, "
            f"got {result.get('active_surface')!r}"
        )
    for expected_name, result_name in (
        ("min_surface_count", "surface_count"),
        ("min_element_count", "element_count"),
        ("min_returned_elements", "returned_elements"),
    ):
        if expected_name in expected:
            actual = result.get(result_name)
            minimum = expected[expected_name]
            if not isinstance(minimum, int) or minimum < 0:
                raise CanaryFailure(f"expect.{expected_name} must be a non-negative integer")
            if not isinstance(actual, int) or actual < minimum:
                failures.append(f"expected {result_name}>={minimum}, got {actual!r}")
    return failures


def resolve_rendered_capability(rendered_text: str | None, contains: Any) -> dict[str, str]:
    """Resolve one public capability using only the immediately prior render."""

    if not isinstance(contains, str) or not contains:
        raise CanaryFailure("match.contains must be a non-empty literal string")
    if not isinstance(rendered_text, str):
        raise CanaryFailure("dynamic action requires an immediately prior rendered read")
    candidates: list[dict[str, str]] = []
    for line in rendered_text.splitlines():
        matched = RENDERED_CAPABILITY.match(line)
        if matched and contains in line:
            candidates.append({
                "contains": contains,
                "line": line,
                "element": matched.group(1),
            })
    if not candidates:
        raise CanaryFailure(
            f"dynamic capability match {contains!r} found zero rendered capability lines"
        )
    if len(candidates) > 1:
        rendered_candidates = "; ".join(candidate["line"] for candidate in candidates[:8])
        raise CanaryFailure(
            f"dynamic capability match {contains!r} is ambiguous: "
            f"{len(candidates)} rendered lines ({rendered_candidates})"
        )
    return candidates[0]


def audit_public_id_stability(previous_text: str | None, current_text: str) -> dict[str, Any]:
    """Compare unchanged, uniquely named public rows across adjacent renders.

    This intentionally knows nothing about private refs. If the same unique
    surface or element line is rendered twice without a semantic change, its
    public A/B or A1/B10 capability must not be rebound.
    """

    def rows(text: str | None) -> dict[str, list[str]]:
        output: dict[str, list[str]] = {}
        for line in (text or "").splitlines():
            matched = RENDERED_CAPABILITY.match(line)
            if matched is None:
                continue
            public_id, label = matched.groups()
            if public_id.isalpha():
                label = re.sub(r"\s+—\s+active(?:\s+—|$)", "", label)
            output.setdefault(label, []).append(public_id)
        return output

    before = rows(previous_text)
    after = rows(current_text)
    mismatches: list[dict[str, str]] = []
    comparable = 0
    for label in sorted(before.keys() & after.keys()):
        if len(before[label]) != 1 or len(after[label]) != 1:
            continue
        comparable += 1
        if before[label][0] != after[label][0]:
            mismatches.append({
                "label": label,
                "before": before[label][0],
                "after": after[label][0],
            })
    return {
        "comparable_rows": comparable,
        "mismatches": mismatches,
        "pass": not mismatches,
    }


@dataclass
class SimpleCanaryClient:
    base_url: str
    request_timeout: float = 330.0
    create_timeout: float = 900.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.session = requests.Session()

    def _request(
        self, method: str, path: str, *, payload: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=dict(payload) if payload is not None else None,
            timeout=timeout or self.request_timeout,
        )
        if not response.ok:
            raise CanaryFailure(
                f"{method} {path} returned HTTP {response.status_code}: {response.text[:1000]}"
            )
        try:
            value = response.json()
        except ValueError as error:
            raise CanaryFailure(f"{method} {path} returned non-JSON data") from error
        if not isinstance(value, dict):
            raise CanaryFailure(f"{method} {path} returned a non-object JSON value")
        return value

    def create(self, task_path: str, max_tool_calls: int) -> dict[str, Any]:
        return self._request("POST", "/episodes", payload={
            "task_path": task_path,
            "runtime": "semantic-simple-v1",
            "require_screenshot": False,
            "initial_observation": False,
            "max_tool_calls": max_tool_calls,
        }, timeout=self.create_timeout)

    def simple(self, episode_id: str, operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", f"/episodes/{episode_id}/simple/{operation}", payload=payload,
        )

    def state(self, episode_id: str) -> dict[str, Any]:
        return self._request("GET", f"/episodes/{episode_id}/semantic/state")

    def evaluate(self, episode_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/episodes/{episode_id}/evaluate", payload={}, timeout=300,
        )

    def close_episode(self, episode_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE", f"/episodes/{episode_id}", timeout=180,
        )


def _step_payload(
    step: Mapping[str, Any], previous_render: str | None,
) -> tuple[str, dict[str, Any], dict[str, str] | None]:
    operation = step.get("op")
    if operation == "read":
        # Mirror the model-facing read_computer schema exactly.  The envserver
        # has an internal limit field for compatibility/testing, but giving a
        # model-free trajectory that extra knob would audit a stronger tool
        # than the policy model actually receives.
        allowed = {"query", "within", "cursor"}
        unexpected = sorted(
            set(step) - {"op", "expect", *allowed}
        )
        if unexpected:
            raise CanaryFailure(
                f"read step contains non-public fields: {unexpected!r}"
            )
        payload = {key: step[key] for key in allowed if key in step}
        return "read", payload, None
    if operation == "click":
        element = step.get("element")
        match = step.get("match")
        if (element is None) == (match is None):
            raise CanaryFailure("click step requires exactly one of element or match")
        if match is not None:
            if not isinstance(match, Mapping):
                raise CanaryFailure("click match must be an object")
            resolved = resolve_rendered_capability(previous_render, match.get("contains"))
            return "click", {"element": resolved["element"]}, resolved
        if not isinstance(element, str) or not element.strip():
            raise CanaryFailure("click step requires an explicit non-empty element ID")
        return "click", {"element": element}, None
    if operation == "type":
        element = step.get("element")
        match = step.get("match")
        text = step.get("text")
        if not isinstance(text, str):
            raise CanaryFailure("type step requires string text")
        if (element is None) == (match is None):
            raise CanaryFailure("type step requires exactly one of element or match")
        if match is not None:
            if not isinstance(match, Mapping):
                raise CanaryFailure("type match must be an object")
            resolved = resolve_rendered_capability(previous_render, match.get("contains"))
            return "type", {"element": resolved["element"], "text": text}, resolved
        if not isinstance(element, str) or not element.strip():
            raise CanaryFailure("type step requires an explicit non-empty element ID")
        return "type", {"element": element, "text": text}, None
    raise CanaryFailure("step op must be read, click, or type")


def _validate_fixture(fixture: Any, source: Path) -> dict[str, Any]:
    if not isinstance(fixture, dict):
        raise CanaryFailure(f"trajectory {source} must contain a JSON object")
    if not isinstance(fixture.get("name"), str) or not fixture["name"].strip():
        raise CanaryFailure(f"trajectory {source} requires a non-empty name")
    if not isinstance(fixture.get("task_path"), str) or not fixture["task_path"].strip():
        raise CanaryFailure(f"trajectory {source} requires a non-empty task_path")
    steps = fixture.get("steps")
    if not isinstance(steps, list) or not steps:
        raise CanaryFailure(f"trajectory {source} requires a non-empty steps array")
    if not all(isinstance(step, dict) for step in steps):
        raise CanaryFailure(f"trajectory {source} steps must be objects")
    max_tool_calls = fixture.get("max_tool_calls", 100)
    if not isinstance(max_tool_calls, int) or not 1 <= max_tool_calls <= 100:
        raise CanaryFailure(f"trajectory {source} max_tool_calls must be 1..100")
    return fixture


def _zero_image_audit(state: Mapping[str, Any]) -> dict[str, Any]:
    counters = {field: state.get(field) for field in ZERO_IMAGE_FIELDS}
    failures = [
        f"{field}={value!r}"
        for field, value in counters.items()
        if value != 0
    ]
    return {"counters": counters, "pass": not failures, "failures": failures}


def _safe_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return slug or "trajectory"


def _write_human_review(directory: Path, bundle: Mapping[str, Any]) -> None:
    zero_image = bundle.get("zero_image") or {}
    totals = bundle.get("totals") or {}
    lines = [
        f"# semantic-simple-v1 canary — {bundle.get('name', 'unnamed')}",
        "",
        f"Status: **{bundle.get('status', 'unknown')}**",
        f"Task: `{bundle.get('task_path', '')}`",
        f"Episode: `{bundle.get('episode_id', '')}`",
        "",
        "## Summary",
        "",
        f"- Steps attempted: {len(bundle.get('steps', []))}",
        f"- Rendered characters: {totals.get('characters', 0)}",
        f"- Estimated model-input tokens: {totals.get('estimated_tokens', 0)}",
        f"- Maximum surfaces: {totals.get('max_surface_count', 0)}",
        f"- Maximum elements: {totals.get('max_element_count', 0)}",
        f"- Zero-image state: {zero_image.get('pass', False)}",
        "",
        "## Findings",
        "",
    ]
    failures = bundle.get("failures") or []
    lines.extend((f"- {failure}" for failure in failures) if failures else ["- None"])
    lines.extend(("", "## Step review", ""))
    for step in bundle.get("steps", []):
        lines.extend((
            f"### {step['index']:03d} — {step['operation']}",
            "",
            f"- Exact text: [{step['text_file']}]({step['text_file']})",
            f"- Characters / estimated tokens: {step['audit']['characters']} / "
            f"{step['audit']['estimated_tokens']}",
            f"- Surfaces / elements / returned: {step.get('surface_count')} / "
            f"{step.get('element_count')} / {step.get('returned_elements')}",
            f"- Active surface: `{step.get('active_surface')}`",
            f"- Duplicate lines: {len(step['audit']['duplicate_lines'])}",
            f"- Forbidden-jargon classes: {len(step['audit']['forbidden_jargon'])}",
            "",
        ))
    directory.joinpath("human-review.md").write_text("\n".join(lines), encoding="utf-8")


def run_trajectory(
    fixture_path: Path,
    output_root: Path,
    client: SimpleCanaryClient,
    *, fail_on_quality: bool = True, run_evaluator: bool = False,
) -> dict[str, Any]:
    fixture = _validate_fixture(
        json.loads(fixture_path.read_text(encoding="utf-8")), fixture_path,
    )
    directory = output_root / _safe_name(str(fixture["name"]))
    text_directory = directory / "rendered-text"
    text_directory.mkdir(parents=True, exist_ok=True)
    started = time.time()
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "runtime": "semantic-simple-v1",
        "model_calls": 0,
        "fixture": str(fixture_path.resolve()),
        "name": fixture["name"],
        "task_path": fixture["task_path"],
        "status": "failed",
        "episode_id": None,
        "created": None,
        "steps": [],
        "zero_image": None,
        "evaluation": {
            "status": "skipped",
            "reason": (
                "model-free facade canaries validate the public interaction surface, "
                "not completion of the source OSWorld task"
            ),
        },
        "cleanup": None,
        "failures": [],
    }
    episode_id: str | None = None
    try:
        created = client.create(str(fixture["task_path"]), int(fixture.get("max_tool_calls", 100)))
        episode_id = created.get("episode_id")
        if not isinstance(episode_id, str) or not episode_id:
            raise CanaryFailure("episode creation returned no episode_id")
        bundle["episode_id"] = episode_id
        bundle["created"] = created
        if created.get("screenshots_captured") not in (None, 0):
            bundle["failures"].append(
                f"episode creation reported screenshots_captured={created.get('screenshots_captured')!r}"
            )
        previous_render: str | None = None
        for index, raw_step in enumerate(fixture["steps"], start=1):
            operation, payload, resolution = _step_payload(raw_step, previous_render)
            result = client.simple(episode_id, operation, payload)
            rendered = result.get("text") if isinstance(result.get("text"), str) else ""
            text_file = f"rendered-text/{index:03d}-{operation}.txt"
            directory.joinpath(text_file).write_text(rendered, encoding="utf-8")
            audit = audit_rendered_text(result.get("text"))
            id_stability = audit_public_id_stability(previous_render, rendered)
            expectation_failures = check_expectations(result, raw_step.get("expect"))
            step_failures = list(expectation_failures)
            if result.get("ok") is not True:
                step_failures.append(f"simple/{operation} returned ok={result.get('ok')!r}")
            if fail_on_quality:
                step_failures.extend(audit["problems"])
                step_failures.extend(
                    "public ID changed for unchanged row "
                    f"{item['label']!r}: {item['before']} -> {item['after']}"
                    for item in id_stability["mismatches"]
                )
            record = {
                "index": index,
                "operation": operation,
                "request": payload,
                "resolution": resolution,
                "response": result,
                "text_file": text_file,
                "audit": audit,
                "id_stability": id_stability,
                "active_surface": result.get("active_surface"),
                "surface_count": result.get("surface_count"),
                "element_count": result.get("element_count"),
                "returned_elements": result.get("returned_elements"),
                "next_cursor": result.get("next_cursor"),
                "expectation_failures": expectation_failures,
                "failures": step_failures,
            }
            bundle["steps"].append(record)
            bundle["failures"].extend(
                f"step {index} {operation}: {failure}" for failure in step_failures
            )
            previous_render = rendered
        state = client.state(episode_id)
        bundle["semantic_state"] = state
        bundle["zero_image"] = _zero_image_audit(state)
        bundle["failures"].extend(
            f"zero-image: {failure}" for failure in bundle["zero_image"]["failures"]
        )
    except Exception as error:  # Preserve a review bundle even on transport/setup failure.
        bundle["failures"].append(f"execution: {type(error).__name__}: {error}")
    finally:
        if episode_id:
            if run_evaluator:
                try:
                    bundle["evaluation"] = {
                        "status": "completed",
                        "result": client.evaluate(episode_id),
                    }
                    if bundle["evaluation"]["result"].get("error"):
                        bundle["failures"].append(
                            f"evaluation: {bundle['evaluation']['result']['error']}"
                        )
                except Exception as error:
                    bundle["evaluation"] = {
                        "status": "failed",
                        "error": f"{type(error).__name__}: {error}",
                    }
                    bundle["failures"].append(
                        f"evaluation: {type(error).__name__}: {error}"
                    )
            try:
                bundle["cleanup"] = client.close_episode(episode_id)
                cleanup_errors = bundle["cleanup"].get("errors") or []
                bundle["failures"].extend(
                    f"cleanup: {message}" for message in cleanup_errors
                )
            except Exception as error:
                bundle["failures"].append(f"cleanup: {type(error).__name__}: {error}")
    steps = bundle["steps"]
    bundle["totals"] = {
        "characters": sum(step["audit"]["characters"] for step in steps),
        "estimated_tokens": sum(step["audit"]["estimated_tokens"] for step in steps),
        "max_surface_count": max(
            (step["surface_count"] for step in steps if isinstance(step["surface_count"], int)),
            default=0,
        ),
        "max_element_count": max(
            (step["element_count"] for step in steps if isinstance(step["element_count"], int)),
            default=0,
        ),
    }
    bundle["elapsed_seconds"] = round(time.time() - started, 3)
    bundle["status"] = "passed" if not bundle["failures"] else "failed"
    directory.joinpath("bundle.json").write_text(_json_text(bundle), encoding="utf-8")
    _write_human_review(directory, bundle)
    return bundle


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="warm OSWorld envserver URL")
    parser.add_argument(
        "--trajectory", action="append", required=True, type=Path,
        help="trajectory JSON fixture; repeat for multiple canaries",
    )
    parser.add_argument("--output", required=True, type=Path, help="human-review bundle root")
    parser.add_argument(
        "--report-quality-only", action="store_true",
        help="record text-quality findings without failing the canary on them",
    )
    parser.add_argument(
        "--evaluate", action="store_true",
        help=(
            "explicitly run the source OSWorld evaluator after the facade canary; "
            "disabled by default because these trajectories do not solve the source task"
        ),
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    args.output.mkdir(parents=True, exist_ok=True)
    client = SimpleCanaryClient(args.base_url)
    bundles = [
        run_trajectory(
            trajectory, args.output, client,
            fail_on_quality=not args.report_quality_only,
            run_evaluator=args.evaluate,
        )
        for trajectory in args.trajectory
    ]
    summary = {
        "schema_version": 1,
        "runtime": "semantic-simple-v1",
        "model_calls": 0,
        "passed": sum(bundle["status"] == "passed" for bundle in bundles),
        "failed": sum(bundle["status"] != "passed" for bundle in bundles),
        "trajectories": [
            {
                "name": bundle["name"],
                "status": bundle["status"],
                "episode_id": bundle["episode_id"],
                "failures": bundle["failures"],
            }
            for bundle in bundles
        ],
    }
    args.output.joinpath("summary.json").write_text(_json_text(summary), encoding="utf-8")
    print(_json_text(summary), end="")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
