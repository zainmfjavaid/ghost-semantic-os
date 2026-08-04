from __future__ import annotations

import unittest

from envserver.semantic.protocol import ErrorCode, ProtocolError, SideEffectState
from envserver.semantic.query import QueryEngine
from envserver.semantic.state import EpisodeState, StateLimits, canonical_fingerprint


def observed(state: EpisodeState):
    rows = [
        {"name": "Alpha", "role": "button", "rank": 3, "state": {"enabled": True}},
        {"name": "Beta", "role": "link", "rank": 1, "state": {"enabled": False}},
        {"name": "Gamma", "role": "button", "rank": 2, "state": {"enabled": True}},
    ]
    receipt, refs = state.record_observation(
        adapter_id="desktop",
        resource="controls",
        entries=[
            {"locator": {"path": index}, "fingerprint": {"name": row["name"]}}
            for index, row in enumerate(rows)
        ],
        summary={"count": 3},
    )
    return receipt, refs, [dict(row, ref=ref.ref) for row, ref in zip(rows, refs)]


def query_payload(**overrides):
    payload = {
        "resource": "controls",
        "scope": {},
        "where": {},
        "fields": [],
        "order_by": [],
        "parameters": {},
        "limit": 30,
        "freshness": "live",
    }
    payload.update(overrides)
    return payload


class StateTests(unittest.TestCase):
    def test_refs_revisions_and_receipts(self) -> None:
        state = EpisodeState("episode", max_tool_calls=4)
        receipt, refs, _ = observed(state)
        self.assertTrue(receipt.revision.startswith("rev_"))
        resolved = state.resolve_ref(refs[0].ref, adapter_id="desktop", resource="controls")
        self.assertEqual(resolved.locator, {"path": 0})
        self.assertNotIn("locator", resolved.public())

        state.advance_revision("desktop", "controls")
        with self.assertRaises(ProtocolError) as stale:
            state.resolve_ref(refs[0].ref)
        self.assertEqual(stale.exception.code, ErrorCode.STALE_REF)
        self.assertTrue(stale.exception.retryable)
        self.assertEqual(
            stale.exception.recovery.allowed_operations, ("computer.query",)
        )
        self.assertEqual(stale.exception.recovery.suggested_resource, "controls")

    def test_deleted_ref_keeps_a_stale_tombstone(self) -> None:
        state = EpisodeState("episode", max_tool_calls=4)
        _, refs, _ = observed(state)

        self.assertTrue(state.retire_ref(refs[0].ref, reason="deleted"))
        with self.assertRaises(ProtocolError) as deleted:
            state.resolve_ref(refs[0].ref)
        self.assertEqual(deleted.exception.code, ErrorCode.STALE_REF)
        self.assertEqual(
            deleted.exception.recovery.suggested_resource, "controls"
        )
        # Deletion never changes the identity of a surviving sibling.
        self.assertEqual(state.resolve_ref(refs[1].ref).ref, refs[1].ref)

    def test_native_revision_invalidation_tombstones_prior_refs(self) -> None:
        state = EpisodeState("episode", max_tool_calls=4)
        _, old_refs = state.record_observation(
            adapter_id="desktop",
            resource="controls",
            entries=[{"locator": {"native": "removed"}}],
            native_revision="native-v1",
        )
        _, new_refs = state.record_observation(
            adapter_id="desktop",
            resource="controls",
            entries=[{"locator": {"native": "replacement"}}],
            native_revision="native-v2",
        )

        with self.assertRaises(ProtocolError) as stale:
            state.resolve_ref(old_refs[0].ref)
        self.assertEqual(stale.exception.code, ErrorCode.STALE_REF)
        self.assertEqual(state.resolve_ref(new_refs[0].ref).ref, new_refs[0].ref)

    def test_ref_tombstones_are_bounded(self) -> None:
        state = EpisodeState(
            "episode",
            max_tool_calls=4,
            limits=StateLimits(max_ref_tombstones=2),
        )
        retired: list[str] = []
        for index in range(3):
            _, refs = state.record_observation(
                adapter_id="desktop",
                resource=f"controls.{index}",
                entries=[{"locator": {"path": index}}],
            )
            retired.append(refs[0].ref)
            state.advance_revision("desktop", f"controls.{index}")

        with self.assertRaises(ProtocolError) as evicted:
            state.resolve_ref(retired[0])
        self.assertEqual(evicted.exception.code, ErrorCode.NOT_FOUND)
        for ref in retired[1:]:
            with self.assertRaises(ProtocolError) as stale:
                state.resolve_ref(ref)
            self.assertEqual(stale.exception.code, ErrorCode.STALE_REF)

    def test_live_ref_capacity_evicts_oldest_instead_of_exhausting_episode(self) -> None:
        state = EpisodeState(
            "episode",
            max_tool_calls=4,
            limits=StateLimits(max_live_refs=2, max_ref_tombstones=4),
        )
        _, first = state.record_observation(
            adapter_id="desktop",
            resource="controls.first",
            entries=[{"locator": {"path": 1}}],
        )
        _, second = state.record_observation(
            adapter_id="desktop",
            resource="controls.second",
            entries=[{"locator": {"path": 2}}],
        )

        # A third legal observation must not turn the episode into a permanent
        # budget-exhausted state merely because two prior refs are still live.
        _, third = state.record_observation(
            adapter_id="desktop",
            resource="controls.third",
            entries=[{"locator": {"path": 3}}],
        )

        with self.assertRaises(ProtocolError) as stale:
            state.resolve_ref(first[0].ref)
        self.assertEqual(stale.exception.code, ErrorCode.STALE_REF)
        self.assertEqual(state.resolve_ref(second[0].ref).ref, second[0].ref)
        self.assertEqual(state.resolve_ref(third[0].ref).ref, third[0].ref)

        # Re-observing the evicted native identity issues a fresh capability;
        # the old opaque ID remains stale and is never silently rebound.
        _, reissued = state.record_observation(
            adapter_id="desktop",
            resource="controls.first",
            entries=[{"locator": {"path": 1}}],
        )
        self.assertNotEqual(reissued[0].ref, first[0].ref)
        with self.assertRaises(ProtocolError) as still_stale:
            state.resolve_ref(first[0].ref)
        self.assertEqual(still_stale.exception.code, ErrorCode.STALE_REF)
        self.assertEqual(state.resolve_ref(reissued[0].ref).ref, reissued[0].ref)

    def test_reobserved_ref_refreshes_recency_without_changing_identity(self) -> None:
        state = EpisodeState(
            "episode",
            max_tool_calls=4,
            limits=StateLimits(max_live_refs=2, max_ref_tombstones=4),
        )
        _, first = state.record_observation(
            adapter_id="desktop",
            resource="controls",
            entries=[{"locator": {"path": 1}}],
            native_revision="stable",
        )
        _, second = state.record_observation(
            adapter_id="desktop",
            resource="controls",
            entries=[{"locator": {"path": 2}}],
            native_revision="stable",
        )
        _, refreshed = state.record_observation(
            adapter_id="desktop",
            resource="controls",
            entries=[{"locator": {"path": 1}}],
            native_revision="stable",
        )
        self.assertEqual(refreshed[0].ref, first[0].ref)

        _, newest = state.record_observation(
            adapter_id="desktop",
            resource="controls.other",
            entries=[{"locator": {"path": 3}}],
        )

        with self.assertRaises(ProtocolError) as stale:
            state.resolve_ref(second[0].ref)
        self.assertEqual(stale.exception.code, ErrorCode.STALE_REF)
        self.assertEqual(state.resolve_ref(first[0].ref).ref, first[0].ref)
        self.assertEqual(state.resolve_ref(newest[0].ref).ref, newest[0].ref)

    def test_capacity_pruning_keeps_every_ref_from_current_full_observation(self) -> None:
        state = EpisodeState(
            "episode",
            max_tool_calls=4,
            limits=StateLimits(max_collection_items=3, max_live_refs=3),
        )
        _, old = state.record_observation(
            adapter_id="desktop",
            resource="controls",
            entries=[
                {"locator": {"path": 1}},
                {"locator": {"path": 2}},
                {"locator": {"path": 3}},
            ],
            native_revision="stable",
        )

        # Mix one existing identity into a capacity-sized observation.  The
        # result must not contain a ref that was evicted later in the same
        # observation.
        _, current = state.record_observation(
            adapter_id="desktop",
            resource="controls",
            entries=[
                {"locator": {"path": 2}},
                {"locator": {"path": 4}},
                {"locator": {"path": 5}},
            ],
            native_revision="stable",
        )
        self.assertEqual(current[0].ref, old[1].ref)
        for record in current:
            self.assertEqual(state.resolve_ref(record.ref).ref, record.ref)
        with self.assertRaises(ProtocolError) as stale:
            state.resolve_ref(old[0].ref)
        self.assertEqual(stale.exception.code, ErrorCode.STALE_REF)

    def test_truly_unknown_ref_remains_not_found(self) -> None:
        state = EpisodeState("episode", max_tool_calls=4)
        with self.assertRaises(ProtocolError) as missing:
            state.resolve_ref("ref_never-issued")
        self.assertEqual(missing.exception.code, ErrorCode.NOT_FOUND)

    def test_idempotency_replays_and_conflicts(self) -> None:
        state = EpisodeState("episode", max_tool_calls=4)
        receipt, refs, _ = observed(state)
        fingerprint = canonical_fingerprint({"action": "activate", "ref": refs[0].ref})
        action = state.record_action(
            adapter_id="desktop",
            resource="controls",
            action="activate",
            target_ref=refs[0].ref,
            expected_revision=receipt.revision,
            changed=True,
            side_effect_state=SideEffectState.APPLIED,
            result={"activated": True},
            idempotency_key="one-click",
            request_fingerprint=fingerprint,
        )
        replay = state.replay_action("one-click", fingerprint)
        self.assertEqual(replay.receipt_id, action.receipt_id)
        with self.assertRaises(ProtocolError) as conflict:
            state.replay_action("one-click", canonical_fingerprint({"different": True}))
        self.assertEqual(conflict.exception.code, ErrorCode.ARTIFACT_CONFLICT)

    def test_episode_operation_budget(self) -> None:
        state = EpisodeState("episode", max_tool_calls=1)
        state.consume_operation(10)
        with self.assertRaises(ProtocolError) as exhausted:
            state.consume_operation()
        self.assertEqual(exhausted.exception.code, ErrorCode.BUDGET_EXHAUSTED)


class QueryTests(unittest.TestCase):
    def test_filter_sort_project_paginate_and_cursor_revision(self) -> None:
        state = EpisodeState("episode", max_tool_calls=10)
        receipt, refs, rows = observed(state)
        engine = QueryEngine()
        payload = {
            **query_payload(),
            "where": {"op": "eq", "field": "role", "value": "button"},
            "order_by": [{"field": "rank", "direction": "asc"}],
            "fields": ["name", "rank"],
            "limit": 1,
        }
        first = engine.query(
            state=state,
            adapter_id="desktop",
            resource="controls",
            items=rows,
            payload=payload,
        )
        self.assertEqual(
            first.records,
            ({"ref": refs[2].ref, "name": "Gamma", "rank": 2},),
        )
        self.assertEqual(first.total, 2)
        self.assertIsNotNone(first.next_cursor)
        self.assertEqual(
            set(first.to_dict()), {"records", "next_cursor", "truncated", "total"}
        )
        second = engine.query(
            state=state,
            adapter_id="desktop",
            resource="controls",
            items=rows,
            payload=dict(payload, cursor=first.next_cursor),
        )
        self.assertEqual(
            second.records,
            ({"ref": refs[0].ref, "name": "Alpha", "rank": 3},),
        )
        self.assertIsNone(second.next_cursor)

        missing_fields = engine.query(
            state=state,
            adapter_id="desktop",
            resource="controls",
            items=rows,
            payload={**query_payload(), "fields": ["does_not_exist"]},
        )
        self.assertTrue(all(set(record) == {"ref"} for record in missing_fields.records))

        state.advance_revision("desktop", "controls")
        with self.assertRaises(ProtocolError) as stale:
            engine.query(
                state=state,
                adapter_id="desktop",
                resource="controls",
                items=rows,
                payload=dict(payload, cursor=first.next_cursor),
            )
        self.assertEqual(stale.exception.code, ErrorCode.REVISION_CONFLICT)
        self.assertNotEqual(receipt.revision, state.current_revision("desktop", "controls"))

    def test_regex_limits_and_two_order_fields(self) -> None:
        state = EpisodeState("episode", max_tool_calls=10)
        _, _, rows = observed(state)
        engine = QueryEngine()
        page = engine.query(
            state=state,
            adapter_id="desktop",
            resource="controls",
            items=rows,
            payload=query_payload(
                where={"field": "name", "op": "matches", "value": "^A"}
            ),
        )
        self.assertEqual(page.total, 1)
        with self.assertRaises(ProtocolError) as too_many:
            engine.query(
                state=state,
                adapter_id="desktop",
                resource="controls",
                items=rows,
                payload=query_payload(
                    order_by=[
                        {"field": "name", "direction": "asc"},
                        {"field": "rank", "direction": "asc"},
                        {"field": "role", "direction": "asc"},
                    ]
                ),
            )
        self.assertEqual(too_many.exception.code, ErrorCode.INVALID_REQUEST)
        with self.assertRaises(ProtocolError) as regex_long:
            engine.query(
                state=state,
                adapter_id="desktop",
                resource="controls",
                items=rows,
                payload=query_payload(
                    where={"field": "name", "op": "matches", "value": "a" * 257}
                ),
            )
        self.assertEqual(regex_long.exception.code, ErrorCode.INVALID_REQUEST)

    def test_recursive_filter_and_exact_operator_vocabulary(self) -> None:
        state = EpisodeState("episode", max_tool_calls=10)
        _, _, rows = observed(state)
        engine = QueryEngine()
        page = engine.query(
            state=state,
            adapter_id="desktop",
            resource="controls",
            items=rows,
            payload=query_payload(
                where={
                    "op": "all",
                    "filters": [
                        {
                            "op": "any",
                            "filters": [
                                {"op": "starts_with", "field": "name", "value": "A"},
                                {"op": "ends_with", "field": "name", "value": "ma"},
                            ],
                        },
                        {"op": "is_true", "field": "state.enabled"},
                        {
                            "op": "not",
                            "filter": {"op": "eq", "field": "role", "value": "link"},
                        },
                    ],
                }
            ),
        )
        self.assertEqual([record["name"] for record in page.records], ["Alpha", "Gamma"])
        with self.assertRaises(ProtocolError) as old_spelling:
            engine.query(
                state=state,
                adapter_id="desktop",
                resource="controls",
                items=rows,
                payload=query_payload(
                    where={"op": "startswith", "field": "name", "value": "A"}
                ),
            )
        self.assertEqual(old_spelling.exception.code, ErrorCode.INVALID_REQUEST)

    def test_cursor_expires_at_ten_minutes(self) -> None:
        now = [0.0]
        state = EpisodeState(
            "episode",
            max_tool_calls=10,
            limits=StateLimits(cursor_ttl_seconds=600),
            clock=lambda: now[0],
        )
        _, _, rows = observed(state)
        engine = QueryEngine()
        first = engine.query(
            state=state,
            adapter_id="desktop",
            resource="controls",
            items=rows,
            payload=query_payload(limit=1),
        )
        now[0] = 601
        with self.assertRaises(ProtocolError) as expired:
            engine.query(
                state=state,
                adapter_id="desktop",
                resource="controls",
                items=rows,
                payload=query_payload(limit=1, cursor=first.next_cursor),
            )
        self.assertEqual(expired.exception.code, ErrorCode.REVISION_CONFLICT)


if __name__ == "__main__":
    unittest.main()
