"""Run the real env server with DesktopEnv stubbed out.

Purpose: exercise the ACTUAL agent loop -- pi session, tool definitions, nudge
loop, termination, image handling -- without an OSWorld VM. Everything on the
web path is real (real server code, real WebProvider, real Chrome over CDP);
only the desktop VM is fake.

This is a PLUMBING test, not a benchmark. The stub evaluator always returns 0,
so no score produced here means anything about task success, and nothing from
this file may be reported as an OSWorld result.
"""
from __future__ import annotations

import io
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))


def _png() -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (1280, 800), (245, 246, 250)).save(b, format="PNG")
    return b.getvalue()


class _StubEnv:
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
        return 0.0   # always zero: this harness cannot grade anything

    def close(self):
        pass


fake_pkg = types.ModuleType("desktop_env")
fake_mod = types.ModuleType("desktop_env.desktop_env")
fake_mod.DesktopEnv = _StubEnv
fake_pkg.desktop_env = fake_mod
sys.modules["desktop_env"] = fake_pkg
sys.modules["desktop_env.desktop_env"] = fake_mod

import server  # noqa: E402

server.DesktopEnv = _StubEnv

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(server.app, host="127.0.0.1", port=8077, log_level="warning")
