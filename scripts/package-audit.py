#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {
    ".git", ".ci-venv", ".venv", "node_modules", "OSWorld", "results",
    "results_gcp",
}
TEXT_SUFFIXES = {"", ".md", ".txt", ".json", ".py", ".ts", ".sh", ".yml", ".yaml", ".toml"}
FORBIDDEN = {
    "developer_home": re.compile(r"/Users/zainj/ghost/projects"),
    "private_gcp_project": re.compile(r"ghost-prod-488706"),
    "private_gcp_ip": re.compile(r"(?:35\.192\.24\.229|34\.45\.27\.217|34\.173\.77\.100|35\.255\.141\.35)"),
    "secret_assignment": re.compile(r"(?i)(?:api[_-]?key|token|secret)\s*[=:]\s*['\"][A-Za-z0-9_-]{20,}"),
}
REQUIRED_EXECUTABLES = [
    "semantic-os", "scripts/bootstrap.sh", "scripts/bootstrap-osworld.sh",
    "scripts/install-host.sh", "scripts/doctor.sh", "scripts/test.sh",
]


def iter_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_PARTS for part in path.parts):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        yield path


def main() -> int:
    errors: list[str] = []
    for rel in REQUIRED_EXECUTABLES:
        path = ROOT / rel
        if not path.is_file():
            errors.append(f"missing required file: {rel}")
        elif not path.stat().st_mode & stat.S_IXUSR:
            errors.append(f"not executable: {rel}")

    for path in iter_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(ROOT)
        for name, pattern in FORBIDDEN.items():
            if pattern.search(text):
                errors.append(f"{name}: {rel}")

    manifest_path = ROOT / "patches/osworld/manifest.json"
    if not manifest_path.is_file():
        errors.append("missing patches/osworld/manifest.json")
    else:
        manifest = json.loads(manifest_path.read_text())
        snapshot = manifest.get("base_snapshot", {})
        snapshot_path = ROOT / "patches/osworld" / snapshot.get("file", "")
        snapshot_actual = (
            hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            if snapshot_path.is_file() else "missing"
        )
        if snapshot_actual != snapshot.get("sha256"):
            errors.append("OSWorld base snapshot hash mismatch")
        for entry in manifest.get("patches", []):
            path = ROOT / "patches/osworld" / entry["file"]
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
            if actual != entry["sha256"]:
                errors.append(f"patch hash mismatch: {entry['file']}")

    for list_name in ["infra/semantic_runtime_files.txt", "infra/semantic_server_runtime_files.txt"]:
        for line in (ROOT / list_name).read_text().splitlines():
            rel = line.strip()
            if rel and not (ROOT / rel).exists():
                errors.append(f"runtime manifest references missing file: {rel}")

    result = {"ok": not errors, "files_scanned": sum(1 for _ in iter_text_files()), "errors": errors}
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
