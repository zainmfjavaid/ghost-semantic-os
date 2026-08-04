"""Evaluate a portable OSWorld pool without taking any agent actions.

Every episode is created in a fresh real OSWorld VM, evaluated immediately,
and torn down. A non-zero score means the task's initial state already passes
its grader, so that task cannot be counted in a model comparison.

This talks only to the env-server API. It never opens or inspects task config
files itself, which keeps a frozen holdout's instructions hidden.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import requests


def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    response = requests.request(method, url, timeout=900, **kwargs)
    response.raise_for_status()
    return response


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pool", type=Path)
    parser.add_argument("--env-url", default="http://127.0.0.1:8079")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pool_path = args.pool.resolve()
    listed = json.loads(pool_path.read_text(encoding="utf-8"))
    if not isinstance(listed, list) or not listed:
        raise SystemExit("pool must be a non-empty JSON array")

    tasks = [
        Path(item) if Path(item).is_absolute() else (pool_path.parent / item).resolve()
        for item in listed
    ]
    base_url = args.env_url.rstrip("/")
    results: list[dict[str, Any]] = []

    for index, task_path in enumerate(tasks, 1):
        episode_id: str | None = None
        result: dict[str, Any] = {
            "index": index,
            "task": task_path.name,
            "score": 0.0,
        }
        try:
            created = _request(
                "POST",
                f"{base_url}/episodes",
                json={
                    "task_path": str(task_path),
                    "initial_observation": False,
                    "web": False,
                    "som": False,
                },
            ).json()
            episode_id = created["episode_id"]
            evaluated = _request(
                "POST", f"{base_url}/episodes/{episode_id}/evaluate"
            ).json()
            result["score"] = float(evaluated.get("score", 0.0))
            if evaluated.get("error"):
                result["error"] = evaluated["error"]
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
        finally:
            if episode_id is not None:
                try:
                    _request("DELETE", f"{base_url}/episodes/{episode_id}")
                except Exception as error:
                    result["cleanup_error"] = f"{type(error).__name__}: {error}"
        results.append(result)
        print(
            f"{index}/{len(tasks)} {task_path.stem[:8]} score={result['score']}",
            flush=True,
        )

    payload = {
        "kind": "osworld_null_agent_control",
        "pool": pool_path.name,
        "completed": len(results),
        "passingWithoutAction": sum(item["score"] > 0 for item in results),
        "errors": sum("error" in item or "cleanup_error" in item for item in results),
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(f"{json.dumps(payload, indent=2)}\n", encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("completed", "passingWithoutAction", "errors")}))

    if payload["passingWithoutAction"] or payload["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
