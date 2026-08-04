from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from typing import Any, Callable, Mapping

from envserver.semantic.adapters import (
    AdapterActionResult,
    AdapterContext,
    AdapterObservation,
    SemanticAdapter,
)
from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.runtime import SemanticRuntime


def _query_payload(resource: str) -> dict[str, Any]:
    return {
        "resource": resource,
        "scope": {},
        "where": {},
        "fields": [],
        "order_by": [],
        "parameters": {},
        "limit": 30,
        "freshness": "live",
    }


class FakeBrowserTargets(SemanticAdapter):
    adapter_id = "browser.targets.test@1"
    resources = frozenset({"browser.targets"})
    capabilities = frozenset()
    resource_actions = {"browser.targets": ()}

    def __init__(self) -> None:
        self.targets: list[dict[str, str]] = [{
            "surface_id": "surface_initial",
            "url": "https://mail.example.test/message/1",
        }]

    def add_target(self, suffix: str, url: str) -> None:
        self.targets.append({"surface_id": f"surface_{suffix}", "url": url})

    def observe(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterObservation:
        del context, payload
        records = [{
            "ref": f"native_{item['surface_id']}",
            "kind": "browser.tab",
            "surface_id": item["surface_id"],
            "url": item["url"],
            "advertised_actions": [],
        } for item in self.targets]
        revision = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
        return AdapterObservation(items=records, native_revision=f"targets-{revision}")

    def act(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterActionResult:
        del context, payload
        raise ProtocolError(ErrorCode.UNSUPPORTED, "browser target fixture is read-only")


class FakeAccessibilityLink:
    def __init__(self, on_invoke: Callable[[], None]) -> None:
        self.on_invoke = on_invoke
        self.act_count = 0
        self.revision = 1

    def request(self, method: str, path: str, payload=None):
        if method == "POST" and path == "/v1/query":
            if payload["resource"] != "ui.elements":
                raise AssertionError(payload["resource"])
            return {"ok": True, "result": {
                "records": [{
                    "ref": "native_mail_link",
                    "kind": "ui.element",
                    "role": "link",
                    "name": "Open related page",
                    "state": {},
                    "advertised_actions": ["invoke"],
                }],
                "revision": f"ui-{self.revision}",
                "total": 1,
            }}
        if method == "POST" and path == "/v1/act":
            if payload["target"] != {"ref": "native_mail_link"}:
                raise AssertionError(payload["target"])
            self.act_count += 1
            self.on_invoke()
            self.revision += 1
            return {"ok": True, "result": {
                "execution_path": "accessibility",
                "invoked": True,
            }}
        raise AssertionError((method, path))


class CrossAppActionReceiptTests(unittest.TestCase):
    def make_runtime(self, on_invoke):
        browser = FakeBrowserTargets()
        guest = FakeAccessibilityLink(lambda: on_invoke(browser))
        runtime = SemanticRuntime(
            episode_id="cross-app-receipts",
            max_tool_calls=40,
            guest_request=guest.request,
            guest_capabilities=[{
                "adapter_id": "universal-atspi.test@1",
                "resources": ["ui.elements"],
                "actions": ["invoke"],
                "resource_actions": {"ui.elements": ["invoke"]},
                "execution_paths": ["accessibility"],
                "accepts_entity_target": True,
            }],
            adapters=[browser],
        )
        return runtime, guest, browser

    @staticmethod
    def dispatch(runtime: SemanticRuntime, operation: str, payload: Mapping[str, Any]):
        return runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": "cross-app-receipts",
            "operation": operation,
            "payload": dict(payload),
        })

    def invoke(self, runtime: SemanticRuntime, *, idempotency_key: str | None = None):
        observed = self.dispatch(runtime, "query", _query_payload("ui.elements"))
        target_ref = observed["result"]["records"][0]["ref"]
        payload = {
            "target": {"ref": target_ref},
            "action": "invoke",
            "arguments": {},
            "preconditions": [],
            "postconditions": [],
            "confirm": False,
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = idempotency_key
        return self.dispatch(runtime, "act", payload), payload

    def test_one_created_tab_receipt_has_unique_surface_ref_and_proven_uri(self):
        runtime, guest, _browser = self.make_runtime(
            lambda browser: browser.add_target("created", "https://console.example.test/home")
        )

        response, _payload = self.invoke(runtime)

        self.assertEqual(response["status"], "ok")
        effect = response["result"]["delta"]["browser_target_effect"]
        self.assertEqual(effect["before_surface_ids"], ["surface_initial"])
        self.assertEqual(
            effect["after_surface_ids"], ["surface_created", "surface_initial"]
        )
        self.assertEqual(effect["created_surface_count"], 1)
        self.assertEqual(effect["creation_verdict"], "one_created")
        self.assertEqual(effect["created_surface_id"], "surface_created")
        self.assertEqual(effect["target_uri"], "https://console.example.test/home")
        self.assertTrue(effect["created_surface_ref"].startswith("ref_"))
        self.assertNotIn("native_surface_created", str(response))
        runtime.state.resolve_ref(
            effect["created_surface_ref"],
            adapter_id="browser.targets.test@1",
            resource="browser.targets",
        )
        self.assertEqual(guest.act_count, 1)

    def test_multiple_created_tabs_are_reported_ambiguous_without_guessed_ref(self):
        def create_two(browser: FakeBrowserTargets) -> None:
            browser.add_target("first", "https://one.example.test/")
            browser.add_target("second", "https://two.example.test/")

        runtime, _guest, _browser = self.make_runtime(create_two)

        response, _payload = self.invoke(runtime)

        effect = response["result"]["delta"]["browser_target_effect"]
        self.assertEqual(effect["created_surface_count"], 2)
        self.assertEqual(effect["creation_verdict"], "ambiguous_multiple_created")
        self.assertNotIn("created_surface_ref", effect)
        self.assertNotIn("target_uri", effect)

    def test_no_created_tab_is_reported_without_fabricated_target(self):
        runtime, _guest, _browser = self.make_runtime(lambda _browser: None)

        response, _payload = self.invoke(runtime)

        effect = response["result"]["delta"]["browser_target_effect"]
        self.assertEqual(effect["created_surface_count"], 0)
        self.assertEqual(effect["creation_verdict"], "none_created")
        self.assertEqual(effect["before_surface_ids"], effect["after_surface_ids"])
        self.assertNotIn("created_surface_ref", effect)
        self.assertNotIn("target_uri", effect)

    def test_idempotency_replay_returns_prior_receipt_without_second_invoke(self):
        runtime, guest, browser = self.make_runtime(
            lambda target_adapter: target_adapter.add_target(
                "created", "https://console.example.test/home"
            )
        )
        first, payload = self.invoke(runtime, idempotency_key="open-link-once")

        replay = self.dispatch(runtime, "act", payload)

        self.assertEqual(replay["status"], "ok")
        self.assertEqual(replay["result"]["receipt_id"], first["result"]["receipt_id"])
        self.assertEqual(replay["result"]["delta"], first["result"]["delta"])
        self.assertEqual(guest.act_count, 1)
        self.assertEqual(len(browser.targets), 2)


if __name__ == "__main__":
    unittest.main()
