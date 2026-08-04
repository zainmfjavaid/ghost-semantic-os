from __future__ import annotations

import unittest
import uuid

from envserver.semantic.adapters import (
    AdapterActionResult,
    AdapterContext,
    AdapterObservation,
    AdapterRegistry,
    SemanticAdapter,
)
from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.runtime import GuestProxyAdapter, SemanticRuntime


def query_payload(resource: str, *, parameters=None):
    return {
        "resource": resource,
        "scope": {},
        "where": {},
        "fields": [],
        "order_by": [],
        "parameters": parameters or {},
        "limit": 30,
        "freshness": "live",
    }


class ContractAdapter(SemanticAdapter):
    adapter_id = "contract.adapter@3"
    application = "contract-fixture"
    supported_versions = ("1.x", "2.x")
    resources = frozenset({"contract.records"})
    capabilities = frozenset({"persist", "touch"})
    execution_paths = ("app_bridge",)
    resource_schemas = {
        "contract.records": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        }
    }
    action_schemas = {
        "persist": {
            "arguments_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
            "risk": "persistent",
            "idempotent": True,
            "execution_paths": ["app_bridge"],
        },
        "touch": {
            "arguments_schema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            "risk": "reversible",
            "idempotent": False,
            "execution_paths": ["app_bridge"],
        },
    }

    def __init__(self) -> None:
        self.closed = False

    def observe(self, context, payload):
        return AdapterObservation(
            items=({"id": "one", "name": "One"},), native_revision="native-1"
        )

    def act(self, context, payload):
        return AdapterActionResult(
            changed=True, result={"execution_path": "app_bridge"}
        )

    def revision(self, surface=None):
        return f"revision:{surface or 'all'}"

    def resolve_ref(self, ref):
        return {"native": ref}

    def close(self):
        self.closed = True


class BrokenProbeAdapter(ContractAdapter):
    adapter_id = "broken.adapter@1"
    resources = frozenset({"broken.records"})

    def probe(self):
        raise TimeoutError("probe timed out")


class FailingActionAdapter(SemanticAdapter):
    adapter_id = "failure.adapter@1"
    resources = frozenset({"failure.records"})
    capabilities = frozenset({"mutate"})

    def observe(self, context, payload):
        return AdapterObservation(
            items=({"id": "record-1", "name": "One"},), native_revision="stable"
        )

    def act(self, context, payload):
        raise TimeoutError("lost response after send")


class QueryFailureAdapter(FailingActionAdapter):
    adapter_id = "query-failure.adapter@1"
    resources = frozenset({"query_failure.records"})

    def observe(self, context, payload):
        raise TimeoutError("read timed out")


class LargeResultAdapter(SemanticAdapter):
    adapter_id = "large.adapter@1"
    resources = frozenset({"large.records"})
    capabilities = frozenset()

    def observe(self, context, payload):
        return AdapterObservation(
            items=({"id": "large-1", "name": "Large", "body": "x" * 9_000},),
            native_revision="large-stable",
        )

    def act(self, context, payload):
        raise ProtocolError(ErrorCode.UNSUPPORTED, "read only")


class AppliedFailureAdapter(FailingActionAdapter):
    adapter_id = "applied-failure.adapter@1"
    resources = frozenset({"applied_failure.records"})

    def act(self, context, payload):
        return AdapterActionResult(changed=True, status="failed")


class AdapterContractTests(unittest.TestCase):
    def test_full_lifecycle_and_descriptor_metadata(self):
        adapter = ContractAdapter()
        registry = AdapterRegistry()
        registry.register(adapter)

        # The immutable action set remains useful for membership and is also
        # the callable capabilities() lifecycle surface.
        self.assertIn("persist", adapter.capabilities)
        self.assertEqual(adapter.capabilities(), ("persist", "touch"))
        self.assertEqual(adapter.revision("surface-a"), "revision:surface-a")
        self.assertEqual(adapter.resolve_ref("opaque"), {"native": "opaque"})

        descriptor = adapter.descriptor()
        self.assertEqual(descriptor["semantic_version"], "3")
        self.assertEqual(descriptor["application"], "contract-fixture")
        self.assertEqual(descriptor["supported_versions"], ["1.x", "2.x"])
        action = descriptor["action_schemas"]["persist"]
        self.assertEqual(action["risk"], "persistent")
        self.assertTrue(action["idempotent"])
        self.assertEqual(action["execution_paths"], ["app_bridge"])

        adapter.validate_parameters("contract.records", {"name": "One"})
        adapter.validate_arguments("persist", {"value": "saved"})
        with self.assertRaises(ProtocolError) as unknown_parameter:
            adapter.validate_parameters("contract.records", {"unknown": True})
        self.assertEqual(unknown_parameter.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(ProtocolError) as missing_argument:
            adapter.validate_arguments("persist", {})
        self.assertEqual(missing_argument.exception.code, ErrorCode.INVALID_REQUEST)

        health = registry.health()[0]
        self.assertEqual(health["status"], "healthy")
        registry.close()
        self.assertTrue(adapter.closed)

    def test_probe_failure_is_bounded_unavailable_health(self):
        registry = AdapterRegistry()
        registry.register(BrokenProbeAdapter())
        health = registry.health()[0]
        self.assertEqual(health["adapter_id"], "broken.adapter@1")
        self.assertEqual(health["status"], "unavailable")
        self.assertNotIn("Traceback", health["error"])

    def test_guest_target_behavior_is_descriptor_controlled(self):
        calls = []

        def request(method, path, payload):
            calls.append(dict(payload))
            return {"ok": True, "result": {"execution_path": "native_api"}}

        base = {
            "adapter_id": "guest.contract@1",
            "resources": ["guest.records"],
            "actions": ["mutate"],
            "execution_paths": ["native_api"],
        }
        context = AdapterContext("episode", "guest.records", "request", "rev")
        payload = {"target": {"ref": "native-ref"}, "action": "mutate", "arguments": {}}

        retaining = GuestProxyAdapter({**base, "accepts_entity_target": True}, request)
        retaining.act(context, payload)
        self.assertEqual(calls[-1]["target"], {"ref": "native-ref"})

        stripping = GuestProxyAdapter(
            {**base, "adapter_id": "guest.targetless@1", "accepts_entity_target": False},
            request,
        )
        stripping.act(context, payload)
        self.assertNotIn("target", calls[-1])

    def test_guest_private_pages_form_one_stable_kernel_snapshot(self):
        requested_offsets = []

        def request(method, path, payload):
            offset = payload["internal_offset"]
            requested_offsets.append(offset)
            pages = {
                0: ([{"ref": "native-1", "name": "One"}], 1),
                1: ([{"ref": "native-2", "name": "Two"}], None),
            }
            records, next_offset = pages[offset]
            return {"ok": True, "result": {
                "records": records,
                "revision": "stable-revision",
                "total": 2,
                "truncated": next_offset is not None,
                "next_internal_offset": next_offset,
            }}

        adapter = GuestProxyAdapter({
            "adapter_id": "guest.paged@1",
            "resources": ["guest.records"],
            "actions": [],
            "execution_paths": ["native_api"],
        }, request)
        observation = adapter.observe(
            AdapterContext("episode", "guest.records", "request", None),
            query_payload("guest.records"),
        )
        self.assertEqual(requested_offsets, [0, 1])
        self.assertEqual([item["name"] for item in observation.items], ["One", "Two"])
        self.assertEqual(observation.native_revision, "stable-revision")
        self.assertEqual(observation.summary["guest_pages"], 2)
        self.assertFalse(observation.summary["guest_truncated"])

    def test_guest_private_page_revision_change_fails_closed(self):
        def request(method, path, payload):
            offset = payload["internal_offset"]
            return {"ok": True, "result": {
                "records": [{"ref": f"native-{offset}"}],
                "revision": f"revision-{offset}",
                "truncated": offset == 0,
                "next_internal_offset": 1 if offset == 0 else None,
            }}

        adapter = GuestProxyAdapter({
            "adapter_id": "guest.changing@1",
            "resources": ["guest.records"],
            "actions": [],
            "execution_paths": ["native_api"],
        }, request)
        with self.assertRaises(ProtocolError) as changed:
            adapter.observe(
                AdapterContext("episode", "guest.records", "request", None),
                query_payload("guest.records"),
            )
        self.assertEqual(changed.exception.code, ErrorCode.REVISION_CONFLICT)

    def _runtime(self, adapter):
        return SemanticRuntime(
            episode_id="failure-episode",
            max_tool_calls=20,
            guest_request=lambda *_: (_ for _ in ()).throw(AssertionError("no guest")),
            guest_capabilities=[],
            adapters=[adapter],
        )

    @staticmethod
    def _dispatch(runtime, operation, payload):
        return runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": "failure-episode",
            "operation": operation,
            "payload": payload,
        })

    def test_action_transport_failure_is_uncertain_and_receipted(self):
        runtime = self._runtime(FailingActionAdapter())
        observed = self._dispatch(runtime, "query", query_payload("failure.records"))
        ref = observed["result"]["records"][0]["ref"]
        failed = self._dispatch(runtime, "act", {
            "target": {"ref": ref},
            "action": "mutate",
            "arguments": {},
            "preconditions": [],
            "postconditions": [],
            "confirm": False,
        })
        self.assertEqual(failed["status"], "uncertain")
        self.assertEqual(failed["error"]["code"], "uncertain")
        self.assertEqual(failed["error"]["side_effect_state"], "unknown")
        receipt_candidates = [
            value for value in failed["error"]["candidates"] if "receipt_id" in value
        ]
        self.assertEqual(len(receipt_candidates), 1)
        self.assertEqual(runtime.state.receipt_counts()["uncertain_actions"], 1)
        completion = runtime.complete({
            "summary": "not safe", "infeasible": False, "claims": [], "evidence_ids": [],
        })
        self.assertEqual(completion.error["code"], "uncertain")

        action_receipt_id = receipt_candidates[0]["receipt_id"]
        reconciled = self._dispatch(runtime, "verify", {
            "mode": "all",
            "assertions": [{
                "claim_id": "record-still-present",
                "query": query_payload("failure.records"),
                "assert": {"op": "exists"},
            }],
            "freshness": "live",
            "reconcile_action": {
                "receipt_id": action_receipt_id,
                "outcome": "none",
            },
        })
        self.assertEqual(reconciled["status"], "ok")
        self.assertEqual(
            reconciled["result"]["reconciliation"]["action_receipt_id"],
            action_receipt_id,
        )
        self.assertEqual(runtime.state.receipt_counts()["uncertain_actions"], 0)
        pending = self._dispatch(
            runtime, "query", query_payload("system.pending_state")
        )
        pending_record = pending["result"]["records"][0]
        self.assertEqual(pending_record["uncertain_actions"], [])
        self.assertEqual(
            pending_record["action_reconciliations"][0]["action_receipt_id"],
            action_receipt_id,
        )
        verified_completion = runtime.complete({
            "summary": "uncertainty resolved from current state",
            "infeasible": False,
            "claims": [{
                "claim": "record remains present",
                "verification_id": reconciled["result"]["verification_id"],
            }],
            "evidence_ids": [],
        })
        self.assertTrue(verified_completion.accepted)

    def test_applied_adapter_failure_has_an_applied_receipt(self):
        runtime = self._runtime(AppliedFailureAdapter())
        observed = self._dispatch(
            runtime, "query", query_payload("applied_failure.records")
        )
        ref = observed["result"]["records"][0]["ref"]
        failed = self._dispatch(runtime, "act", {
            "target": {"ref": ref},
            "action": "mutate",
            "arguments": {},
            "preconditions": [],
            "postconditions": [],
            "confirm": False,
        })
        self.assertEqual(failed["error"]["code"], "postcondition_failed")
        self.assertEqual(failed["error"]["side_effect_state"], "applied")
        receipts = [
            value for value in failed["error"]["candidates"] if "receipt_id" in value
        ]
        self.assertEqual(len(receipts), 1)
        receipt = runtime.state.get_action(receipts[0]["receipt_id"])
        self.assertTrue(receipt.changed)
        self.assertEqual(receipt.side_effect_state.value, "applied")

    def test_query_transport_failure_has_no_side_effects(self):
        runtime = self._runtime(QueryFailureAdapter())
        failed = self._dispatch(
            runtime, "query", query_payload("query_failure.records")
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "timeout")
        self.assertEqual(failed["error"]["side_effect_state"], "none")
        self.assertEqual(runtime.state.receipt_counts()["uncertain_actions"], 0)

    def test_large_result_becomes_revision_scoped_paginated_handle(self):
        runtime = self._runtime(LargeResultAdapter())
        result = self._dispatch(runtime, "query", query_payload("large.records"))
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["result"]["truncated"])
        handle = result["result"]["data_handle"]
        self.assertTrue(handle.startswith("data_"))
        self.assertNotIn("x" * 2_001, str(result))
        self.assertEqual(runtime.state.receipt_counts()["data_handles"], 1)

        chunks = self._dispatch(runtime, "query", {
            **query_payload("system.data_handle"),
            "scope": {"ref": handle},
            "limit": 100,
        })
        self.assertEqual(chunks["status"], "ok")
        self.assertLessEqual(len(chunks["result"]["records"]), 12)
        self.assertTrue(chunks["result"]["truncated"])
        self.assertIsNotNone(chunks["result"]["next_cursor"])
        self.assertEqual(chunks["result"]["records"][0]["kind"], "data_handle.chunk")

        runtime.state.advance_revision("large.adapter@1", "large.records")
        stale = self._dispatch(runtime, "query", {
            **query_payload("system.data_handle"),
            "scope": {"ref": handle},
        })
        self.assertEqual(stale["error"]["code"], "revision_conflict")


if __name__ == "__main__":
    unittest.main()
