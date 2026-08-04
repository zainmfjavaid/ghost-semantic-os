from __future__ import annotations

import json
import unittest
import uuid

from envserver.semantic.browser_adapter import AsyncBrowserAdapter
from envserver.semantic.research_adapter import PublicResearchAdapter
from envserver.semantic.runtime import SemanticRuntime
from guest_agent.semantic_agent import CAPABILITIES


def query_payload(resource: str, *, where=None):
    return {
        "resource": resource,
        "scope": {},
        "where": where or {},
        "fields": [],
        "order_by": [],
        "parameters": {},
        "limit": 30,
        "freshness": "live",
    }


class FakeGuest:
    def __init__(self) -> None:
        self.selected = False
        self.revision = 1

    def request(self, method, path, payload=None):
        if method == "POST" and path == "/v1/query":
            resource = payload["resource"]
            if resource == "ui.elements":
                records = [{
                    "ref": "private-native-ref",
                    "kind": "ui.element",
                    "role": "button",
                    "name": "Save",
                    "state": {"selected": self.selected},
                    "advertised_actions": ["invoke"],
                }]
                return {"ok": True, "result": {
                    "records": records,
                    "revision": f"native-{self.revision}",
                    "total": 1,
                }}
            raise AssertionError(resource)
        if method == "POST" and path == "/v1/act":
            self.assert_target(payload)
            self.selected = not self.selected
            self.revision += 1
            return {"ok": True, "result": {
                "execution_path": "accessibility", "selected": self.selected,
            }}
        raise AssertionError((method, path))

    def assert_target(self, payload):
        if payload["target"] != {"ref": "private-native-ref"}:
            raise AssertionError(payload["target"])


class FakeScopedGuest:
    def __init__(self) -> None:
        self.revision = 1
        self.query_scopes = []

    def request(self, method, path, payload=None):
        if method == "POST" and path == "/v1/query":
            self.query_scopes.append(dict(payload.get("scope") or {}))
            if payload.get("scope", {}).get("path") != "/home/user/document.txt":
                return {"ok": False, "error": {
                    "code": "invalid_request", "message": "path is required",
                    "retryable": False, "side_effect_state": "none",
                }}
            return {"ok": True, "result": {
                "records": [{"ref": "private-file", "kind": "filesystem.file"}],
                "revision": f"file-{self.revision}", "total": 1,
            }}
        if method == "POST" and path == "/v1/act":
            self.revision += 1
            return {"ok": True, "result": {"execution_path": "native_api"}}
        raise AssertionError((method, path))


class FakeVolatileNativeRefGuest:
    def __init__(self) -> None:
        self.query_count = 0
        self.last_ref = ""
        self.acted = False

    def request(self, method, path, payload=None):
        if method == "POST" and path == "/v1/query":
            self.query_count += 1
            self.last_ref = f"volatile-native-{self.query_count}"
            return {"ok": True, "result": {
                "records": [{
                    "ref": self.last_ref, "kind": "ui.element", "role": "dialog",
                    "name": "Open File", "child_count": 2,
                    "advertised_actions": ["dismiss"],
                }],
                "revision": f"volatile-{self.query_count}", "total": 1,
            }}
        if method == "POST" and path == "/v1/act":
            if payload["target"] != {"ref": self.last_ref}:
                raise AssertionError(payload["target"])
            self.acted = True
            return {"ok": True, "result": {"execution_path": "accessibility"}}
        raise AssertionError((method, path))


class SemanticRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.guest = FakeGuest()
        self.runtime = SemanticRuntime(
            episode_id="episode-test",
            max_tool_calls=100,
            guest_request=self.guest.request,
            guest_capabilities=[{
                "adapter_id": "universal-atspi@1",
                "resources": ["ui.elements"],
                "actions": ["invoke"],
                "execution_paths": ["accessibility"],
            }],
        )

    def dispatch(self, operation, payload):
        return self.runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": "episode-test",
            "operation": operation,
            "payload": payload,
        })

    def test_query_is_read_only_and_refs_hide_native_identity(self):
        first = self.dispatch("query", query_payload("ui.elements"))
        second = self.dispatch("query", query_payload("ui.elements"))
        self.assertEqual(first["status"], "ok")
        self.assertEqual(first["before_revision"], first["after_revision"])
        self.assertEqual(first["after_revision"], second["after_revision"])
        first_ref = first["result"]["records"][0]["ref"]
        second_ref = second["result"]["records"][0]["ref"]
        self.assertEqual(first_ref, second_ref)
        self.assertNotIn("private-native-ref", str(first))

    def test_capability_records_advertise_a_queryable_kind(self):
        response = self.dispatch("query", query_payload("system.capabilities"))
        self.assertEqual(response["status"], "ok")
        self.assertTrue(response["result"]["records"])
        self.assertTrue(all(
            record["kind"] == "system.capability"
            for record in response["result"]["records"]
        ))
        capability_ref = response["result"]["records"][0]["ref"]
        detail = query_payload("system.capability")
        detail["scope"] = {"ref": capability_ref}
        dereferenced = self.dispatch("query", detail)
        self.assertEqual(dereferenced["status"], "ok")
        self.assertEqual(len(dereferenced["result"]["records"]), 1)
        self.assertEqual(
            dereferenced["result"]["records"][0]["adapter_id"],
            response["result"]["records"][0]["adapter_id"],
        )
        detail_ref = dereferenced["result"]["records"][0]["ref"]
        repeated = query_payload("system.capability")
        repeated["scope"] = {"ref": detail_ref}
        repeated_result = self.dispatch("query", repeated)
        self.assertEqual(repeated_result["status"], "ok")
        self.assertEqual(
            repeated_result["result"]["records"][0]["adapter_id"],
            response["result"]["records"][0]["adapter_id"],
        )

    def test_resource_capability_ref_is_scoped_and_survives_unrelated_queries(self):
        listing = self.dispatch("query", query_payload("system.capabilities"))
        resource_summary = next(
            record for record in listing["result"]["records"]
            if record.get("capability_type") == "resource"
            and record.get("resource") == "ui.elements"
        )
        adapter_summary = next(
            record for record in listing["result"]["records"]
            if record.get("capability_type") == "adapter"
        )
        detail_query = query_payload("system.capability")
        detail_query["scope"] = {"ref": resource_summary["ref"]}
        detail = self.dispatch("query", detail_query)
        self.assertEqual(detail["status"], "ok")
        self.assertIsNone(detail["result"].get("data_handle"))
        record = detail["result"]["records"][0]
        self.assertEqual(record["capability_type"], "resource_descriptor")
        self.assertEqual(record["resource"], "ui.elements")
        self.assertEqual(record["resources"], ["ui.elements"])
        self.assertEqual(record["actions"], ["invoke"])
        self.assertEqual(set(record["action_schemas"]), {"invoke"})
        self.assertIn("field_schema", record)
        self.assertIn("parameter_schema", record)
        self.assertIn("verification_schema", record)
        self.assertNotIn("resource_schemas", record)

        # Looking up an adapter descriptor used to change the one shared
        # system.capability revision and stale every prior detail ref.
        adapter_query = query_payload("system.capability")
        adapter_query["scope"] = {"ref": adapter_summary["ref"]}
        adapter_detail = self.dispatch("query", adapter_query)
        self.assertEqual(adapter_detail["status"], "ok")
        repeated_query = query_payload("system.capability")
        repeated_query["scope"] = {"ref": record["ref"]}
        repeated = self.dispatch("query", repeated_query)
        self.assertEqual(repeated["status"], "ok")
        self.assertEqual(repeated["result"]["records"][0]["ref"], record["ref"])
        self.assertEqual(repeated["after_revision"], detail["after_revision"])

    def test_large_adapter_descriptor_handle_survives_other_capability_queries(self):
        actions = [f"action_{index:03d}" for index in range(160)]
        descriptor = {
            "adapter_id": "large.generic@1",
            "resources": ["large.first", "large.second"],
            "actions": actions,
            "execution_paths": ["native_api"],
            "resource_actions": {
                "large.first": [actions[0]],
                "large.second": [actions[1]],
            },
            "action_schemas": {
                action: {
                    "value": "required string value with a bounded semantic description"
                }
                for action in actions
            },
        }
        runtime = SemanticRuntime(
            episode_id="large-capabilities",
            max_tool_calls=20,
            guest_request=self.guest.request,
            guest_capabilities=[descriptor],
        )

        def dispatch(resource, *, scope=None, parameters=None):
            payload = query_payload(resource)
            payload["scope"] = scope or {}
            payload["parameters"] = parameters or {}
            return runtime.dispatch({
                "protocol_version": "1.0", "request_id": str(uuid.uuid4()),
                "episode_id": "large-capabilities", "operation": "query",
                "payload": payload,
            })

        listing = dispatch("system.capabilities")
        adapter_ref = next(
            record["ref"] for record in listing["result"]["records"]
            if record.get("capability_type") == "adapter"
            and record.get("adapter_id") == "large.generic@1"
        )
        descriptor_response = dispatch("system.capability", scope={"ref": adapter_ref})
        handle = descriptor_response["result"].get("data_handle")
        self.assertIsInstance(handle, str)
        other = dispatch(
            "system.capability", parameters={"resource": "large.second"}
        )
        self.assertEqual(other["status"], "ok")
        handle_response = dispatch("system.data_handle", scope={"ref": handle})
        self.assertEqual(handle_response["status"], "ok")
        self.assertTrue(handle_response["result"]["records"])

    def test_resource_discovery_is_two_calls_and_bounded_for_core_families(self):
        browser = AsyncBrowserAdapter.__new__(AsyncBrowserAdapter).descriptor()
        research = PublicResearchAdapter.__new__(PublicResearchAdapter).descriptor()
        guest_by_id = {record["adapter_id"]: record for record in CAPABILITIES}
        runtime = SemanticRuntime(
            episode_id="discovery-measurement",
            max_tool_calls=40,
            guest_request=self.guest.request,
            guest_capabilities=[
                browser,
                guest_by_id["guest-filesystem@1"],
                research,
                guest_by_id["libreoffice.uno@1"],
            ],
        )

        def dispatch(payload):
            return runtime.dispatch({
                "protocol_version": "1.0", "request_id": str(uuid.uuid4()),
                "episode_id": "discovery-measurement", "operation": "query",
                "payload": payload,
            })

        expected_actions = {
            "browser.elements": {"invoke", "set_text", "submit"},
            "filesystem.file": {"write_text", "write_base64_atomic"},
            "research.documents": set(),
            "spreadsheet.cells": {"set_value", "set_formula", "set_text"},
        }
        for resource, required_actions in expected_actions.items():
            with self.subTest(resource=resource):
                before = runtime.state.semantic_operations
                summary_query = query_payload(
                    "system.capabilities",
                    where={"op": "eq", "field": "resource", "value": resource},
                )
                summary = dispatch(summary_query)
                self.assertEqual(summary["status"], "ok")
                self.assertEqual(len(summary["result"]["records"]), 1)
                self.assertIsNone(summary["result"].get("data_handle"))
                detail_query = query_payload("system.capability")
                detail_query["scope"] = {
                    "ref": summary["result"]["records"][0]["ref"]
                }
                detail = dispatch(detail_query)
                self.assertEqual(detail["status"], "ok")
                self.assertEqual(runtime.state.semantic_operations - before, 2)
                self.assertIsNone(detail["result"].get("data_handle"))
                detail_record = detail["result"]["records"][0]
                self.assertEqual(detail_record["resource"], resource)
                self.assertTrue(required_actions <= set(detail_record["actions"]))
                self.assertLess(
                    len(json.dumps(summary, separators=(",", ":"))), 4_000
                )
                self.assertLess(
                    len(json.dumps(detail, separators=(",", ":"))), 8_000
                )

    def test_default_capability_page_is_resource_first(self):
        request = query_payload("system.capabilities")
        request["limit"] = 100
        listing = self.dispatch("query", request)
        records = listing["result"]["records"]
        self.assertTrue(records)
        self.assertEqual(records[0].get("capability_type"), "resource")
        seen_adapter = False
        for record in records:
            if record.get("capability_type") == "adapter":
                seen_adapter = True
            else:
                self.assertFalse(seen_adapter, "resource record appeared after adapter card")
                self.assertIsInstance(record.get("resource"), str)
                self.assertIsInstance(record.get("actions"), list)

    def test_overflow_keeps_decision_bearing_record_fields(self):
        runtime = SemanticRuntime(
            episode_id="overflow-fields",
            max_tool_calls=40,
            guest_request=self.guest.request,
            guest_capabilities=[AsyncBrowserAdapter.__new__(AsyncBrowserAdapter).descriptor()],
        )

        def dispatch(payload):
            return runtime.dispatch({
                "protocol_version": "1.0", "request_id": str(uuid.uuid4()),
                "episode_id": "overflow-fields", "operation": "query",
                "payload": payload,
            })

        request = query_payload("system.capabilities")
        request["limit"] = 100
        listing = dispatch(request)
        self.assertTrue(listing["result"]["truncated"])
        self.assertIsInstance(listing["result"].get("data_handle"), str)
        first = listing["result"]["records"][0]
        self.assertEqual(first["capability_type"], "resource")
        self.assertIsInstance(first["resource"], str)
        self.assertIsInstance(first["actions"], list)

    def test_act_verify_run_and_receipt_gated_completion(self):
        observed = self.dispatch("query", query_payload("ui.elements"))
        target_ref = observed["result"]["records"][0]["ref"]
        acted = self.dispatch("act", {
            "target": {"ref": target_ref},
            "action": "invoke",
            "arguments": {},
            "preconditions": [],
            "postconditions": [],
            "confirm": False,
        })
        self.assertEqual(acted["status"], "ok")
        self.assertEqual(acted["result"]["execution_path"], "accessibility")
        self.assertEqual(acted["result"]["status"], "applied")

        assertion = {
            "claim_id": "selected",
            "query": query_payload("ui.elements"),
            "assert": {"op": "eq", "field": "states.selected", "value": True},
        }
        verified = self.dispatch("verify", {
            "mode": "all", "assertions": [assertion], "freshness": "live",
        })
        self.assertEqual(verified["result"]["verdict"], "pass")
        verification_id = verified["result"]["verification_id"]
        completed = self.runtime.complete({
            "summary": "saved",
            "infeasible": False,
            "claims": [{"claim": "selected", "verification_id": verification_id}],
            "evidence_ids": [],
        })
        self.assertTrue(completed.accepted)

        fabricated = self.runtime.complete({
            "summary": "not actually verified",
            "infeasible": False,
            "claims": [{"claim": "invented", "verification_id": "ver_missing"}],
            "evidence_ids": [],
        })
        self.assertFalse(fabricated.accepted)
        self.assertEqual(fabricated.error["code"], "precondition_failed")

        run = self.dispatch("run", {
            "code": "rows = computer.query(" + repr(query_payload("ui.elements")) + ")\nemit(len(rows['records']))",
        })
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["result"]["output"], [1])
        self.assertGreaterEqual(run["result"]["operation_count"], 1)

        keyword_run = self.dispatch("run", {
            "code": (
                "rows = computer.query(resource='ui.elements', limit=30)\n"
                "emit(len(rows['records']))"
            ),
        })
        self.assertEqual(keyword_run["status"], "ok")
        self.assertEqual(keyword_run["result"]["output"], [1])

        default_act_run = self.dispatch("run", {
            "code": (
                "result = computer.act(target={'resource': 'ui.elements', "
                "'scope': {}, 'where': {'op': 'eq', 'field': 'name', "
                "'value': 'Save'}}, action='invoke')\nemit(result['status'])"
            ),
        })
        self.assertEqual(default_act_run["status"], "ok", default_act_run)
        self.assertEqual(default_act_run["result"]["output"], ["applied"])

        mixed_run = self.dispatch("run", {
            "code": "computer.query({'resource': 'ui.elements'}, resource='ui.elements')",
        })
        self.assertEqual(mixed_run["status"], "partial")
        self.assertEqual(
            mixed_run["result"]["failed_operation"]["error"]["code"],
            "invalid_request",
        )
        self.assertIn(
            "cannot mix",
            mixed_run["result"]["failed_operation"]["error"]["message"],
        )

    def test_selector_ambiguity_and_episode_isolation_fail_closed(self):
        wrong = self.runtime.dispatch({
            "protocol_version": "1.0", "request_id": "wrong",
            "episode_id": "another", "operation": "query",
            "payload": query_payload("ui.elements"),
        })
        self.assertEqual(wrong["status"], "rejected")
        self.assertEqual(wrong["error"]["code"], "permission_denied")

    def test_action_reobserves_the_private_originating_scope(self):
        guest = FakeScopedGuest()
        runtime = SemanticRuntime(
            episode_id="scoped",
            max_tool_calls=20,
            guest_request=guest.request,
            guest_capabilities=[{
                "adapter_id": "scoped.filesystem@1",
                "resources": ["filesystem.file"],
                "actions": ["mutate"],
                "execution_paths": ["native_api"],
            }],
        )
        query = query_payload("filesystem.file")
        query["scope"] = {"path": "/home/user/document.txt"}
        observed = runtime.dispatch({
            "protocol_version": "1.0", "request_id": "query",
            "episode_id": "scoped", "operation": "query", "payload": query,
        })
        target_ref = observed["result"]["records"][0]["ref"]
        acted = runtime.dispatch({
            "protocol_version": "1.0", "request_id": "act",
            "episode_id": "scoped", "operation": "act",
            "payload": {
                "target": {"ref": target_ref}, "action": "mutate",
                "arguments": {}, "preconditions": [], "postconditions": [],
                "confirm": False,
            },
        })
        self.assertEqual(acted["status"], "ok")
        self.assertGreaterEqual(len(guest.query_scopes), 3)
        self.assertTrue(all(
            scope.get("path") == "/home/user/document.txt"
            for scope in guest.query_scopes
        ))

    def test_action_rebinds_one_unique_semantic_identity_when_native_proxy_changes(self):
        guest = FakeVolatileNativeRefGuest()
        runtime = SemanticRuntime(
            episode_id="volatile", max_tool_calls=20,
            guest_request=guest.request,
            guest_capabilities=[{
                "adapter_id": "universal-atspi@1", "resources": ["os.file_choosers"],
                "actions": ["dismiss"], "execution_paths": ["accessibility"],
            }],
        )
        observed = runtime.dispatch({
            "protocol_version": "1.0", "request_id": "query",
            "episode_id": "volatile", "operation": "query",
            "payload": query_payload("os.file_choosers"),
        })
        target_ref = observed["result"]["records"][0]["ref"]
        acted = runtime.dispatch({
            "protocol_version": "1.0", "request_id": "act",
            "episode_id": "volatile", "operation": "act",
            "payload": {
                "target": {"ref": target_ref}, "action": "dismiss", "arguments": {},
                "preconditions": [], "postconditions": [], "confirm": False,
            },
        })
        self.assertEqual(acted["status"], "ok")
        self.assertTrue(guest.acted)


if __name__ == "__main__":
    unittest.main()
