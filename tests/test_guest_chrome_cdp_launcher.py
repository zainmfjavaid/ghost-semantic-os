"""Contracts for the private desktop-mediated Chrome CDP launcher."""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from guest_agent import chrome_cdp_launcher


class GuestChromeCdpLauncherTests(unittest.TestCase):
    def test_ordinary_url_launch_adds_debugging_and_accessibility(self) -> None:
        argv = chrome_cdp_launcher.browser_argv(
            "/usr/bin/google-chrome", ["https://example.test/page"],
        )

        self.assertEqual(argv[0], "/usr/bin/google-chrome")
        self.assertIn("--remote-debugging-address=0.0.0.0", argv)
        self.assertIn("--remote-debugging-port=9222", argv)
        self.assertIn("--force-renderer-accessibility", argv)
        self.assertEqual(argv[-1], "https://example.test/page")

    def test_existing_debugging_port_is_preserved_without_duplicate(self) -> None:
        argv = chrome_cdp_launcher.browser_argv(
            "/usr/bin/chromium",
            ["--remote-debugging-port=1337", "https://example.test"],
        )

        self.assertEqual(argv.count("--remote-debugging-port=1337"), 1)
        self.assertNotIn("--remote-debugging-port=9222", argv)
        self.assertIn("--force-renderer-accessibility", argv)

    def test_main_execs_only_resolved_allowlisted_browser(self) -> None:
        with patch.object(
            chrome_cdp_launcher,
            "resolve_browser",
            return_value="/usr/bin/google-chrome",
        ), patch.object(
            chrome_cdp_launcher,
            "ensure_cdp_bridge",
            return_value=1337,
        ), patch.object(chrome_cdp_launcher.os, "execv") as execute:
            result = chrome_cdp_launcher.main([
                "google-chrome", "https://example.test",
            ])

        self.assertEqual(result, 70)
        command = execute.call_args.args[1]
        self.assertEqual(execute.call_args.args[0], "/usr/bin/google-chrome")
        self.assertIn("--remote-debugging-port=1337", command)
        self.assertEqual(command[-1], "https://example.test")

    def test_existing_public_listener_reuses_private_debug_port(self) -> None:
        with patch.object(
            chrome_cdp_launcher,
            "_port_is_listening",
            return_value=True,
        ), patch.object(chrome_cdp_launcher.subprocess, "Popen") as spawn:
            port = chrome_cdp_launcher.ensure_cdp_bridge()

        self.assertEqual(port, 1337)
        spawn.assert_not_called()

    def test_missing_socat_falls_back_to_direct_debug_port(self) -> None:
        with patch.object(
            chrome_cdp_launcher,
            "_port_is_listening",
            return_value=False,
        ), patch.object(
            chrome_cdp_launcher.Path,
            "is_file",
            return_value=False,
        ), patch.object(chrome_cdp_launcher.subprocess, "Popen") as spawn:
            port = chrome_cdp_launcher.ensure_cdp_bridge()

        self.assertEqual(port, 9222)
        spawn.assert_not_called()

    def test_starts_canonical_socat_bridge_for_indirect_launch(self) -> None:
        process = Mock()
        process.poll.return_value = None
        with patch.object(
            chrome_cdp_launcher,
            "_port_is_listening",
            side_effect=[False, True],
        ), patch.object(
            chrome_cdp_launcher.Path,
            "is_file",
            return_value=True,
        ), patch.object(
            chrome_cdp_launcher.os,
            "access",
            return_value=True,
        ), patch.object(
            chrome_cdp_launcher.subprocess,
            "Popen",
            return_value=process,
        ) as spawn:
            port = chrome_cdp_launcher.ensure_cdp_bridge()

        self.assertEqual(port, 1337)
        command = spawn.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/socat")
        self.assertIn("TCP-LISTEN:9222,reuseaddr,fork", command)
        self.assertIn("TCP:127.0.0.1:1337", command)

    def test_unknown_browser_alias_fails_without_execution(self) -> None:
        with patch.object(chrome_cdp_launcher.os, "execv") as execute:
            result = chrome_cdp_launcher.main(["arbitrary-browser"])

        self.assertEqual(result, 69)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
