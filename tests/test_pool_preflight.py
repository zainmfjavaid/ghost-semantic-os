#!/usr/bin/env python3
"""Environment eligibility is a gate, never a model score."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "infra/audit_pool_preflight.py"


def run(*names: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(AUDIT), *(str(REPO / "pools" / name) for name in names)],
        check=False,
        capture_output=True,
        text=True,
    )


valid = run(
    "browser_holdout_a_v2_runnable.json",
    "browser_holdout_b_v2_runnable.json",
    "desktop_ood_v2.json",
)
assert valid.returncode == 0, valid.stderr
valid_payload = json.loads(valid.stdout)
assert [report["status"] for report in valid_payload["pools"]] == [
    "pass", "pass", "pass",
]
assert [report["task_count"] for report in valid_payload["pools"]] == [11, 11, 12]

invalid = run("browser_holdout_a_v2.json", "browser_holdout_b_v2.json")
assert invalid.returncode == 2, invalid.stderr
invalid_payload = json.loads(invalid.stdout)
assert [report["failure_counts"]["googledrive_setup"]
        for report in invalid_payload["pools"]] == [3, 4]
assert [report["failure_counts"]["outside_published_nogdrive_matrix"]
        for report in invalid_payload["pools"]] == [3, 4]

for report in valid_payload["pools"] + invalid_payload["pools"]:
    assert report["inspection_fields"] == [
        "id", "config[].type", "published_nogdrive_membership",
    ]

print("pool preflight tests passed")
