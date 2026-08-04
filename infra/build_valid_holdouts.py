#!/usr/bin/env python3
"""Select disjoint post-freeze OSWorld holdouts from metadata only.

The selection code intentionally uses only task ID, path/domain and
``related_apps``. It never consults task wording, setup, evaluator type or
grader details. Existing results, existing pools and explicit development
probes are excluded before deterministic hash-ranked sampling.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable


REPO = Path(__file__).resolve().parents[1]
FREEZE = REPO / "infra/frozen_runtime_v2.sha256"
RUNTIME_FILES = REPO / "infra/runtime_v2_files.txt"
EXAMPLES = REPO / "OSWorld/evaluation_examples/examples"
DEV_PROBES = REPO / "pools/development_probe_ids.txt"
TARGET_NAMES = (
    "browser_holdout_a_v2.json",
    "browser_holdout_b_v2.json",
    "desktop_ood_v2.json",
    "runtime_v2_holdouts.manifest.json",
)
DESKTOP_DOMAINS = (
    "libreoffice_writer",
    "libreoffice_calc",
    "libreoffice_impress",
    "os",
)
BASIC_CROSS_APP = {
    "chrome",
    "libreoffice_suite",
    "libreoffice_writer",
    "libreoffice_calc",
    "libreoffice_impress",
    "os",
    "pdf",
    "thunderbird",
    "vs_code",
}
APP_ALIASES = {
    "calc": "libreoffice_calc",
    "writer": "libreoffice_writer",
    "vscode": "vs_code",
    "libreoffice": "libreoffice_suite",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_hash() -> str:
    lines = []
    for relative in RUNTIME_FILES.read_text(encoding="utf-8").splitlines():
        relative = relative.strip()
        if relative:
            path = REPO / relative
            lines.append(f"{sha256_file(path)}  {path}\n")
    return hashlib.sha256("".join(lines).encode()).hexdigest()


def ids_from_paths(values: Iterable[str]) -> set[str]:
    return {Path(value).stem for value in values if isinstance(value, str)}


def prior_ids(output_dir: Path) -> set[str]:
    seen: set[str] = set()
    for result_path in REPO.glob("results*/**/results.json"):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            for result in payload.get("results", []):
                if isinstance(result, dict) and isinstance(result.get("taskId"), str):
                    seen.add(result["taskId"])
    for result_path in (REPO / "results_gcp").glob("**/osworld-dev-[0-9].json"):
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            for result in payload.get("results", []):
                if isinstance(result, dict) and isinstance(result.get("taskId"), str):
                    seen.add(result["taskId"])
    for pool_path in (REPO / "pools").glob("*.json"):
        if pool_path.name in TARGET_NAMES:
            continue
        try:
            payload = json.loads(pool_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, list):
            seen.update(ids_from_paths(payload))
    if DEV_PROBES.exists():
        seen.update(
            line.strip()
            for line in DEV_PROBES.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return seen


def task_metadata(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    task_id = payload.get("id")
    apps = payload.get("related_apps") or []
    if not isinstance(task_id, str) or not isinstance(apps, list):
        raise ValueError(f"invalid metadata in {path}")
    normalized_apps = (
        re.sub(r"[^a-z0-9]+", "_", str(app).casefold()).strip("_")
        for app in apps
    )
    canonical_apps = tuple(sorted(
        APP_ALIASES.get(app, app) for app in normalized_apps
    ))
    return {
        "id": task_id,
        "path": path,
        "domain": path.parent.name,
        "apps": canonical_apps,
    }


def rank(records: list[dict[str, object]], seed: str, bucket: str) -> list[dict[str, object]]:
    return sorted(records, key=lambda record: hashlib.sha256(
        f"{seed}:{bucket}:{record['id']}".encode()
    ).hexdigest())


def write_pool(path: Path, records: list[dict[str, object]]) -> None:
    paths = [
        str(Path(record["path"]).relative_to(path.parent.parent))
        for record in records
    ]
    # Pools live in repo/pools, so make paths explicitly relative to that file.
    paths = [f"../{value}" for value in paths]
    path.write_text(json.dumps(paths, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=12)
    parser.add_argument("--seed", default="semantic-runtime-v2-ood-2026-07-31")
    parser.add_argument("--output-dir", type=Path, default=REPO / "pools")
    args = parser.parse_args()
    if args.count < 4 or args.count % 4:
        raise SystemExit("--count must be a positive multiple of four")

    if not FREEZE.is_file():
        raise SystemExit("runtime must be frozen before selecting holdouts")
    frozen = dict(
        line.split("=", 1)
        for line in FREEZE.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    actual_runtime = runtime_hash()
    if frozen.get("runtime_sha256") != actual_runtime:
        raise SystemExit(
            f"runtime changed after freeze: {frozen.get('runtime_sha256')} != {actual_runtime}"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    for name in TARGET_NAMES:
        if (args.output_dir / name).exists():
            raise SystemExit(f"refusing to overwrite one-time holdout artifact: {name}")

    excluded = prior_ids(args.output_dir)
    records = [
        task_metadata(path)
        for path in sorted(EXAMPLES.glob("*/*.json"))
        if path.stem not in excluded
    ]
    browser_candidates = [
        record for record in records
        if "chrome" in set(record["apps"])
        and set(record["apps"]) <= BASIC_CROSS_APP
    ]
    ranked_browser = rank(browser_candidates, args.seed, "browser-associated")
    selected_a = ranked_browser[:args.count]
    selected_b = ranked_browser[args.count:args.count * 2]
    if len(selected_a) != args.count or len(selected_b) != args.count:
        raise SystemExit(json.dumps({
            "error": "insufficient browser candidates",
            "browser_associated": len(browser_candidates),
        }))

    per_domain = args.count // len(DESKTOP_DOMAINS)
    selected_desktop: list[dict[str, object]] = []
    desktop_available: dict[str, int] = {}
    for domain in DESKTOP_DOMAINS:
        candidates = [record for record in records if record["domain"] == domain]
        desktop_available[domain] = len(candidates)
        selected_desktop.extend(rank(candidates, args.seed, domain)[:per_domain])
    if len(selected_desktop) != args.count:
        raise SystemExit(json.dumps({
            "error": "insufficient desktop candidates",
            "available": desktop_available,
        }))

    pools = {
        "browser_holdout_a_v2.json": selected_a,
        "browser_holdout_b_v2.json": selected_b,
        "desktop_ood_v2.json": selected_desktop,
    }
    for name, selected in pools.items():
        write_pool(args.output_dir / name, selected)

    manifest = {
        "runtime_sha256": actual_runtime,
        "seed": args.seed,
        "selection_fields": ["id", "related_apps", "path/domain"],
        "excluded_prior_or_development_ids": len(excluded),
        "candidate_counts": {
            "browser_associated": len(browser_candidates),
            **{f"desktop_{key}": value for key, value in desktop_available.items()},
        },
        "pools": {},
    }
    for name, selected in pools.items():
        pool_path = args.output_dir / name
        manifest["pools"][name] = {
            "count": len(selected),
            "sha256": sha256_file(pool_path),
            "task_ids": [record["id"] for record in selected],
            "app_signatures": [list(record["apps"]) for record in selected],
        }
    (args.output_dir / "runtime_v2_holdouts.manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "runtime_sha256": actual_runtime,
        "pool_counts": {name: len(value) for name, value in pools.items()},
        "selection_fields": manifest["selection_fields"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
