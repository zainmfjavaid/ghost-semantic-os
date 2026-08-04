#!/usr/bin/env python3
"""Model-free real-guest canary for the generalized execution seams."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-url", default="http://127.0.0.1:8079")
    parser.add_argument("--task", required=True, type=Path)
    parser.add_argument("--sleep-seconds", type=float, default=2.0)
    args = parser.parse_args()

    task = args.task.resolve()
    if not task.is_file():
        raise SystemExit(f"task does not exist: {task}")
    created = requests.post(
        f"{args.env_url}/episodes",
        json={"task_path": str(task), "web": True, "som": True},
        timeout=300,
    )
    created.raise_for_status()
    episode_id = created.json()["episode_id"]
    try:
        executed = requests.post(
            f"{args.env_url}/episodes/{episode_id}/exec",
            json={
                "script": f"sleep {args.sleep_seconds}; printf async-ok",
                # Above the legacy synchronous cutoff, even for a short canary.
                "timeout_seconds": 120,
                "working_dir": "/home/user",
            },
            timeout=150,
        )
        executed.raise_for_status()
        execution_payload = executed.json()
        if execution_payload.get("errors") or '"stdout":"async-ok"' not in str(
            execution_payload.get("result")
        ):
            raise RuntimeError(f"detached execution failed: {execution_payload}")

        rejected = requests.post(
            f"{args.env_url}/episodes/{episode_id}/exec",
            json={"script": "pkill -f chrome", "timeout_seconds": 10},
            timeout=30,
        )
        if rejected.status_code != 400 or "execution wrapper's own argv" not in rejected.text:
            raise RuntimeError(
                f"self-match guard did not fail closed: {rejected.status_code} {rejected.text}"
            )

        researched = requests.post(
            f"{args.env_url}/episodes/{episode_id}/web",
            json={
                "action": "read_pages",
                "urls": ["https://example.com/"],
                "text_limit": 500,
                "observe": False,
            },
            timeout=120,
        )
        researched.raise_for_status()
        research_payload = researched.json()
        if research_payload.get("errors"):
            diagnosed = requests.post(
                f"{args.env_url}/episodes/{episode_id}/exec",
                json={
                    "script": (
                        "cat /tmp/osworld_chrome_stderr.log 2>/dev/null; "
                        "echo '--- processes ---'; pgrep -a chrome || true; "
                        "echo '--- listeners ---'; "
                        "ss -ltnp 2>/dev/null | grep -E '1337|9222' || true"
                    ),
                    "timeout_seconds": 10,
                },
                timeout=30,
            )
            diagnostics = diagnosed.json() if diagnosed.ok else diagnosed.text
            raise RuntimeError(
                f"CDP recovery failed: {research_payload}; diagnostics={diagnostics}"
            )
        if not research_payload.get("web_provider_recovered"):
            raise RuntimeError(
                "canary did not exercise read-only unavailable-CDP recovery"
            )
        if "Example Domain" not in str(research_payload.get("result")):
            raise RuntimeError(f"research missing page evidence: {research_payload}")

        navigated = requests.post(
            f"{args.env_url}/episodes/{episode_id}/web",
            json={
                "action": "navigate",
                "url": "https://example.com/",
                "observe": False,
            },
            timeout=120,
        )
        navigated.raise_for_status()
        navigation_payload = navigated.json()
        if navigation_payload.get("errors"):
            raise RuntimeError(f"navigation after recovery failed: {navigation_payload}")
        if "navigated to" not in str(navigation_payload.get("result")):
            raise RuntimeError(f"navigation missing completion evidence: {navigation_payload}")

        print(json.dumps({
            "ok": True,
            "episode_id": episode_id,
            "detached_exec": "async-ok",
            "self_match_guard": "rejected",
            "cdp_recovered": True,
        }, sort_keys=True))
    finally:
        closed = requests.delete(
            f"{args.env_url}/episodes/{episode_id}", timeout=180,
        )
        closed.raise_for_status()


if __name__ == "__main__":
    main()
