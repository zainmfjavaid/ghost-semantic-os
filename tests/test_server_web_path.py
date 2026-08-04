"""Integration test for the env server's own /web handler.

WebProvider has been tested directly, but the server code around it never has:
episode state, lazy provider attachment, element caching between calls, index
validation, error shaping. That code runs on resume against real OSWorld VMs, and
a bug there would look like "CDP does not work" and cost another hour.

DesktopEnv is stubbed so no VM is needed; the provider points at a local Chrome.
Run with a Chrome listening on 9222.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))
sys.path.insert(0, str(REPO / "OSWorld"))


class _StubEnv:
    """Just enough DesktopEnv surface for the server's web path."""

    def __init__(self, *a, **k):
        self.vm_ip = "127.0.0.1"
        self.chromium_port = 9222

    def reset(self, task_config=None):
        return {"screenshot": _png(), "accessibility_tree": None}

    def _get_obs(self):
        return {"screenshot": _png(), "accessibility_tree": None}

    def step(self, command, pause=1.0):
        return {"screenshot": _png()}, 0, False, {}

    def evaluate(self):
        return 0.0

    def close(self):
        pass


def _png() -> bytes:
    import io

    from PIL import Image

    b = io.BytesIO()
    Image.new("RGB", (400, 300), (250, 250, 252)).save(b, format="PNG")
    return b.getvalue()


# Install the stub before importing the server, which imports DesktopEnv at module load.
fake_pkg = types.ModuleType("desktop_env")
fake_mod = types.ModuleType("desktop_env.desktop_env")
fake_mod.DesktopEnv = _StubEnv
fake_pkg.desktop_env = fake_mod
sys.modules.setdefault("desktop_env", fake_pkg)
sys.modules["desktop_env.desktop_env"] = fake_mod

import server  # noqa: E402


def main() -> None:
    ok = True
    server.DesktopEnv = _StubEnv  # the module captured the real symbol at import

    ep = server.create_episode(server.CreateEpisode(
        task_path=str(REPO / "OSWorld/evaluation_examples/examples/chrome/"
                      "b4f95342-463e-4179-8c3f-193cd7241fb2.json"),
        web=True,
    ))
    eid = ep["episode_id"]
    print(f"PASS episode created ({eid}) instruction={ep['instruction'][:50]!r}")
    clock = ep.get("browser_clock") or {}
    if not clock.get("text") or "offsetMinutes" not in clock:
        print(f"FAIL browser clock missing or incomplete: {clock}"); ok = False
    else:
        print(f"PASS browser clock grounded -> {clock.get('text')}")

    r = server.web_action(eid, server.WebAction(action="navigate",
                                                url="https://example.com"))
    print("PASS navigate ->", r.get("result"))
    if "web_elements" not in r:
        print("FAIL navigate returned no element list"); ok = False

    r = server.web_action(eid, server.WebAction(action="elements"))
    n = r.get("web_element_count", 0)
    print(f"PASS elements -> {n} listed")
    if n < 1:
        print("FAIL no elements"); ok = False

    r = server.web_action(eid, server.WebAction(action="text"))
    print("PASS text ->", (r.get("page_text") or "")[:60].replace("\n", " "))

    r = server.web_action(eid, server.WebAction(action="text", observe=False))
    if "screenshot" in r:
        print("FAIL observe=false still returned a screenshot"); ok = False
    else:
        print("PASS text-only web action skips the discarded VM screenshot")

    # index validation must be a clean 400, not a crash
    try:
        server.web_action(eid, server.WebAction(action="click", index=9999))
        print("FAIL out-of-range index was accepted"); ok = False
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        print(f"PASS out-of-range index rejected (status={code})")
        if code != 400:
            print("  (expected 400)"); ok = False

    r = server.web_action(eid, server.WebAction(action="click", index=0))
    print("PASS click ->", r.get("result"))

    # unknown action must also be a clean 400
    try:
        server.web_action(eid, server.WebAction(action="bogus"))
        print("FAIL unknown action accepted"); ok = False
    except Exception as exc:
        print(f"PASS unknown action rejected (status={getattr(exc,'status_code',None)})")

    out = server.close_episode(eid)
    print("PASS episode closed ->", out)

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES PRESENT")


if __name__ == "__main__":
    main()
