from __future__ import annotations

import unittest

from envserver.semantic.protocol import ErrorCode, ProtocolError
from envserver.semantic.query import QueryEngine
from envserver.semantic.state import EpisodeState
from envserver.semantic.verify import VerificationEngine


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = EpisodeState("episode", max_tool_calls=20)
        self.observation, refs = self.state.record_observation(
            adapter_id="desktop.adapter@1",
            resource="controls",
            entries=[
                {"locator": {"path": 0}, "fingerprint": {"name": "Save"}},
                {"locator": {"path": 1}, "fingerprint": {"name": "Cancel"}},
            ],
        )
        self.rows = [
            {"ref": refs[0].ref, "name": "Save", "role": "button", "enabled": True},
            {"ref": refs[1].ref, "name": "Cancel", "role": "button", "enabled": True},
        ]
        self.query_engine = QueryEngine()
        self.engine = VerificationEngine()

    @staticmethod
    def query_payload(where=None, fields=None, limit=30):
        return {
            "resource": "controls",
            "scope": {},
            "where": where or {},
            "fields": fields or [],
            "order_by": [],
            "parameters": {},
            "limit": limit,
            "freshness": "live",
        }

    def resolver(self, payload):
        return self.query_engine.query(
            state=self.state,
            adapter_id="desktop.adapter@1",
            resource="controls",
            items=self.rows,
            payload=payload,
            consume_budget=False,
        )

    def test_canonical_claims_nested_logic_and_receipt(self) -> None:
        payload = {
            "mode": "all",
            "freshness": "live",
            "assertions": [
                {
                    "claim_id": "save-exists",
                    "query": self.query_payload(
                        {"op": "eq", "field": "name", "value": "Save"}
                    ),
                    "assert": {"op": "exists"},
                },
                {
                    "any": [
                        {
                            "claim_id": "wrong-count",
                            "query": self.query_payload(),
                            "assert": {"op": "count", "value": 99},
                        },
                        {
                            "not": {
                                "claim_id": "missing-delete",
                                "query": self.query_payload(
                                    {"op": "eq", "field": "name", "value": "Delete"}
                                ),
                                "assert": {"op": "exists"},
                            }
                        },
                    ]
                },
                {
                    "claim_id": "save-name",
                    "query": self.query_payload(
                        {"op": "eq", "field": "name", "value": "Save"},
                        fields=["name"],
                    ),
                    "assert": {"op": "eq", "field": "name", "value": "Save"},
                },
            ],
        }
        result = self.engine.verify(
            state=self.state,
            adapter_id="desktop.adapter@1",
            payload=payload,
            query=self.resolver,
        )
        self.assertEqual(result.verdict, "pass")
        self.assertTrue(result.passed)
        self.assertTrue(result.verification_id.startswith("ver_"))
        self.assertEqual(len(result.claims), 4)
        self.assertEqual({claim["verdict"] for claim in result.claims}, {"pass", "fail"})
        self.assertEqual(
            set(result.to_dict()),
            {"verification_id", "verdict", "claims", "dependencies", "evidence", "observed_at"},
        )
        self.assertEqual(result.dependencies[0]["revision"], self.observation.revision)

    def test_assertion_operators_count_approx_matches_parseable(self) -> None:
        self.rows = [
            {"name": "version-123", "score": 9.95, "json": '{"ok": true}'},
        ]
        assertions = [
            ("count", None, 1, None),
            ("approx", "score", 10, 0.1),
            ("matches", "name", r"^version-[0-9]+$", None),
            ("parseable", "json", "json", None),
        ]
        leaves = []
        for index, (operation, field, value, tolerance) in enumerate(assertions):
            assertion = {"op": operation, "value": value}
            if field is not None:
                assertion["field"] = field
            if tolerance is not None:
                assertion["tolerance"] = tolerance
            leaves.append(
                {
                    "claim_id": f"claim-{index}",
                    "query": self.query_payload(),
                    "assert": assertion,
                }
            )
        result = self.engine.verify(
            state=self.state,
            adapter_id="desktop.adapter@1",
            payload={"mode": "all", "assertions": leaves, "freshness": "live"},
            query=self.resolver,
        )
        self.assertEqual(result.verdict, "pass")

    def test_operational_query_gap_is_unknown(self) -> None:
        def unavailable(payload):
            raise ProtocolError(ErrorCode.REPRESENTATION_GAP, "not represented")

        result = self.engine.verify(
            state=self.state,
            adapter_id="desktop.adapter@1",
            payload={
                "mode": "all",
                "freshness": "live",
                "assertions": [
                    {
                        "claim_id": "unknown",
                        "query": self.query_payload(),
                        "assert": {"op": "exists"},
                    }
                ],
            },
            query=unavailable,
        )
        self.assertEqual(result.verdict, "unknown")
        self.assertEqual(result.claims[0]["verdict"], "unknown")
        self.assertEqual(result.evidence[0]["error"]["code"], "representation_gap")

    def test_passing_live_verification_immutably_reconciles_exact_uncertain_action(self) -> None:
        action = self.state.record_action(
            adapter_id="desktop.adapter@1",
            resource="controls",
            action="invoke",
            target_ref=self.rows[0]["ref"],
            expected_revision=self.observation.revision,
            changed=None,
            side_effect_state="unknown",
            result={"error": {"code": "uncertain"}},
        )
        result = self.engine.verify(
            state=self.state,
            adapter_id="desktop.adapter@1",
            payload={
                "mode": "all",
                "freshness": "live",
                "assertions": [{
                    "claim_id": "save-remains-present",
                    "query": self.query_payload(
                        {"op": "eq", "field": "name", "value": "Save"}
                    ),
                    "assert": {"op": "exists"},
                }],
                "reconcile_action": {
                    "receipt_id": action.receipt_id,
                    "outcome": "none",
                },
            },
            query=self.resolver,
        )
        self.assertEqual(result.verdict, "pass")
        self.assertIsNotNone(result.reconciliation)
        assert result.reconciliation is not None
        self.assertEqual(result.reconciliation.action_receipt_id, action.receipt_id)
        self.assertEqual(result.reconciliation.verification_id, result.verification_id)
        self.assertEqual(len(result.reconciliation.verification_fingerprint), 64)
        self.assertEqual(result.reconciliation.outcome.value, "none")
        self.assertEqual(
            self.state.get_action(action.receipt_id).side_effect_state.value,
            "unknown",
            "the original receipt remains immutable",
        )
        self.assertEqual(self.state.uncertain_actions(), [])
        self.assertEqual(len(self.state.action_reconciliations()), 1)

        with self.assertRaises(ProtocolError) as duplicate:
            self.state.reconcile_action(
                action_receipt_id=action.receipt_id,
                verification_id=result.verification_id,
                outcome="none",
            )
        self.assertEqual(duplicate.exception.code, ErrorCode.ARTIFACT_CONFLICT)

    def test_failed_verification_does_not_reconcile_uncertain_action(self) -> None:
        action = self.state.record_action(
            adapter_id="desktop.adapter@1",
            resource="controls",
            action="invoke",
            target_ref=self.rows[0]["ref"],
            expected_revision=self.observation.revision,
            changed=None,
            side_effect_state="unknown",
        )
        result = self.engine.verify(
            state=self.state,
            adapter_id="desktop.adapter@1",
            payload={
                "mode": "all",
                "freshness": "live",
                "assertions": [{
                    "claim_id": "impossible-count",
                    "query": self.query_payload(),
                    "assert": {"op": "count", "value": 99},
                }],
                "reconcile_action": {
                    "receipt_id": action.receipt_id,
                    "outcome": "applied",
                },
            },
            query=self.resolver,
        )
        self.assertEqual(result.verdict, "fail")
        self.assertIsNone(result.reconciliation)
        self.assertEqual(
            [receipt.receipt_id for receipt in self.state.uncertain_actions()],
            [action.receipt_id],
        )

    def test_scoped_read_revisions_do_not_stale_multi_assertion_receipt(self) -> None:
        """Range/path-specific view hashes are not mutation revisions."""

        ranges = {
            "A1:A1": [{"value": "alpha"}],
            "B1:B1": [{"value": "beta"}],
        }

        def scoped_resolver(payload):
            requested = payload["parameters"]["range"]
            records = ranges[requested]
            observation, refs = self.state.record_observation(
                adapter_id="desktop.adapter@1",
                resource="controls",
                entries=[{
                    "locator": {"range": requested},
                    "fingerprint": {"value": records[0]["value"]},
                }],
                # Reproduces the old UNO contract: each queried range has a
                # different record-derived native/query revision.
                native_revision=f"native-view-{requested}",
            )
            items = [{**records[0], "ref": refs[0].ref}]
            return self.query_engine.query(
                state=self.state,
                adapter_id="desktop.adapter@1",
                resource="controls",
                items=items,
                payload=payload,
                consume_budget=False,
            )

        assertions = []
        for index, (requested, expected) in enumerate(
            (("A1:A1", "alpha"), ("B1:B1", "beta"))
        ):
            payload = self.query_payload()
            payload["parameters"] = {"range": requested}
            assertions.append({
                "claim_id": f"range-{index}",
                "query": payload,
                "assert": {"op": "eq", "field": "value", "value": expected},
            })

        result = self.engine.verify(
            state=self.state,
            adapter_id="desktop.adapter@1",
            payload={"mode": "all", "assertions": assertions, "freshness": "live"},
            query=scoped_resolver,
        )

        self.assertEqual(result.verdict, "pass")
        self.assertTrue(self.state.verification_is_current(result.verification_id))
        self.assertEqual(
            len(result.receipt.evidence["internal_dependencies"]), 1
        )

        # Another differently scoped read remains read-only and cannot stale
        # the receipt, even though it advances the transient query revision.
        scoped_resolver(assertions[0]["query"])
        self.assertTrue(self.state.verification_is_current(result.verification_id))

        # A real mutation of the dependency does invalidate it.
        self.state.record_action(
            adapter_id="desktop.adapter@1",
            resource="controls",
            action="set_value",
            target_ref=None,
            expected_revision=self.state.current_revision(
                "desktop.adapter@1", "controls"
            ),
            changed=True,
            side_effect_state="applied",
        )
        self.assertFalse(self.state.verification_is_current(result.verification_id))

    def test_duplicate_claim_and_invented_shape_rejected(self) -> None:
        leaf = {
            "claim_id": "same",
            "query": self.query_payload(),
            "assert": {"op": "exists"},
        }
        with self.assertRaises(ProtocolError) as duplicate:
            self.engine.verify(
                state=self.state,
                adapter_id="desktop.adapter@1",
                payload={
                    "mode": "all",
                    "freshness": "live",
                    "assertions": [leaf, leaf],
                },
                query=self.resolver,
            )
        self.assertEqual(duplicate.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(ProtocolError) as old_shape:
            self.engine.verify(
                state=self.state,
                adapter_id="desktop.adapter@1",
                payload={
                    "mode": "all",
                    "freshness": "live",
                    "assertions": [{"kind": "exists", "where": []}],
                },
                query=self.resolver,
            )
        self.assertEqual(old_shape.exception.code, ErrorCode.INVALID_REQUEST)


if __name__ == "__main__":
    unittest.main()
