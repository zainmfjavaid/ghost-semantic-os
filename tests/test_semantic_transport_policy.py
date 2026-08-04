"""Semantic episodes must never expose legacy observation/action routes."""
from __future__ import annotations

import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))

ocr = types.ModuleType("easyocr")
ocr.Reader = lambda *args, **kwargs: None
sys.modules.setdefault("easyocr", ocr)


class FakeStrictEnv:
    instances: list["FakeStrictEnv"] = []

    def __init__(self, *args, **kwargs):
        self.require_screenshot = kwargs.get("require_screenshot", True)
        self.screenshot_capture_count = 0
        self.closed = False
        self.instances.append(self)

    def reset(self, task_config=None):
        return {"screenshot": None, "accessibility_tree": None}

    def close(self):
        self.closed = True

    def evaluate(self):
        return 0.75


class FakeWebProvider:
    def elements(self):
        return []

    def describe(self, elements, limit, chars):
        assert elements == []
        return ""


desktop = types.ModuleType("desktop_env")
desktop_module = types.ModuleType("desktop_env.desktop_env")
desktop_module.DesktopEnv = FakeStrictEnv
desktop.desktop_env = desktop_module
sys.modules["desktop_env"] = desktop
sys.modules["desktop_env.desktop_env"] = desktop_module

import server  # noqa: E402


def expect_policy_violation(callable_, route: str | None = None) -> None:
    try:
        callable_()
    except server.HTTPException as error:
        assert error.status_code == 409
        assert error.detail["code"] == "policy_violation"
        if route is not None:
            assert route in error.detail["message"]
    else:
        raise AssertionError("strict legacy route unexpectedly succeeded")


def main() -> None:
    server.DesktopEnv = FakeStrictEnv
    server.guest_semantic.bootstrap = lambda env, episode_id: {
        "token": "private-test-token",
        "port": 8765,
        "bundle_hash": "a" * 64,
        "agent_version": "test",
    }
    server.guest_semantic.shutdown = lambda env, state: None
    def fake_guest_request(env, state, method, path, payload=None):
        if path == "/v1/query":
            return {
                "ok": True,
                "result": {
                    "records": [
                        {
                            "ref": "desktop",
                            "role": "frame",
                            "name": "Desktop",
                            "state": {"visible": True, "showing": True},
                        },
                        {
                            "ref": "app-window",
                            "role": "frame",
                            "name": "Application",
                            "state": {
                                "visible": True, "showing": True, "active": True,
                            },
                        },
                    ],
                },
            }
        return {
            "ok": True,
            "result": {
                "records": [{
                    "adapter_id": "test-adapter@1",
                    "resources": ["ui.elements"],
                    "actions": ["invoke"],
                    "execution_paths": ["accessibility"],
                }],
            },
        }

    server.guest_semantic.request = fake_guest_request
    original_readiness = server._wait_for_semantic_setup_readiness
    server._wait_for_semantic_setup_readiness = lambda env, state, task: original_readiness(
        env, state, task, timeout_seconds=0.1, poll_seconds=0
    )

    # Generic setup inference ignores background helpers and recognizes actual
    # GUI launch mechanics without consulting instruction/evaluator fields.
    assert server._gui_setup_families({
        "instruction": "must not be inspected",
        "evaluator": {"gold": "must not be inspected"},
        "config": [
            {"type": "launch", "parameters": {"command": ["socat", "x", "y"]}},
            {"type": "launch", "parameters": {"command": ["code"]}},
            {"type": "activate_window", "parameters": {"window_name": "ignored"}},
        ],
    }) == frozenset({"vscode"})
    assert server._expected_gui_surface_count({
        "config": [
            {"type": "launch", "parameters": {"command": ["libreoffice", "--writer", "a.docx"]}},
            {"type": "launch", "parameters": {"command": ["libreoffice", "--calc", "b.xlsx"]}},
            {"type": "launch", "parameters": {"command": ["nautilus", "/tmp"]}},
            {"type": "chrome_open_tabs", "parameters": {"urls_to_open": ["https://one.test"]}},
            {"type": "chrome_open_tabs", "parameters": {"urls_to_open": ["https://two.test"]}},
        ],
    }) == 4

    # Desktop-only setup returns immediately and performs no guest query.
    query_count = 0

    def forbidden_query(*_args, **_kwargs):
        nonlocal query_count
        query_count += 1
        raise AssertionError("desktop-only readiness queried GUI state")

    server.guest_semantic.request = forbidden_query
    desktop_readiness = original_readiness(
        object(), {}, {"config": [
            {"type": "execute", "parameters": {}},
            {"type": "activate_window", "parameters": {"window_name": "Desktop"}},
        ]}
    )
    assert desktop_readiness["status"] == "not_required"
    assert desktop_readiness["waited_ms"] == 0
    assert query_count == 0

    # A real-app surface must be present with the same identity on consecutive
    # polls. Desktop alone never satisfies readiness.
    readiness_responses = iter([
        [{"ref": "desktop", "role": "frame", "name": "Desktop"}],
        [
            {"ref": "desktop", "role": "frame", "name": "Desktop"},
            {
                "ref": "mail", "role": "frame", "name": "Mail",
                "state": {"active": True},
            },
        ],
        [
            {"ref": "desktop", "role": "frame", "name": "Desktop"},
            {
                "ref": "mail", "role": "frame", "name": "Mail",
                "state": {"focused": True},
            },
        ],
    ])
    server.guest_semantic.request = lambda *_args, **_kwargs: {
        "ok": True, "result": {"records": next(readiness_responses)}
    }
    now = [0.0]
    readiness = original_readiness(
        object(), {},
        {"config": [{"type": "launch", "parameters": {"command": ["thunderbird"]}}]},
        timeout_seconds=2,
        poll_seconds=0.1,
        clock=lambda: now[0],
        sleep=lambda duration: now.__setitem__(0, now[0] + duration),
    )
    assert readiness["status"] == "ready"
    assert readiness["polls"] == 3
    assert readiness["observed_gui_surfaces"] == 1
    assert readiness["observed_active_gui_surfaces"] == 1
    assert readiness["stable_polls"] == 2

    # Stable visible error/network windows are not usable setup state without
    # a current active/focused non-shell interaction root.
    readiness_responses = iter([
        [{"ref": "network-error", "role": "frame", "name": "Network Error"}],
        [{"ref": "network-error", "role": "frame", "name": "Network Error"}],
        [{
            "ref": "browser", "role": "frame", "name": "Browser",
            "state": {"active": True},
        }],
        [{
            "ref": "browser", "role": "frame", "name": "Browser",
            "state": {"focused": True},
        }],
    ])
    server.guest_semantic.request = lambda *_args, **_kwargs: {
        "ok": True, "result": {"records": next(readiness_responses)}
    }
    now = [0.0]
    active_readiness = original_readiness(
        object(), {},
        {"config": [{"type": "launch", "parameters": {"command": ["google-chrome"]}}]},
        timeout_seconds=2,
        poll_seconds=0.1,
        clock=lambda: now[0],
        sleep=lambda duration: now.__setitem__(0, now[0] + duration),
    )
    assert active_readiness["status"] == "ready"
    assert active_readiness["polls"] == 4
    assert active_readiness["observed_active_gui_surfaces"] == 1
    assert active_readiness["stable_polls"] == 2

    # Transport loss is bounded and explicitly fail-open rather than silently
    # failing the episode or pretending the app is ready.
    server.guest_semantic.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("guest unavailable")
    )
    now = [0.0]
    unavailable = original_readiness(
        object(), {},
        {"config": [{"type": "launch", "parameters": {"command": ["code"]}}]},
        timeout_seconds=0.2,
        poll_seconds=0.1,
        clock=lambda: now[0],
        sleep=lambda duration: now.__setitem__(0, now[0] + duration),
    )
    assert unavailable["status"] == "unavailable"
    assert unavailable["fail_open"] is True
    assert unavailable["query_errors"] == 3

    server.guest_semantic.request = fake_guest_request
    task = (
        REPO
        / "OSWorld/evaluation_examples/examples/chrome/"
        / "b4f95342-463e-4179-8c3f-193cd7241fb2.json"
    )
    for runtime_name in ("semantic-v1", "semantic-plus-v1", "semantic-simple-v1"):
        expect_policy_violation(lambda: server.create_episode(server.CreateEpisode(
            task_path=str(task), runtime=runtime_name, require_screenshot=True,
            initial_observation=False,
        )))
        expect_policy_violation(lambda: server.create_episode(server.CreateEpisode(
            task_path=str(task), runtime=runtime_name, require_screenshot=False,
            initial_observation=True,
        )))

        created = server.create_episode(server.CreateEpisode(
            task_path=str(task), runtime=runtime_name, require_screenshot=False,
            initial_observation=False,
        ))
        episode_id = created["episode_id"]
        entry = server._episodes[episode_id]
        env = entry["env"]
        closed_by_route = False
        try:
            assert created["runtime"] == runtime_name
            assert created["semantic_protocol_version"] == "1.0"
            assert created["screenshots_captured"] == 0
            assert created["setup_readiness"]["status"] == "ready"
            assert created["setup_readiness"]["observed_gui_surfaces"] == 1
            assert env.require_screenshot is False
            assert env.screenshot_capture_count == 0
            semantic_state = server.semantic_state(episode_id)
            assert semantic_state["runtime"] == runtime_name
            assert semantic_state["screenshots_captured"] == 0
            assert semantic_state["pixels_sent_to_policy_model"] == 0
            expect_policy_violation(lambda: server.get_obs(episode_id))
            expect_policy_violation(lambda: server.step(
                episode_id,
                server.StepRequest(
                    command="import pyautogui; pyautogui.click(1, 1)"
                ),
            ))
            if runtime_name != "semantic-simple-v1":
                expect_policy_violation(lambda: server.simple_read(
                    episode_id,
                    server.SimpleReadRequest(),
                ))
                expect_policy_violation(lambda: server.simple_click(
                    episode_id,
                    server.SimpleClickRequest(element="A1"),
                ))
                expect_policy_violation(lambda: server.simple_type(
                    episode_id,
                    server.SimpleTypeRequest(element="A1", text="hello"),
                ))
            if runtime_name == "semantic-plus-v1":
                expect_policy_violation(lambda: server.web_action(
                    episode_id,
                    server.WebAction(action="elements", observe=True),
                ))
                entry["web_provider"] = FakeWebProvider()
                web_result = server.web_action(
                    episode_id,
                    server.WebAction(action="elements", observe=False),
                )
                assert web_result["web_element_count"] == 0
            if runtime_name == "semantic-simple-v1":
                for operation in ("query", "act"):
                    expect_policy_violation(lambda operation=operation: server.semantic_operation(
                        episode_id,
                        {
                            "protocol_version": "1.0",
                            "request_id": f"blocked-{operation}",
                            "episode_id": episode_id,
                            "operation": operation,
                            "payload": {},
                        },
                    ), "/semantic")
                expect_policy_violation(lambda: server.semantic_complete(
                    episode_id, {"task_status": "complete"},
                ), "/semantic/complete")
                expect_policy_violation(lambda: server.computer_exec(
                    episode_id,
                    server.ComputerExecRequest(script="true"),
                ), "/exec")
                for observe in (False, True):
                    expect_policy_violation(lambda observe=observe: server.web_action(
                        episode_id,
                        server.WebAction(action="elements", observe=observe),
                    ), "/web")
                expect_policy_violation(lambda: server.element_find(
                    episode_id,
                    server.ElementFind(query="Save"),
                ), "/element/find")
                expect_policy_violation(lambda: server.element_match(
                    episode_id,
                    server.ElementMatch(query="Save"),
                ), "/element/match")
                expect_policy_violation(lambda: server.element_action(
                    episode_id,
                    server.ElementAction(index=1),
                ), "/element")

                facade = entry["simple_facade"]
                facade.read = lambda **kwargs: {
                    "ok": True, "text": "simple read", "arguments": kwargs,
                }
                facade.click = lambda element: {
                    "ok": True, "text": f"simple click {element}",
                }
                facade.type_text = lambda element, text: {
                    "ok": True, "text": f"simple type {element} {text}",
                }
                assert server.SimpleReadRequest().limit == 60
                simple_read = server.simple_read(
                    episode_id,
                    server.SimpleReadRequest(query="Save", limit=3),
                )
                assert simple_read == {
                    "ok": True,
                    "text": "simple read",
                    "arguments": {
                        "query": "Save",
                        "within": None,
                        "cursor": None,
                        "limit": 3,
                    },
                }
                assert server.simple_click(
                    episode_id,
                    server.SimpleClickRequest(element="A1"),
                )["text"] == "simple click A1"
                assert server.simple_type(
                    episode_id,
                    server.SimpleTypeRequest(element="A2", text="hello"),
                )["text"] == "simple type A2 hello"

                evaluation = server.evaluate(episode_id)
                assert evaluation == {"score": 0.75, "steps": 0}
                closed = server.close_episode(episode_id)
                assert closed == {"closed": episode_id}
                assert env.closed is True
                closed_by_route = True
            assert env.screenshot_capture_count == 0
            print(f"PASS {runtime_name} constructs a screenshot-disabled desktop")
            print(f"PASS {runtime_name} rejects legacy observation and pyautogui routes")
        finally:
            if not closed_by_route:
                with server._lock:
                    server._episodes.pop(episode_id, None)
                entry["pool"].submit(env.close).result()
                entry["pool"].shutdown(wait=True)


if __name__ == "__main__":
    main()
