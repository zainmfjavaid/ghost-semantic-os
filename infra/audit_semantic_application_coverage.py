#!/usr/bin/env python3
"""Prove every Linux OSWorld application family has a semantic disposition.

This build-time audit reads only the static application inventory and adapter
descriptors.  It never reads task instructions, evaluator rules, or expected
state, and it is not imported by the episode runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from envserver.semantic.remaining_apps import create_remaining_application_adapters
from guest_agent.native_app_bridges import NativeAppBridgeDispatcher


DEFAULT_INVENTORY = ROOT / "OSWorld" / "protocol" / "linux-app-inventory.json"
DEFAULT_OUTPUT = ROOT / "protocol" / "semantic-v1-application-coverage.json"

CORE_ADAPTERS = frozenset({
    "browser.cdp@1",
    "chrome.semantic@1",
    "guest-os@1",
    "universal-atspi@1",
    "libreoffice.uno@1",
})
EXPLICIT_INTEGRATION_GAPS = frozenset({
    "thunderbird-extension@1",
    "vscode-ghost-extension@1",
    "gimp-pdb@1",
    "picard-media@1",
})


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(inventory_path: Path) -> dict[str, Any]:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    remaining = {
        adapter.adapter_id: adapter.descriptor()
        for adapter in create_remaining_application_adapters(None)
    }
    native = set(NativeAppBridgeDispatcher().bridges)
    known = CORE_ADAPTERS | set(remaining)
    families: list[dict[str, Any]] = []
    missing: set[str] = set()
    for family in inventory["canonical_families"]:
        adapters: list[dict[str, Any]] = []
        for adapter_id in family["adapter_ids"]:
            if adapter_id not in known:
                missing.add(adapter_id)
                status = "missing"
            elif adapter_id in EXPLICIT_INTEGRATION_GAPS:
                status = "representation_gap"
            elif adapter_id in CORE_ADAPTERS or adapter_id in native:
                status = "implemented"
            else:
                status = "representation_gap"
            descriptor = remaining.get(adapter_id, {})
            adapters.append({
                "adapter_id": adapter_id,
                "status": status,
                "native_bridge": adapter_id in native,
                "known_representation_gaps": descriptor.get(
                    "known_representation_gaps", []
                ),
            })
        families.append({
            "canonical_id": family["canonical_id"],
            "task_count": family["task_count"],
            "adapters": adapters,
            "family_representation_gaps": family.get("representation_gaps", []),
            "disposition": (
                "missing" if any(item["status"] == "missing" for item in adapters)
                else "representation_gap"
                if any(item["status"] == "representation_gap" for item in adapters)
                else "implemented"
            ),
        })
    if missing:
        raise SystemExit(f"unmapped application adapter IDs: {sorted(missing)!r}")
    return {
        "schema_version": 1,
        "runtime": "semantic-v1",
        "inventory_sha256": sha256(inventory_path),
        "inventory_task_count": inventory["summary"]["task_count"]
        if "task_count" in inventory["summary"]
        else len(inventory["tasks"]),
        "families": families,
        "summary": {
            "family_count": len(families),
            "implemented_families": sum(
                family["disposition"] == "implemented" for family in families
            ),
            "representation_gap_families": sum(
                family["disposition"] == "representation_gap" for family in families
            ),
            "missing_families": sum(
                family["disposition"] == "missing" for family in families
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    payload = json.dumps(build(args.inventory), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != payload:
            raise SystemExit(f"application coverage artifact is stale: {args.output}")
        print(f"PASS semantic application coverage is current: {args.output}")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    print(f"WROTE {args.output}")


if __name__ == "__main__":
    main()
