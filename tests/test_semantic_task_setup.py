"""Hermetic contracts for generic semantic task launch preparation."""
from __future__ import annotations

import copy
import sys
import types
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "envserver"))

# server.py imports OSWorld's environment at module load. Task preparation is
# pure data transformation, so keep this unit contract independent of a VM.
desktop = types.ModuleType("desktop_env")
desktop_module = types.ModuleType("desktop_env.desktop_env")
desktop_module.DesktopEnv = object
desktop.desktop_env = desktop_module
sys.modules.setdefault("desktop_env", desktop)
sys.modules.setdefault("desktop_env.desktop_env", desktop_module)

import server  # noqa: E402


class SemanticTaskSetupTests(unittest.TestCase):
    @staticmethod
    def _task(command):
        return {
            "instruction": "opaque and untouched",
            "config": [{
                "type": "launch",
                "parameters": {"command": command, "wait": True},
            }],
        }

    def test_vscode_argv_aliases_receive_accessibility_after_executable(self) -> None:
        for executable in (
            "code", "/usr/bin/code", "code-insiders", "code-oss", "codium", "vscodium",
        ):
            with self.subTest(executable=executable):
                original = self._task([
                    executable, "--new-window", "/home/oai/share/project",
                ])
                untouched = copy.deepcopy(original)

                prepared = server._semantic_task_setup(original)

                self.assertEqual(original, untouched)
                self.assertEqual(
                    prepared["config"][0]["parameters"]["command"],
                    [
                        executable,
                        "--force-renderer-accessibility",
                        "--new-window",
                        "/home/oai/share/project",
                    ],
                )
                self.assertEqual(prepared["instruction"], original["instruction"])
                self.assertTrue(prepared["config"][0]["parameters"]["wait"])

    def test_existing_accessibility_flag_is_idempotent_and_not_reordered(self) -> None:
        command = [
            "code", "--new-window", "--force-renderer-accessibility", "/tmp/project",
        ]

        once = server._semantic_task_setup(self._task(command))
        twice = server._semantic_task_setup(once)

        self.assertEqual(once, twice)
        self.assertEqual(
            twice["config"][0]["parameters"]["command"], command,
        )

    def test_shell_string_fails_closed_without_rewriting(self) -> None:
        task = self._task("code --new-window /tmp/project")

        self.assertEqual(server._semantic_task_setup(task), task)

    def test_unrelated_argv_and_wrapper_commands_are_unchanged(self) -> None:
        commands = (
            ["thunderbird", "--new-window"],
            ["bash", "-lc", "code --new-window /tmp/project"],
            ["env", "GDK_BACKEND=x11", "code", "/tmp/project"],
        )
        for command in commands:
            with self.subTest(command=command):
                task = self._task(command)
                self.assertEqual(server._semantic_task_setup(task), task)


if __name__ == "__main__":
    unittest.main()
