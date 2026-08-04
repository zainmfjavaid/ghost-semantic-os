#!/usr/bin/env python3
"""Build the immutable, secret-free identity for an OSWorld runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SEMANTIC_RUNTIMES = frozenset({
    "semantic-v1", "semantic-plus-v1", "semantic-simple-v1",
})
RUNTIMES = frozenset({"vision-v15", "hybrid-v15", *SEMANTIC_RUNTIMES})
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def re_full_sha256(value: str) -> bool:
    return bool(SHA256.fullmatch(value))


def git(repo: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *arguments], text=True,
    ).strip()


def aggregate(paths: list[Path], root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def aggregate_named(paths: list[Path], root: Path) -> str:
    """Hash a portable bundle by its names relative to its installation root."""

    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def load_model_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"status": "not_supplied"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("model config must be a JSON object")
    forbidden = {"api_key", "token", "authorization", "password", "secret"}
    bad = sorted(key for key in payload if key.casefold() in forbidden)
    if bad:
        raise SystemExit(f"model config contains secret-bearing keys: {bad}")
    return payload


def source_git(
    root: Path, override: str | None, *arguments: str,
) -> str:
    if override:
        return override
    try:
        return git(root, *arguments)
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit(
            f"source root is not a Git checkout; explicit identity is required: {root}"
        ) from None


def build(args: argparse.Namespace) -> dict[str, Any]:
    if args.runtime not in RUNTIMES:
        raise SystemExit(f"unsupported runtime: {args.runtime}")
    source_root = Path(args.source_root).resolve() if args.source_root else REPO
    environment_root = (
        Path(args.environment_root).resolve()
        if args.environment_root else source_root
    )
    files_manifest = source_root / "infra" / (
        "semantic_runtime_files.txt"
        if args.runtime in SEMANTIC_RUNTIMES else "runtime_v15_files.txt"
    )
    if not files_manifest.is_file():
        raise SystemExit(f"runtime file manifest is absent: {files_manifest}")
    runtime_paths = [
        source_root / line.strip()
        for line in files_manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    missing = [path for path in runtime_paths if not path.is_file()]
    if missing:
        raise SystemExit(f"runtime manifest paths missing: {missing}")
    task_pool = Path(args.task_pool).resolve() if args.task_pool else None
    if args.task_pool_sha256 and not re_full_sha256(args.task_pool_sha256):
        raise SystemExit("--task-pool-sha256 must be 64 lowercase hex characters")
    evaluator_paths = sorted(
        (source_root / "OSWorld/desktop_env/evaluators").rglob("*.py")
    )
    if not evaluator_paths:
        raise SystemExit(f"no evaluator sources found below {source_root}")
    freeze = environment_root / ".python-freeze.txt"
    environment_state = environment_root / ".environment_state"
    model = load_model_config(Path(args.model_config).resolve() if args.model_config else None)
    files = {
        path.relative_to(source_root).as_posix(): sha256_file(path)
        for path in runtime_paths
    }
    guest_root = source_root / "guest_agent"
    guest_bundle_paths = [
        guest_root / "semantic_agent.py",
        guest_root / "native_app_bridges.py",
        guest_root / "package_installer.py",
    ] if args.runtime in SEMANTIC_RUNTIMES else []
    integration_paths = [
        path for path in runtime_paths
        if path in guest_bundle_paths
        or path.name in {
            "guest_semantic.py", "application_adapter.py", "remaining_apps.py",
            "thunderbird_adapter.py", "vscode_adapter.py", "vlc_adapter.py",
            "pdf_adapter.py", "terminal_adapter.py", "gimp_adapter.py",
            "media_adapter.py", "chrome_adapter.py", "browser_adapter.py",
        }
    ]
    parent_commit = source_git(
        source_root, args.parent_commit, "rev-parse", "HEAD"
    )
    nested_commit = source_git(
        source_root / "OSWorld", args.nested_osworld_commit, "rev-parse", "HEAD"
    )
    parent_branch = args.parent_branch or (
        "feature/semantic-os-v1" if args.runtime in SEMANTIC_RUNTIMES
        else "freeze/v15-harness-2026-08-02"
    )
    nested_branch = args.nested_osworld_branch or (
        "ghost/semantic-os-v1" if args.runtime in SEMANTIC_RUNTIMES
        else "frozen/fad6d07f0a3ad456e7d966dcc98a7fee2491afe0"
    )
    payload: dict[str, Any] = {
        "runtime": args.runtime,
        "semantic_protocol_version": "1.0" if args.runtime in SEMANTIC_RUNTIMES else None,
        "parent_commit": parent_commit,
        "nested_osworld_commit": nested_commit,
        "parent_branch": parent_branch,
        "nested_osworld_branch": nested_branch,
        "runtime_files_sha256": aggregate(runtime_paths, source_root),
        "server_runtime_sha256": os.environ.get(
            "SEMANTIC_SERVER_RUNTIME_SHA256", "unknown"
        ),
        "runtime_files": files,
        "protocol_schema_sha256": files.get("protocol/semantic-v1.schema.json"),
        "system_prompt": ({
            "version": "1.2",
            "source": "harness/prompts/semantic-simple-v1.4.txt",
            "sha256": files["harness/prompts/semantic-simple-v1.4.txt"],
        } if args.runtime == "semantic-simple-v1" else {
            "status": "not_applicable",
        }),
        "harness_lockfile_sha256": files["harness/package-lock.json"],
        "guest_bundle_sha256": (
            aggregate_named(guest_bundle_paths, guest_root)
            if guest_bundle_paths else None
        ),
        "guest_bundle_files": {
            path.relative_to(guest_root).as_posix(): sha256_file(path)
            for path in guest_bundle_paths
        },
        "browser_extension": {
            "status": (
                "not_installed_native_routes_used"
                if args.runtime in SEMANTIC_RUNTIMES else "not_applicable_v15"
            ),
            "sha256": None,
        },
        "application_integrations": ({
            "integration_files_sha256": aggregate(integration_paths, source_root),
            "coverage_sha256": files[
                "protocol/semantic-v1-application-coverage.json"
            ],
            "linux_inventory_sha256": files[
                "OSWorld/protocol/linux-app-inventory.json"
            ],
        } if args.runtime in SEMANTIC_RUNTIMES else {"status": "not_applicable_v15"}),
        "python": {
            "version": platform.python_version(),
            "freeze_sha256": sha256_file(freeze) if freeze.is_file() else None,
        },
        "environment_state_sha256": (
            sha256_file(environment_state) if environment_state.is_file() else None
        ),
        "base_image_digest": os.environ.get("OSWORLD_BASE_IMAGE_DIGEST", "unknown"),
        "ghost_image_digest": os.environ.get("OSWORLD_GUEST_IMAGE_DIGEST", "overlay-development"),
        "model_endpoint": model,
        "model_endpoint_config_sha256": sha256_bytes(
            json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
        ),
        "task_pool": ({
            "path": args.task_pool_name or "frozen-pool",
            "sha256": args.task_pool_sha256,
        } if args.task_pool_sha256 else ({
            "path": task_pool.name,
            "sha256": sha256_file(task_pool),
        } if task_pool else {"status": "not_supplied"})),
        "evaluator_source_sha256": aggregate(evaluator_paths, source_root),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["runtime_manifest_sha256"] = sha256_bytes(canonical)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=sorted(RUNTIMES), default="semantic-v1")
    parser.add_argument("--source-root")
    parser.add_argument("--environment-root")
    parser.add_argument("--parent-commit")
    parser.add_argument("--nested-osworld-commit")
    parser.add_argument("--parent-branch")
    parser.add_argument("--nested-osworld-branch")
    parser.add_argument("--output")
    parser.add_argument("--task-pool")
    parser.add_argument("--task-pool-sha256")
    parser.add_argument("--task-pool-name")
    parser.add_argument("--model-config")
    parser.add_argument("--check")
    args = parser.parse_args()
    payload = build(args)
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.check:
        current = Path(args.check).read_text(encoding="utf-8")
        if current != encoded:
            raise SystemExit(f"semantic runtime manifest is stale: {args.check}")
        print(payload["runtime_manifest_sha256"])
        return
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent, delete=False,
        ) as handle:
            handle.write(encoded)
            temporary = Path(handle.name)
        os.replace(temporary, destination)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
