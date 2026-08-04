"""A reset failure after VM allocation must close the partial environment."""
from __future__ import annotations

import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))

ocr = types.ModuleType("easyocr")
ocr.Reader = lambda *args, **kwargs: None
sys.modules.setdefault("easyocr", ocr)


class FailingEnv:
    instances: list["FailingEnv"] = []

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.instances.append(self)

    def reset(self, task_config=None):
        raise RuntimeError("forced reset failure")

    def close(self):
        self.closed = True


desktop = types.ModuleType("desktop_env")
desktop_module = types.ModuleType("desktop_env.desktop_env")
desktop_module.DesktopEnv = FailingEnv
desktop.desktop_env = desktop_module
sys.modules["desktop_env"] = desktop
sys.modules["desktop_env.desktop_env"] = desktop_module

import server  # noqa: E402


def main() -> None:
    server.DesktopEnv = FailingEnv
    task = (
        REPO
        / "OSWorld/evaluation_examples/examples/chrome/"
        / "b4f95342-463e-4179-8c3f-193cd7241fb2.json"
    )
    try:
        server.create_episode(server.CreateEpisode(task_path=str(task)))
    except RuntimeError as error:
        assert str(error) == "forced reset failure"
    else:
        raise AssertionError("reset failure unexpectedly succeeded")

    assert len(FailingEnv.instances) == 1
    assert FailingEnv.instances[0].closed
    assert server._episodes == {}
    print("PASS partial environment closed after reset failure")


if __name__ == "__main__":
    main()
