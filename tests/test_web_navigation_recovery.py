"""A requested navigation can start Chrome's CDP surface, but clicks never replay."""
from __future__ import annotations

import sys
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))

ocr = types.ModuleType("easyocr")
ocr.Reader = lambda *args, **kwargs: None
sys.modules.setdefault("easyocr", ocr)


class StubDesktopEnv:
    pass


desktop_env = types.ModuleType("desktop_env")
desktop_env_module = types.ModuleType("desktop_env.desktop_env")
desktop_env_module.DesktopEnv = StubDesktopEnv
desktop_env.desktop_env = desktop_env_module
sys.modules["desktop_env"] = desktop_env
sys.modules["desktop_env.desktop_env"] = desktop_env_module

import server  # noqa: E402


class SetupController:
    def __init__(self) -> None:
        self.launches: list[tuple[list[str], bool]] = []

    def _launch_setup(self, command, wait_for_cdp=False):
        self.launches.append((command, wait_for_cdp))


class Env:
    vm_ip = "localhost"
    # Host-mapped concurrent episode port. The guest command must not use it.
    chromium_port = 9229

    def __init__(self) -> None:
        self.setup_controller = SetupController()


class UnavailableProvider:
    def navigate(self, _url):
        raise RuntimeError("could not reach Chrome over CDP on (9229, 1337)")

    def elements(self):
        raise RuntimeError("could not reach Chrome over CDP on (9229, 1337)")

    def retire(self):
        return None


class RecoveredProvider:
    def navigate(self, url):
        return f"navigated to {url}"

    def elements(self):
        return []

    def describe(self, _elements, _limit=None, _chars=80):
        return ""


def entry(env, provider):
    return {
        "env": env,
        "task": {"config": []},
        "steps": 0,
        "som": False,
        "semantic_only": False,
        "marks": [],
        "web": True,
        "web_provider": provider,
        "web_elements": [],
        "pool": ThreadPoolExecutor(max_workers=1),
        "recent_commands": [],
    }


def main() -> None:
    original_web = server._web
    original_chrome_running = server._guest_chrome_running
    env = Env()
    recovered = RecoveredProvider()
    episode_id = "navigation-recovery"
    server._episodes[episode_id] = entry(env, UnavailableProvider())

    def fake_web(record):
        if record.get("web_provider") is None:
            record["web_provider"] = recovered
        return record["web_provider"]

    try:
        server._web = fake_web
        server._guest_chrome_running = lambda _record: False
        result = server.web_action(
            episode_id,
            server.WebAction(
                action="navigate", url="https://example.test/path", observe=False,
            ),
        )
        assert not result.get("errors"), result
        assert result["web_provider_recovered"] is True
        assert result["result"] == "navigated to https://example.test/path"
        assert len(env.setup_controller.launches) == 1
        command, waited = env.setup_controller.launches[0]
        assert command[0] == "google-chrome"
        assert "--remote-debugging-port=9222" in command
        assert "--remote-debugging-port=9229" not in command
        assert command[-1] == "https://example.test/path"
        assert waited is False
        print("PASS web_navigate safely launches and retries CDP Chrome")
    finally:
        server._web = original_web
        server._guest_chrome_running = original_chrome_running
        record = server._episodes.pop(episode_id)
        record["pool"].shutdown(wait=False)

    # A running ordinary Chrome cannot gain CDP after launch. Detect it before
    # entering SetupController's old 120-second readiness wait, preserve its
    # state, and tell the model which semantic routes remain.
    env_running = Env()
    episode_id = "running-chrome-no-launch"
    server._episodes[episode_id] = entry(env_running, UnavailableProvider())
    try:
        server._guest_chrome_running = lambda _record: True
        result = server.web_action(
            episode_id,
            server.WebAction(
                action="navigate", url="https://example.test/path", observe=False,
            ),
        )
        assert result.get("errors"), result
        assert "already running without a CDP endpoint" in result["errors"][0]
        assert "desktop_find" in result["errors"][0]
        assert env_running.setup_controller.launches == []
        print("PASS ordinary running Chrome fails fast without a doomed CDP launch")
    finally:
        server._guest_chrome_running = original_chrome_running
        record = server._episodes.pop(episode_id)
        record["pool"].shutdown(wait=False)

    # A non-idempotent operation receives an actionable error and is never
    # launched/replayed behind the model's back.
    env2 = Env()
    episode_id = "click-no-replay"
    server._episodes[episode_id] = entry(env2, UnavailableProvider())
    try:
        result = server.web_action(
            episode_id,
            server.WebAction(action="click", index=0, observe=False),
        )
        assert result.get("errors")
        assert "web_navigate" in result["errors"][0]
        assert env2.setup_controller.launches == []
        print("PASS unavailable CDP never replays non-idempotent actions")
    finally:
        record = server._episodes.pop(episode_id)
        record["pool"].shutdown(wait=False)


if __name__ == "__main__":
    main()
