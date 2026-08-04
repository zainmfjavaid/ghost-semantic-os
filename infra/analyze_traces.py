"""Summarize trace-complete harness results for the next hill-climb decision."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path


def load_results(paths: list[Path]) -> list[dict]:
    episodes: list[dict] = []
    for path in paths:
        try:
            payload = json.loads(path.read_text())
        except Exception as exc:
            print(f"SKIP {path}: {exc}")
            continue
        for episode in payload.get("results", []):
            episodes.append({**episode, "_source": str(path)})
    return episodes


def tool_starts(episode: dict) -> list[dict]:
    return [event for event in episode.get("trace", []) if event.get("kind") == "tool_start"]


def tool_ends(episode: dict) -> list[dict]:
    return [event for event in episode.get("trace", []) if event.get("kind") == "tool_end"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument("--all", action="store_true", help="show successful episode detail too")
    args = parser.parse_args()
    episodes = load_results(args.results)
    if not episodes:
        raise SystemExit("no episodes found")

    scored = [episode for episode in episodes if episode.get("score", 0) > 0]
    calls = [episode.get("toolCalls", 0) for episode in episodes]
    tokens = [episode.get("tokensTotal", 0) for episode in episodes]
    stops = Counter(episode.get("stopReason", "unknown") for episode in episodes)
    tools = Counter(
        event.get("toolName", "unknown")
        for episode in episodes for event in tool_starts(episode)
    )
    js_ends = [
        event for episode in episodes for event in tool_ends(episode)
        if event.get("toolName") == "web_js"
    ]
    js_errors = [
        event for event in js_ends
        if '"ok": false' in (event.get("resultText") or "")
        or "Action error:" in (event.get("resultText") or "")
        or event.get("isError")
    ]
    action_ends = [
        event for episode in episodes for event in tool_ends(episode)
        if event.get("toolName") == "web_actions"
    ]
    action_errors = [
        event for event in action_ends
        if '"ok": false' in (event.get("resultText") or "")
        or "Action error:" in (event.get("resultText") or "")
        or event.get("isError")
    ]
    no_change = sum(
        "did not change" in (event.get("resultText") or "")
        or "has not changed" in (event.get("resultText") or "")
        for episode in episodes for event in tool_ends(episode)
    )
    repeats = sum(
        "same action" in (event.get("resultText") or "")
        for episode in episodes for event in tool_ends(episode)
    )

    print(
        f"{len(scored)}/{len(episodes)} solved "
        f"({100 * len(scored) / len(episodes):.1f}%) | "
        f"calls mean={statistics.mean(calls):.1f} median={statistics.median(calls):.1f} | "
        f"tokens/ep={statistics.mean(tokens):,.0f}"
    )
    print("stops:", " ".join(f"{name}={count}" for name, count in stops.most_common()))
    print(
        f"web_js={len(js_ends)} errors={len(js_errors)} | "
        f"web_actions={len(action_ends)} errors={len(action_errors)} | "
        f"no-change notices={no_change} repeated notices={repeats}"
    )
    print("tools:", " ".join(f"{name}={count}" for name, count in tools.most_common()))

    print("\nEPISODES")
    for episode in episodes:
        if episode.get("score", 0) > 0 and not args.all:
            continue
        starts = tool_starts(episode)
        sequence = " ".join((event.get("toolName") or "?").removeprefix("web_") for event in starts)
        print(
            f"\n{episode.get('taskId')} score={episode.get('score')} "
            f"calls={episode.get('toolCalls')} stop={episode.get('stopReason')}"
        )
        print(f"  {episode.get('instruction', '')}")
        if episode.get("error"):
            print(f"  HARNESS ERROR: {episode['error']}")
        if episode.get("evaluationError"):
            print(f"  EVALUATION ERROR: {episode['evaluationError']}")
        if episode.get("cleanupError"):
            print(f"  CLEANUP ERROR: {episode['cleanupError']}")
        print(f"  sequence: {sequence}")
        for start in starts:
            if start.get("toolName") not in ("web_js", "web_actions"):
                continue
            call_id = start.get("toolCallId")
            end = next(
                (event for event in tool_ends(episode)
                 if event.get("toolCallId") == call_id),
                None,
            )
            args_text = str(start.get("args") or {}).replace("\n", " ")
            result = str((end or {}).get("resultText", "")).replace("\n", " ")
            label = "JS" if start.get("toolName") == "web_js" else "ACTIONS"
            print(f"  {label} {args_text[:360]}")
            print(f"    -> {result[:320]}")


if __name__ == "__main__":
    main()
