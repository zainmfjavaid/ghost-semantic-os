from __future__ import annotations

import json
import unittest
from unittest import mock  # noqa: F401 -- exposes unittest.mock on all supported Pythons

from envserver import guest_semantic


class _Controller:
    def __init__(self) -> None:
        self.script_source = ""
        self.execute_calls = 0
        self.last_envelope = ""

    def execute_python_command(self, _source: str):
        self.execute_calls += 1
        raise AssertionError("the large bootstrap must not be placed in python -c")

    def run_python_script(self, source: str):
        self.script_source = source
        payload = {
            "ok": True,
            "pid": 42,
            "health": {
                "result": {
                    "bundle_hash": guest_semantic.bundle_hash(),
                    "agent_version": "test",
                    "guest_platform": "linux",
                }
            },
        }
        result = {
            "status": "success",
            "output": guest_semantic._MARKER + json.dumps(payload) + "\n",
            "error": "",
        }
        self.last_envelope = json.dumps(result)
        return result


class _Env:
    def __init__(self) -> None:
        self.controller = _Controller()
        self.client_password = "private-test-password"


class GuestSemanticTransportTests(unittest.TestCase):
    def test_large_bundle_uses_script_body_transport(self) -> None:
        env = _Env()
        state = guest_semantic.bootstrap(env, "episode")

        self.assertEqual(env.controller.execute_calls, 0)
        self.assertGreater(len(env.controller.script_source), 128 * 1024)
        compile(env.controller.script_source, "<guest-semantic-bootstrap>", "exec")
        self.assertEqual(state["bundle_hash"], guest_semantic.bundle_hash())
        self.assertEqual(state["guest_platform"], "linux")
        self.assertIn(
            "NOPASSWD: /usr/local/libexec/ghost-semantic-install-package *",
            env.controller.script_source,
        )
        self.assertIn(
            "['/usr/bin/sudo', '-n', '--', "
            "'/usr/local/libexec/ghost-semantic-install-package', 'INVALID']",
            env.controller.script_source,
        )
        self.assertIn("chrome_cdp_launcher.py", guest_semantic._BUNDLE_FILES)
        self.assertIn("desktop_specs = (", env.controller.script_source)
        self.assertIn("google-chrome.desktop", env.controller.script_source)
        self.assertIn("chromium.desktop", env.controller.script_source)
        self.assertIn("x-scheme-handler/https", env.controller.script_source)
        self.assertIn("update-desktop-database", env.controller.script_source)
        self.assertNotIn("shell=True", env.controller.script_source)
        self.assertNotIn("private-test-password", json.dumps(state))
        self.assertNotIn("private-test-password", env.controller.last_envelope)

    def test_bootstrap_rejects_missing_privilege_credential(self) -> None:
        env = _Env()
        env.client_password = ""
        with self.assertRaises(guest_semantic.GuestSemanticError) as raised:
            guest_semantic.bootstrap(env, "episode")
        self.assertIn("privilege credential", str(raised.exception))
        self.assertEqual(env.controller.execute_calls, 0)
        self.assertEqual(env.controller.script_source, "")

    def test_missing_envelope_includes_guest_error(self) -> None:
        with self.assertRaises(guest_semantic.GuestSemanticError) as raised:
            guest_semantic._decode_envelope({"error": "guest failed"})
        self.assertIn("guest failed", str(raised.exception))

    def test_small_daemon_request_uses_inline_command_transport(self) -> None:
        state = {"token": "private-token", "port": 8765}
        expected = {"ok": True, "result": {"healthy": True}}
        with unittest.mock.patch.object(
            guest_semantic, "_execute", return_value=expected
        ) as execute, unittest.mock.patch.object(
            guest_semantic, "_run_script"
        ) as run_script:
            result = guest_semantic.request(
                object(), state, "POST", "/v1/query", {"resource": "system.health"}
            )

        self.assertEqual(result, expected)
        execute.assert_called_once()
        run_script.assert_not_called()

    def test_large_daemon_request_uses_script_body_transport(self) -> None:
        state = {"token": "private-token", "port": 8765}
        expected = {"ok": True, "result": {"complete": False}}
        payload = {
            "action": "stage_base64_chunk",
            "arguments": {
                "transfer_id": "a" * 24,
                "offset": 0,
                "base64": "A" * (guest_semantic._MAX_INLINE_REQUEST_SCRIPT_BYTES + 1),
                "final": False,
            },
        }
        with unittest.mock.patch.object(
            guest_semantic, "_execute"
        ) as execute, unittest.mock.patch.object(
            guest_semantic, "_run_script", return_value=expected
        ) as run_script:
            result = guest_semantic.request(
                object(), state, "POST", "/v1/act", payload
            )

        self.assertEqual(result, expected)
        execute.assert_not_called()
        run_script.assert_called_once()
        routed_source = run_script.call_args.args[1]
        self.assertIn("stage_base64_chunk", routed_source)
        self.assertGreater(
            len(routed_source.encode("utf-8")),
            guest_semantic._MAX_INLINE_REQUEST_SCRIPT_BYTES,
        )


if __name__ == "__main__":
    unittest.main()
