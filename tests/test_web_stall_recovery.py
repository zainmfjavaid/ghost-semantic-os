"""A wedged Playwright call must not poison every later browser action."""
from __future__ import annotations

import sys
import threading
import time
import types
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))

ocr = types.ModuleType("easyocr")
ocr.Reader = lambda *args, **kwargs: None
sys.modules.setdefault("easyocr", ocr)


class StubEnv:
    vm_ip = "127.0.0.1"
    chromium_port = 9222

    def _get_obs(self):
        return {"screenshot": None, "accessibility_tree": None}


desktop = types.ModuleType("desktop_env")
desktop_module = types.ModuleType("desktop_env.desktop_env")
desktop_module.DesktopEnv = StubEnv
desktop.desktop_env = desktop_module
sys.modules["desktop_env"] = desktop
sys.modules["desktop_env.desktop_env"] = desktop_module

import server  # noqa: E402
from web_provider import WebProvider, WebProviderStalled, _pinned  # noqa: E402


class TimeoutProbe:
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._call_timeout_s = 0.05
        self.release = threading.Event()
        self.terminated = False

    def _terminate_execution_out_of_band(self):
        self.terminated = True
        return True

    @_pinned
    def block(self):
        self.release.wait(2)

    def retire(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


class StalledProvider:
    def __init__(self):
        self.retired = False

    def elements(self):
        raise WebProviderStalled("forced provider stall")

    def describe(self, elements, limit=None, chars=80):
        return ""

    def retire(self):
        self.retired = True


def main() -> None:
    probe = TimeoutProbe()
    started = time.monotonic()
    try:
        probe.block()
    except WebProviderStalled as error:
        assert "will be replaced" in str(error)
    else:
        raise AssertionError("pinned call did not time out")
    assert time.monotonic() - started < 0.5
    assert probe.terminated
    probe.release.set()
    probe.retire()
    print("PASS pinned provider call has a bounded wall-clock deadline")

    episode_id = "forced-stall"
    provider = StalledProvider()
    server._episodes[episode_id] = {
        "env": StubEnv(),
        "steps": 0,
        "web_provider": provider,
    }
    try:
        result = server.web_action(
            episode_id,
            server.WebAction(action="elements", observe=False),
        )
        assert result["web_provider_recovered"] is True
        assert result["errors"][0].startswith("WebProviderStalled:")
        assert server._episodes[episode_id]["web_provider"] is None
        assert "web_elements" not in server._episodes[episode_id]
        assert provider.retired
        print("PASS server retires the wedged provider for lazy reconnection")
    finally:
        server._episodes.pop(episode_id, None)

    try:
        urllib.request.urlopen(
            "http://127.0.0.1:1337/json/version", timeout=1
        ).close()
    except Exception:
        print("SKIP live CDP recovery check (no headless Chrome on port 1337)")
        return

    first = WebProvider(
        "127.0.0.1", port=1337, fallback_ports=(), call_timeout_s=5
    )
    first.navigate("data:text/html,<title>recovery</title><button>still here</button>")
    first._call_timeout_s = 0.2
    try:
        first.run_js("new Promise(() => {})")
    except WebProviderStalled:
        pass
    else:
        raise AssertionError("unresolved browser promise did not trip provider deadline")
    first.retire()

    replacement = WebProvider(
        "127.0.0.1", port=1337, fallback_ports=(), call_timeout_s=5
    )
    try:
        assert "still here" in replacement.page_text()
        print("PASS out-of-band termination preserves state for a new CDP provider")
    finally:
        replacement.close()


if __name__ == "__main__":
    main()
