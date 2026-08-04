from __future__ import annotations

import unittest
import uuid
from typing import Any, Mapping

from envserver.semantic.runtime import SemanticRuntime, _guest_where_pushdown


def _query_payload(where: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "resource": "ui.elements",
        "scope": {},
        "where": dict(where),
        "fields": [],
        "order_by": [],
        "parameters": {},
        "limit": 30,
        "freshness": "live",
    }


def _legacy_guest_match(record: Mapping[str, Any], where: Mapping[str, Any]) -> bool:
    if not where:
        return True
    if "all" in where:
        return all(_legacy_guest_match(record, child) for child in where["all"])
    if "any" in where:
        return any(_legacy_guest_match(record, child) for child in where["any"])
    if "not" in where:
        return not _legacy_guest_match(record, where["not"])
    value: Any = record
    for part in str(where["field"]).split("."):
        value = value.get(part) if isinstance(value, Mapping) else None
    if "eq" in where:
        return value == where["eq"]
    if "contains" in where:
        return str(where["contains"]).casefold() in str(value).casefold()
    raise AssertionError(where)


class GuestFilterPushdownTests(unittest.TestCase):
    def test_exact_scalar_equality_and_safe_boolean_composition_translate(self):
        role = {"op": "eq", "field": "role", "value": "tree item"}
        name = {"op": "eq", "field": "name", "value": "Bills"}
        self.assertEqual(
            _guest_where_pushdown({"op": "all", "filters": [role, name]}),
            {"all": [
                {"field": "role", "eq": "tree item"},
                {"field": "name", "eq": "Bills"},
            ]},
        )
        self.assertEqual(
            _guest_where_pushdown({"op": "any", "filters": [role, name]}),
            {"any": [
                {"field": "role", "eq": "tree item"},
                {"field": "name", "eq": "Bills"},
            ]},
        )
        self.assertEqual(
            _guest_where_pushdown({"op": "not", "filter": role}),
            {"not": {"field": "role", "eq": "tree item"}},
        )

    def test_unsupported_canonical_leaves_fall_back_without_narrowing(self):
        valued = (
            "ne", "contains", "starts_with", "ends_with", "matches",
            "gt", "gte", "lt", "lte", "in", "has",
        )
        for operation in valued:
            with self.subTest(operation=operation):
                self.assertEqual(_guest_where_pushdown({
                    "op": operation, "field": "name", "value": "Bills",
                }), {})
        for operation in ("is_true", "is_false"):
            with self.subTest(operation=operation):
                self.assertEqual(_guest_where_pushdown({
                    "op": operation, "field": "states.selected",
                }), {})
        for value in (None, ["Bills"], {"name": "Bills"}):
            with self.subTest(value=value):
                self.assertEqual(_guest_where_pushdown({
                    "op": "eq", "field": "name", "value": value,
                }), {})
        self.assertEqual(_guest_where_pushdown({
            "op": "eq", "field": "children.0.name", "value": "Bills",
        }), {})

    def test_contains_pushdown_requires_a_contractually_string_field(self):
        contains = {"op": "contains", "field": "name", "value": "Invoice"}
        self.assertEqual(_guest_where_pushdown(contains), {})
        self.assertEqual(
            _guest_where_pushdown(
                contains, contains_fields=frozenset({"name", "text"})
            ),
            {"field": "name", "contains": "Invoice"},
        )
        self.assertEqual(
            _guest_where_pushdown(
                {**contains, "field": "value"},
                contains_fields=frozenset({"name", "text"}),
            ),
            {},
        )

    def test_partial_all_is_safe_but_partial_any_and_negation_fall_back(self):
        exact = {"op": "eq", "field": "role", "value": "tree item"}
        unsupported = {"op": "contains", "field": "name", "value": "Invoice"}
        partial_all = {"op": "all", "filters": [exact, unsupported]}
        self.assertEqual(
            _guest_where_pushdown(partial_all),
            {"all": [{"field": "role", "eq": "tree item"}]},
        )
        self.assertEqual(
            _guest_where_pushdown({"op": "any", "filters": [exact, unsupported]}),
            {},
        )
        self.assertEqual(
            _guest_where_pushdown({"op": "not", "filter": partial_all}),
            {},
        )

    def make_runtime(self):
        records = [{
            "ref": f"native-noise-{index}",
            "kind": "ui.element",
            "role": "button",
            "name": f"Noise {index}",
            "state": {},
            "advertised_actions": [],
        } for index in range(2_300)]
        records.extend((
            {
                "ref": "native-bills", "kind": "ui.element",
                "role": "tree item", "name": "Bills", "state": {},
                "advertised_actions": ["activate"],
            },
            {
                "ref": "native-invoice", "kind": "ui.element",
                "role": "tree item", "name": "Amazon Invoice Available",
                "state": {}, "advertised_actions": ["activate"],
            },
            {
                "ref": "native-lowercase-invoice", "kind": "ui.element",
                "role": "tree item", "name": "amazon invoice available",
                "state": {}, "advertised_actions": ["activate"],
            },
        ))
        requests: list[dict[str, Any]] = []
        revision = 1

        def request(method, path, payload=None):
            nonlocal revision
            self.assertEqual(method, "POST")
            if path == "/v1/act":
                self.assertEqual(payload["target"], {"ref": "native-bills"})
                revision += 1
                return {"ok": True, "result": {
                    "execution_path": "accessibility", "invoked": True,
                }}
            self.assertEqual(path, "/v1/query")
            requests.append(dict(payload))
            selected = [
                record for record in records
                if _legacy_guest_match(record, payload.get("where") or {})
            ]
            offset = int(payload["internal_offset"])
            limit = int(payload["limit"])
            page = selected[offset:offset + limit]
            end = offset + len(page)
            return {"ok": True, "result": {
                "records": page,
                # A pushed view retains the full native-surface revision.
                "revision": f"full-accessibility-revision-{revision}",
                "total": len(selected),
                "truncated": end < len(selected),
                "next_internal_offset": end if end < len(selected) else None,
            }}

        runtime = SemanticRuntime(
            episode_id="guest-filter-pushdown",
            max_tool_calls=40,
            guest_request=request,
            guest_capabilities=[{
                "adapter_id": "universal-atspi@1",
                "resources": ["ui.elements"],
                "actions": ["invoke"],
                "execution_paths": ["accessibility"],
                "accepts_entity_target": True,
            }],
        )
        return runtime, requests

    def dispatch(self, runtime: SemanticRuntime, where: Mapping[str, Any]):
        return runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": "guest-filter-pushdown",
            "operation": "query",
            "payload": _query_payload(where),
        })

    def test_exact_role_name_lookup_transports_one_page_not_full_tree(self):
        runtime, requests = self.make_runtime()
        response = self.dispatch(runtime, {"op": "all", "filters": [
            {"op": "eq", "field": "role", "value": "tree item"},
            {"op": "eq", "field": "name", "value": "Bills"},
        ]})

        self.assertEqual(response["status"], "ok", response)
        self.assertEqual([record["name"] for record in response["result"]["records"]], ["Bills"])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["where"], {"all": [
            {"field": "role", "eq": "tree item"},
            {"field": "name", "eq": "Bills"},
        ]})

    def test_partial_pushdown_keeps_canonical_contains_as_outer_authority(self):
        runtime, requests = self.make_runtime()
        response = self.dispatch(runtime, {"op": "all", "filters": [
            {"op": "eq", "field": "role", "value": "tree item"},
            {"op": "contains", "field": "name", "value": "Invoice"},
        ]})

        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(
            [record["name"] for record in response["result"]["records"]],
            ["Amazon Invoice Available"],
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0]["where"],
            {"all": [
                {"field": "role", "eq": "tree item"},
                {"field": "name", "contains": "Invoice"},
            ]},
        )

    def test_lone_name_contains_pushes_superset_and_outer_filter_remains_authority(self):
        runtime, requests = self.make_runtime()
        response = self.dispatch(runtime, {
            "op": "contains", "field": "name", "value": "Invoice",
        })

        self.assertEqual(response["status"], "ok", response)
        # Guest case-folding selected both invoice spellings. Canonical
        # case-sensitive filtering retained only the actual requested match.
        self.assertEqual(
            [record["name"] for record in response["result"]["records"]],
            ["Amazon Invoice Available"],
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            requests[0]["where"],
            {"field": "name", "contains": "Invoice"},
        )

    def test_targeted_action_reuses_originating_filter_for_bounded_reobservation(self):
        runtime, requests = self.make_runtime()
        where = {"op": "all", "filters": [
            {"op": "eq", "field": "role", "value": "tree item"},
            {"op": "eq", "field": "name", "value": "Bills"},
        ]}
        observed = self.dispatch(runtime, where)
        target_ref = observed["result"]["records"][0]["ref"]

        acted = runtime.dispatch({
            "protocol_version": "1.0",
            "request_id": str(uuid.uuid4()),
            "episode_id": "guest-filter-pushdown",
            "operation": "act",
            "payload": {
                "target": {"ref": target_ref},
                "action": "invoke",
                "arguments": {},
                "preconditions": [],
                "postconditions": [],
                "confirm": False,
            },
        })

        self.assertEqual(acted["status"], "ok", acted)
        # Initial query, pre-action stale-ref check and post-action revision
        # observation each transport exactly the filtered one-record page.
        self.assertEqual(len(requests), 3)
        expected_pushdown = {"all": [
            {"field": "role", "eq": "tree item"},
            {"field": "name", "eq": "Bills"},
        ]}
        self.assertTrue(all(request["where"] == expected_pushdown for request in requests))
        self.assertTrue(all(request["internal_offset"] == 0 for request in requests))

    def test_unsupported_leaf_fetches_all_pages_and_misses_no_match(self):
        runtime, requests = self.make_runtime()
        response = self.dispatch(runtime, {
            "op": "matches", "field": "name", "value": "Invoice",
        })

        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(
            [record["name"] for record in response["result"]["records"]],
            ["Amazon Invoice Available"],
        )
        self.assertEqual(len(requests), 24)
        self.assertTrue(all(request["where"] == {} for request in requests))


if __name__ == "__main__":
    unittest.main()
