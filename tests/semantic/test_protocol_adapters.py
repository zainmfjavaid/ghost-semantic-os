from __future__ import annotations

import json
import unittest

from envserver.semantic.adapters import (
    AdapterActionResult,
    AdapterContext,
    AdapterObservation,
    AdapterRegistry,
    SemanticAdapter,
)
from envserver.semantic.protocol import (
    ErrorCode,
    ProtocolError,
    RequestEnvelope,
    SideEffectState,
    Status,
    error_response,
    ok_response,
)


class FakeAdapter(SemanticAdapter):
    adapter_id = "fake.adapter@1"
    resources = frozenset({"desktop.window"})
    capabilities = frozenset({"observe", "activate"})

    def observe(self, context: AdapterContext, payload: dict) -> AdapterObservation:
        return AdapterObservation(items=({"name": "One"},))

    def act(self, context: AdapterContext, payload: dict) -> AdapterActionResult:
        return AdapterActionResult(changed=True, result={"done": True})


class ProtocolTests(unittest.TestCase):
    def test_request_contract(self) -> None:
        request = RequestEnvelope.parse(
            {
                "protocol_version": "1.0",
                "request_id": "request-1",
                "episode_id": "episode-1",
                "operation": "query",
                "payload": {"resource": "desktop.window"},
            }
        )
        self.assertEqual(request.operation, "query")
        with self.assertRaises(ProtocolError) as caught:
            RequestEnvelope.parse(
                {
                    "protocol_version": "2.0",
                    "request_id": "r",
                    "episode_id": "e",
                    "operation": "query",
                    "payload": {},
                }
            )
        self.assertEqual(caught.exception.code, ErrorCode.UNSUPPORTED)

    def test_exact_response_and_error_shapes(self) -> None:
        response = ok_response(
            request_id="r1",
            adapter_id="fake.adapter@1",
            before_revision="before",
            after_revision="after",
            result={"items": []},
        ).to_dict()
        self.assertEqual(
            set(response),
            {
                "protocol_version",
                "request_id",
                "status",
                "adapter_id",
                "observed_at",
                "before_revision",
                "after_revision",
                "result",
                "provenance",
                "error",
            },
        )
        self.assertEqual(response["status"], "ok")
        self.assertIsNone(response["error"])

        error = ProtocolError(
            ErrorCode.AMBIGUOUS,
            "two targets",
            retryable=True,
            side_effect_state=SideEffectState.NONE,
            candidates=({"ref": "opaque-a"}, {"ref": "opaque-b"}),
        )
        failed = error_response(
            request_id="r2", adapter_id="fake.adapter@1", error=error
        ).to_dict()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            set(failed["error"]),
            {
                "code",
                "message",
                "retryable",
                "side_effect_state",
                "missing_capability",
                "candidates",
                "recovery",
            },
        )
        self.assertEqual(failed["error"]["code"], "ambiguous")
        self.assertEqual(failed["error"]["side_effect_state"], "none")

    def test_response_bounds(self) -> None:
        response = ok_response(
            request_id="r",
            adapter_id="a",
            before_revision=None,
            after_revision=None,
            result={"huge": "x" * 20_000, "many": ["y" * 500] * 100},
            status=Status.OK,
        ).to_dict()
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        self.assertLessEqual(len(encoded), 12_000)
        self.assertIn(response["status"], {"ok", "partial"})


class AdapterRegistryTests(unittest.TestCase):
    def test_registration_resolution_and_ambiguity(self) -> None:
        registry = AdapterRegistry()
        adapter = registry.register(FakeAdapter())
        self.assertIs(registry.resolve("desktop.window"), adapter)
        self.assertIs(
            registry.resolve("desktop.window", required_capability="activate"), adapter
        )
        self.assertEqual(registry.describe()[0]["adapter_id"], "fake.adapter@1")
        with self.assertRaises(ProtocolError) as caught:
            registry.register(FakeAdapter())
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_REQUEST)

        class Second(FakeAdapter):
            adapter_id = "second.adapter@2"

        registry.register(Second())
        with self.assertRaises(ProtocolError) as ambiguous:
            registry.resolve("desktop.window")
        self.assertEqual(ambiguous.exception.code, ErrorCode.AMBIGUOUS)
        self.assertEqual(len(ambiguous.exception.candidates), 2)

    def test_unknown_resource_and_missing_capability(self) -> None:
        registry = AdapterRegistry()
        registry.register(FakeAdapter())
        with self.assertRaises(ProtocolError) as missing:
            registry.resolve("browser.tab")
        self.assertEqual(missing.exception.code, ErrorCode.UNKNOWN_RESOURCE)
        with self.assertRaises(ProtocolError) as unsupported:
            registry.resolve("desktop.window", required_capability="delete")
        self.assertEqual(unsupported.exception.code, ErrorCode.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
