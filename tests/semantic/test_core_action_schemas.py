"""Truthfulness checks for core semantic action contracts."""
from __future__ import annotations

import unittest

from envserver.semantic.browser_adapter import AsyncBrowserAdapter
from envserver.semantic.runtime import GuestProxyAdapter
from guest_agent.semantic_agent import CAPABILITIES


class CoreActionSchemaTests(unittest.TestCase):
    @staticmethod
    def _assert_complete_nonpermissive(test, descriptor):
        actions = set(descriptor["actions"])
        schemas = descriptor["action_schemas"]
        test.assertEqual(actions, set(schemas))
        for action in actions:
            arguments = schemas[action]["arguments_schema"]
            test.assertEqual(arguments.get("type"), "object", action)
            test.assertIs(arguments.get("additionalProperties"), False, action)
            test.assertIsInstance(arguments.get("properties"), dict, action)

    def test_browser_actions_have_exact_nonpermissive_contracts(self):
        descriptor = AsyncBrowserAdapter.__new__(AsyncBrowserAdapter).descriptor()
        self._assert_complete_nonpermissive(self, descriptor)
        expected_required = {"select_option": {"value"}}
        for action, schema in descriptor["action_schemas"].items():
            self.assertEqual(
                set(schema["arguments_schema"].get("required", ())),
                expected_required.get(action, set()),
                action,
            )

    def test_guest_core_actions_have_exact_nonpermissive_contracts(self):
        expected_required = {
            "universal-atspi@1": {
                "set_value": {"value"}, "choose_path": {"path"},
            },
            "guest-filesystem@1": {
                "create_directory": {"path"},
                "copy": {"source", "destination"},
                "move": {"source", "destination"},
                "rename": {"source", "destination"},
                "write_text": {"path"},
                "write_base64_atomic": {"path", "base64"},
                "extract_archive": {"source", "destination"},
                "create_desktop_entry": {"name", "url"},
            },
            "guest-os@1": {
                "launch": {"desktop_id"},
                "set_setting": {"schema", "key", "value"},
                "write_clipboard": {"text"},
                "set_audio_volume": {"percent"},
                "set_audio_muted": {"muted"},
                "set_wallpaper": {"path"},
                "install_package": {"name"},
            },
        }
        by_id = {item["adapter_id"]: item for item in CAPABILITIES}
        for adapter_id, required_by_action in expected_required.items():
            descriptor = GuestProxyAdapter(by_id[adapter_id], lambda *_args: {}).descriptor()
            self._assert_complete_nonpermissive(self, descriptor)
            for action, schema in descriptor["action_schemas"].items():
                self.assertEqual(
                    set(schema["arguments_schema"].get("required", ())),
                    required_by_action.get(action, set()),
                    f"{adapter_id}:{action}",
                )


if __name__ == "__main__":
    unittest.main()
