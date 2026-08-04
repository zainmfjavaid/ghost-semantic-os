from __future__ import annotations

import json
import unittest
import uuid
from pathlib import Path
from typing import Any, Mapping

from envserver.semantic.adapters import AdapterContext, AdapterRegistry
from envserver.semantic.gimp_adapter import GimpSemanticAdapter
from envserver.semantic.media_adapter import MediaMetadataAdapter
from envserver.semantic.terminal_adapter import SandboxedTerminalAdapter
from envserver.semantic.protocol import ErrorCode, ProtocolError, SideEffectState
from envserver.semantic.remaining_apps import (
    INVENTORY_ADAPTER_IDS,
    create_remaining_application_adapters,
)
from envserver.semantic.runtime import SemanticRuntime
from envserver.semantic.thunderbird_adapter import ThunderbirdSemanticAdapter


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "OSWorld" / "protocol" / "linux-app-inventory.json"


class FakeNativeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Mapping[str, Any] | None]] = []
        self.revision = 1
        self.fail_action: Mapping[str, Any] | None = None

    def __call__(
        self, method: str, path: str, payload: Mapping[str, Any] | None
    ) -> Mapping[str, Any]:
        self.calls.append((method, path, payload))
        if path.endswith("/health"):
            return {
                "ok": True,
                "result": {"ok": True, "bridge_version": "test-1"},
            }
        if path.endswith("/revision"):
            return {"ok": True, "result": {"revision": f"native-{self.revision}"}}
        if path.endswith("/resolve-ref"):
            return {"ok": True, "result": {"ref": (payload or {}).get("ref")}}
        if path.endswith("/query"):
            resource = str((payload or {}).get("resource"))
            return {
                "ok": True,
                "result": {
                    "records": [{
                        "ref": "native-entity",
                        "kind": resource.rstrip("s"),
                        "name": "semantic record",
                        "advertised_actions": [],
                    }],
                    "revision": f"native-{self.revision}",
                    "execution_path": "app_bridge",
                },
            }
        if path.endswith("/act"):
            if self.fail_action is not None:
                return self.fail_action
            self.revision += 1
            return {
                "ok": True,
                "status": "ok",
                "result": {
                    "status": "applied",
                    "changed": True,
                    "execution_path": "app_bridge",
                    "revision": f"native-{self.revision}",
                    "delta": {"action": (payload or {}).get("action")},
                },
            }
        if path.endswith("/close"):
            return {"ok": True, "result": {}}
        raise AssertionError(path)


class RemainingApplicationInventoryTests(unittest.TestCase):
    def test_factory_covers_every_remaining_inventory_adapter(self) -> None:
        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
        declared = {
            adapter_id
            for family in inventory["canonical_families"]
            for adapter_id in family["adapter_ids"]
        }
        adapters = create_remaining_application_adapters(None)
        actual = {adapter.adapter_id for adapter in adapters}
        self.assertEqual(actual, INVENTORY_ADAPTER_IDS)
        self.assertTrue(INVENTORY_ADAPTER_IDS <= declared)

    def test_every_adapter_registers_and_has_complete_descriptor(self) -> None:
        registry = AdapterRegistry()
        adapters = create_remaining_application_adapters(None)
        for adapter in adapters:
            registry.register(adapter)
            descriptor = adapter.descriptor()
            self.assertEqual(descriptor["adapter_id"], adapter.adapter_id)
            self.assertEqual(set(descriptor["resources"]), set(adapter.resources))
            self.assertEqual(set(descriptor["actions"]), set(adapter.capabilities))
            self.assertTrue(descriptor["execution_paths"])
            self.assertEqual(
                set(descriptor["resource_schemas"]), set(adapter.resources)
            )
            self.assertEqual(
                set(descriptor["action_schemas"]), set(adapter.capabilities)
            )
            for action in descriptor["action_schemas"].values():
                self.assertIn(action["risk"], {"reversible", "persistent", "external"})
                self.assertIsInstance(action["idempotent"], bool)
                self.assertIn("arguments_schema", action)
            forbidden = {
                "screenshot", "coordinates", "keyboard", "pyautogui",
                "browser_javascript", "host_python", "shell_gui",
            }
            self.assertTrue(forbidden.isdisjoint(descriptor["execution_paths"]))

    def test_resource_namespaces_are_authoritative(self) -> None:
        owners: dict[str, str] = {}
        for adapter in create_remaining_application_adapters(None):
            for resource in adapter.resources:
                self.assertNotIn(resource, owners)
                owners[resource] = adapter.adapter_id


class RemainingApplicationContractTests(unittest.TestCase):
    @staticmethod
    def _dispatch(runtime: SemanticRuntime, operation: str, payload: Mapping[str, Any]):
        return runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": "remaining-apps-episode",
            "operation": operation,
            "payload": payload,
        })

    def test_missing_bridge_is_explicit_representation_gap(self) -> None:
        adapter = GimpSemanticAdapter()
        health = adapter.health()
        self.assertEqual(health["status"], "unavailable")
        self.assertFalse(health["probe"]["ok"])
        self.assertEqual(health["probe"]["code"], "representation_gap")
        with self.assertRaises(ProtocolError) as caught:
            adapter.observe(
                AdapterContext("episode", "gimp.layers", "request", None),
                {"resource": "gimp.layers", "scope": {}},
            )
        self.assertEqual(caught.exception.code, ErrorCode.REPRESENTATION_GAP)
        self.assertEqual(caught.exception.side_effect_state, SideEffectState.NONE)
        self.assertEqual(
            caught.exception.to_dict()["recovery"]["suggested_resource"],
            "system.health",
        )

    def test_base_guest_route_404_is_an_explicit_integration_gap(self) -> None:
        adapter = GimpSemanticAdapter(
            lambda *_: {"ok": False, "error": {"code": "not_found"}}
        )
        self.assertEqual(adapter.probe()["code"], "representation_gap")
        with self.assertRaises(ProtocolError) as caught:
            adapter.observe(
                AdapterContext("episode", "gimp.layers", "request", None),
                {"resource": "gimp.layers", "scope": {}, "parameters": {}},
            )
        self.assertEqual(caught.exception.code, ErrorCode.REPRESENTATION_GAP)
        self.assertEqual(caught.exception.side_effect_state, SideEffectState.NONE)

    def test_query_action_health_revision_and_close(self) -> None:
        transport = FakeNativeTransport()
        adapter = MediaMetadataAdapter(transport)
        self.assertTrue(adapter.probe()["ok"])
        self.assertEqual(adapter.revision(), "native-1")
        observation = adapter.observe(
            AdapterContext("episode", "media.metadata", "query", None),
            {
                "resource": "media.metadata",
                "scope": {"path": "/home/oai/share/input.mp3"},
                "parameters": {},
            },
        )
        self.assertEqual(observation.native_revision, "native-1")
        self.assertEqual(observation.items[0]["ref"], "native-entity")
        action = adapter.act(
            AdapterContext("episode", "media.metadata", "act", "native-1"),
            {
                "target": {"ref": "native-entity"},
                "action": "edit_metadata",
                "arguments": {"fields": {"artist": "Example"}},
                "confirm": True,
            },
        )
        self.assertTrue(action.changed)
        self.assertEqual(action.native_revision, "native-2")
        self.assertEqual(action.result["execution_path"], "app_bridge")
        self.assertEqual(adapter.resolve_ref("native-entity")["ref"], "native-entity")
        adapter.close()
        adapter.close()
        with self.assertRaises(ProtocolError) as closed:
            adapter.observe(
                AdapterContext("episode", "media.metadata", "query", None),
                {
                    "resource": "media.metadata",
                    "scope": {"path": "/home/oai/share/input.mp3"},
                },
            )
        self.assertEqual(closed.exception.code, ErrorCode.ADAPTER_UNAVAILABLE)

    def test_unknown_action_is_rejected_before_transport(self) -> None:
        transport = FakeNativeTransport()
        adapter = GimpSemanticAdapter(transport)
        with self.assertRaises(ProtocolError) as caught:
            adapter.act(
                AdapterContext("episode", "gimp.layers", "act", None),
                {"action": "click", "arguments": {}, "target": {"ref": "x"}},
            )
        self.assertEqual(caught.exception.code, ErrorCode.UNSUPPORTED)
        self.assertEqual(transport.calls, [])

    def test_application_query_assembles_all_private_pages_at_one_revision(self) -> None:
        class PagedTransport(FakeNativeTransport):
            def __call__(self, method, path, payload):
                if not path.endswith("/query"):
                    return super().__call__(method, path, payload)
                offset = int((payload or {}).get("internal_offset", 0))
                records = [
                    {"ref": f"native-{index}", "kind": "media.file", "index": index}
                    for index in range(offset, min(offset + 100, 235))
                ]
                return {
                    "ok": True,
                    "result": {
                        "records": records,
                        "revision": "native-many-1",
                        "total": 235,
                        "truncated": offset + 100 < 235,
                        "next_internal_offset": offset + 100
                        if offset + 100 < 235 else None,
                    },
                }

        adapter = MediaMetadataAdapter(PagedTransport())
        observation = adapter.observe(
            AdapterContext("episode", "media.files", "query", None),
            {
                "resource": "media.files",
                "scope": {"path": "/home/oai/share"},
                "parameters": {},
            },
        )
        self.assertEqual(len(observation.items), 235)
        self.assertEqual(observation.summary["transport_pages"], 3)
        self.assertEqual(observation.native_revision, "native-many-1")

    def test_unknown_side_effect_after_action_is_uncertain(self) -> None:
        transport = FakeNativeTransport()
        transport.fail_action = {
            "ok": False,
            "error": {
                "code": "timeout",
                "message": "bridge disconnected",
                "retryable": False,
                "side_effect_state": "unknown",
            },
        }
        adapter = MediaMetadataAdapter(transport)
        with self.assertRaises(ProtocolError) as caught:
            adapter.act(
                AdapterContext("episode", "media.metadata", "act", None),
                {
                    "action": "edit_metadata",
                    "arguments": {"fields": {"artist": "Example"}},
                    "target": {"ref": "x"},
                },
            )
        self.assertEqual(caught.exception.code, ErrorCode.UNCERTAIN)
        self.assertEqual(caught.exception.side_effect_state, SideEffectState.UNKNOWN)

    def test_thunderbird_send_requires_idempotency_key(self) -> None:
        transport = FakeNativeTransport()
        adapter = ThunderbirdSemanticAdapter(transport)
        with self.assertRaises(ProtocolError) as caught:
            adapter.act(
                AdapterContext("episode", "mail.composer", "act", None),
                {"action": "send", "arguments": {}, "target": {"ref": "draft"}},
            )
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(transport.calls, [])

    def test_process_requires_argv_and_blocks_desktop_environment(self) -> None:
        transport = FakeNativeTransport()
        adapter = SandboxedTerminalAdapter(transport)
        context = AdapterContext("episode", "terminal.sessions", "act", None)
        with self.assertRaises(ProtocolError) as shell:
            adapter.act(context, {
                "action": "exec",
                "arguments": {"command": "touch /tmp/x", "cwd": "/home/oai"},
                "target": {"ref": "session"},
            })
        self.assertEqual(shell.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(ProtocolError) as display:
            adapter.act(context, {
                "action": "exec",
                "arguments": {
                    "argv": ["python3", "script.py"],
                    "cwd": "/home/oai",
                    "env": {"DISPLAY": ":0"},
                },
                "target": {"ref": "session"},
            })
        self.assertEqual(display.exception.code, ErrorCode.INVALID_REQUEST)
        self.assertEqual(transport.calls, [])

    def test_visual_capabilities_are_disclosed_as_gaps(self) -> None:
        gimp_gaps = GimpSemanticAdapter().descriptor()["known_representation_gaps"]
        media_gaps = MediaMetadataAdapter().descriptor()["known_representation_gaps"]
        self.assertIn("visual_composition", {gap["capability"] for gap in gimp_gaps})
        self.assertIn("visual_content", {gap["capability"] for gap in media_gaps})

    def test_adapter_executes_through_kernel_refs_and_risk_gate(self) -> None:
        transport = FakeNativeTransport()
        adapter = MediaMetadataAdapter(transport)
        runtime = SemanticRuntime(
            episode_id="remaining-apps-episode",
            max_tool_calls=20,
            guest_request=lambda *_: (_ for _ in ()).throw(AssertionError("unused")),
            guest_capabilities=[],
            adapters=[adapter],
        )
        query = self._dispatch(runtime, "query", {
            "resource": "media.metadata",
            "scope": {"path": "/home/oai/share/input.mp3"},
            "where": {},
            "fields": [],
            "order_by": [],
            "parameters": {},
            "limit": 30,
            "freshness": "live",
        })
        self.assertEqual(query["status"], "ok")
        public_ref = query["result"]["records"][0]["ref"]
        denied = self._dispatch(runtime, "act", {
            "target": {"ref": public_ref},
            "action": "edit_metadata",
            "arguments": {"fields": {"artist": "Example"}},
            "preconditions": [], "postconditions": [], "confirm": False,
        })
        self.assertEqual(denied["error"]["code"], "permission_denied")
        applied = self._dispatch(runtime, "act", {
            "target": {"ref": public_ref},
            "action": "edit_metadata",
            "arguments": {"fields": {"artist": "Example"}},
            "preconditions": [], "postconditions": [], "confirm": True,
            "idempotency_key": "metadata-edit-1",
        })
        self.assertEqual(applied["status"], "ok")
        self.assertEqual(applied["result"]["execution_path"], "app_bridge")
        native_actions = [
            call for call in transport.calls if call[1].endswith("/act")
        ]
        self.assertEqual(native_actions[-1][2]["target"], {"ref": "native-entity"})


if __name__ == "__main__":
    unittest.main()
