#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import os

ROOT = Path(__file__).resolve().parents[1]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build() -> dict[str, object]:
    patch_manifest = json.loads((ROOT / "patches/osworld/manifest.json").read_text())
    return {
        "schema_version": 1,
        "release": (ROOT / "VERSION").read_text().strip(),
        "license": "Apache-2.0",
        "supported_runtime": "semantic-simple-v1",
        "semantic_protocol_version": "1.0",
        "model_facing_tools": ["read_computer", "computer_click", "computer_type"],
        "osworld": {
            "upstream": patch_manifest["upstream"],
            "base_commit": patch_manifest["base_commit"],
            "base_tree": patch_manifest["base_tree"],
            "base_snapshot": patch_manifest["base_snapshot"],
            "canonical_final_commit": patch_manifest["canonical_final_commit"],
            "final_tree": patch_manifest["final_tree"],
            "patch_manifest_sha256": sha(ROOT / "patches/osworld/manifest.json"),
        },
        "docker_image": "happysixd/osworld-docker@sha256:0e6497a9295647cf05bf2b2af522fdd79bdeba2737595259cab310a3bcf6baa9",
        "dependencies": {
            "node_lock_sha256": sha(ROOT / "harness/package-lock.json"),
            "python_host_lock_sha256": sha(ROOT / "requirements/host.lock"),
            "python_ci_lock_sha256": sha(ROOT / "requirements/ci.lock"),
        },
        "runtime_file_manifest_sha256": sha(ROOT / "infra/semantic_runtime_files.txt"),
        "server_file_manifest_sha256": sha(ROOT / "infra/semantic_server_runtime_files.txt"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    destination = ROOT / "release-manifest.json"
    encoded = json.dumps(build(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not destination.is_file() or destination.read_text() != encoded:
            print("release-manifest.json is stale")
            return 1
        print("release-manifest.json is current")
        return 0
    with tempfile.NamedTemporaryFile("w", dir=ROOT, delete=False) as handle:
        handle.write(encoded)
        temporary = Path(handle.name)
    os.replace(temporary, destination)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
