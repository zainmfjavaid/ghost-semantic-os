from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from envserver.semantic.adapters import AdapterContext
from envserver.semantic.process_adapter import (
    BubblewrapBackend,
    ProcessAdapter,
    ProcessExecution,
    ProcessOutcome,
)
from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.runtime import SemanticRuntime


class FakeBackend:
    name = "fake-isolated"

    def __init__(self, outcome: ProcessOutcome | None = None) -> None:
        self.requests: list[ProcessExecution] = []
        self.outcome = outcome or ProcessOutcome(0, b"ok", b"", 0.01, False)

    def execute(self, request: ProcessExecution) -> ProcessOutcome:
        self.requests.append(request)
        return self.outcome


def _context(resource: str) -> AdapterContext:
    return AdapterContext("episode", resource, "request", None)


class ProcessAdapterTests(unittest.TestCase):
    def test_argv_execution_is_bounded_and_uses_public_guest_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend()
            adapter = ProcessAdapter({"/home/user/work": temporary}, backend=backend)
            manager = adapter.observe(_context("process.sessions"), {}).items[0]
            result = adapter.act(_context("process.sessions"), {
                "target": {"ref": manager["ref"]},
                "action": "process.exec",
                "arguments": {
                    "argv": ["python3", "-c", "print('literal')"],
                    "cwd": "/home/user/work",
                    "stdin": "value",
                    "timeout_seconds": 10,
                },
            })
            self.assertTrue(result.changed)
            self.assertEqual(backend.requests[0].argv[0], "python3")
            self.assertEqual(backend.requests[0].cwd, "/home/user/work")
            self.assertNotIn(temporary, str(result.result))
            self.assertEqual(result.result["stdout"], "ok")

    def test_no_shell_string_or_outside_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = ProcessAdapter({"/work": temporary}, backend=FakeBackend())
            manager = adapter.observe(_context("process.sessions"), {}).items[0]
            base = {"target": {"ref": manager["ref"]}, "action": "process.exec"}
            with self.assertRaises(ProtocolError) as argv:
                adapter.act(_context("process.sessions"), {**base, "arguments": {"argv": "echo hi", "cwd": "/work"}})
            self.assertEqual(argv.exception.code, ErrorCode.INVALID_REQUEST)
            with self.assertRaises(ProtocolError) as outside:
                adapter.act(_context("process.sessions"), {**base, "arguments": {"argv": ["echo", "hi"], "cwd": "/etc"}})
            self.assertEqual(outside.exception.code, ErrorCode.PERMISSION_DENIED)

    def test_bubblewrap_command_has_network_and_session_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = BubblewrapBackend({"/work": Path(temporary)}, executable="/usr/bin/true")
            command = backend._command(ProcessExecution(("echo", "hello"), "/work", b"", 1))
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertNotIn("--share-net", command)
            joined = " ".join(command)
            for secret in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS", "AT_SPI_BUS_ADDRESS"):
                self.assertNotIn(secret, joined)
            self.assertEqual(command[-2:], ["echo", "hello"])

    def test_guest_privilege_helper_cannot_escape_through_process_sandbox(self) -> None:
        """The process tool never runs in the nested guest privilege domain.

        Even if policy code guesses the helper path, it remains behind a new
        user/mount namespace with no guest sudoers configuration, guest root,
        session bus, or raw nested-desktop filesystem mounted into it.
        """
        with tempfile.TemporaryDirectory() as temporary:
            backend = BubblewrapBackend({"/work": Path(temporary)}, executable="/usr/bin/true")
            helper = "/usr/local/libexec/ghost-semantic-install-package"
            command = backend._command(
                ProcessExecution(("sudo", "-n", "--", helper, "sl"), "/work", b"", 1)
            )
            self.assertIn("--unshare-all", command)
            self.assertIn("--clearenv", command)
            self.assertNotIn("/etc/sudoers", command)
            self.assertNotIn("/etc/sudoers.d", command)
            self.assertNotIn("/run", command)
            self.assertEqual(command[-5:], ["sudo", "-n", "--", helper, "sl"])
            # The only writable bind is the declared artifact root, never a
            # guest system/runtime path that could contain the privileged helper.
            bind_sources = [
                command[index + 1]
                for index, value in enumerate(command[:-2])
                if value == "--bind"
            ]
            self.assertEqual(bind_sources, [str(Path(temporary).resolve())])

    def test_timeout_is_typed_unknown_and_output_hashes_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            backend = FakeBackend(ProcessOutcome(None, b"partial", b"timeout", 2.0, True))
            adapter = ProcessAdapter({"/work": temporary}, backend=backend)
            manager = adapter.observe(_context("process.sessions"), {}).items[0]
            with self.assertRaises(ProtocolError) as caught:
                adapter.act(_context("process.sessions"), {
                    "target": {"ref": manager["ref"]}, "action": "process.exec",
                    "arguments": {"argv": ["tool"], "cwd": "/work", "timeout_seconds": 1},
                })
            self.assertEqual(caught.exception.code, ErrorCode.TIMEOUT)
            self.assertEqual(caught.exception.side_effect_state.value, "unknown")
            history = adapter.observe(_context("process.runs"), {}).items
            self.assertEqual(len(history), 1)
            self.assertTrue(history[0]["timed_out"])

    def test_runtime_requires_confirmation_from_published_persistent_risk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = ProcessAdapter({"/work": temporary}, backend=FakeBackend())
            runtime = SemanticRuntime(
                episode_id="episode", max_tool_calls=10,
                guest_request=lambda *_args: {}, guest_capabilities=(), adapters=(adapter,),
            )
            query = runtime.dispatch({
                "protocol_version": "1.0", "request_id": "q", "episode_id": "episode",
                "operation": "query", "payload": {
                    "resource": "process.sessions", "scope": {}, "where": {}, "fields": [],
                    "order_by": [], "parameters": {}, "limit": 30, "freshness": "live",
                },
            })
            ref = query["result"]["records"][0]["ref"]
            payload = {
                "target": {"ref": ref}, "action": "process.exec",
                "arguments": {"argv": ["true"], "cwd": "/work"},
                "preconditions": [], "postconditions": [], "timeout_ms": 10_000,
                "idempotency_key": None,
            }
            denied = runtime.dispatch({
                "protocol_version": "1.0", "request_id": "deny", "episode_id": "episode",
                "operation": "act", "payload": {**payload, "confirm": False},
            })
            self.assertEqual(denied["error"]["code"], "permission_denied")
            applied = runtime.dispatch({
                "protocol_version": "1.0", "request_id": "apply", "episode_id": "episode",
                "operation": "act", "payload": {**payload, "confirm": True},
            })
            self.assertEqual(applied["status"], "ok")
            self.assertEqual(applied["result"]["status"], "applied")


if __name__ == "__main__":
    unittest.main()
