from __future__ import annotations

import asyncio
import inspect
import threading
import unittest

from envserver.semantic.adapters import AdapterContext
from envserver.semantic.browser_adapter import (
    MAX_BROWSER_AX_RECORDS,
    AsyncBrowserAdapter,
    _compile_ax_records,
    _normalized_ax_states,
    _public_url,
)
from envserver.semantic.protocol import ErrorCode, ProtocolError, SideEffectState, Status


class BrowserAdapterLoopTests(unittest.TestCase):
    def test_all_coroutines_run_on_one_owned_event_loop_thread(self):
        adapter = AsyncBrowserAdapter("192.0.2.1", call_timeout_seconds=1)
        async def identity():
            return id(asyncio.get_running_loop()), threading.get_ident()
        try:
            first = adapter._call(identity())
            second = adapter._call(identity())
            self.assertEqual(first, second)
            self.assertNotEqual(first[1], threading.get_ident())
        finally:
            adapter.close()


class BrowserAccessibilityCompactionTests(unittest.TestCase):
    @staticmethod
    def _remember(_kind, _identity, payload):
        return f"ref-{payload['backend_node_id']}"

    @staticmethod
    def _node(
        index,
        *,
        role="StaticText",
        name=None,
        parent=None,
        ignored=False,
        properties=(),
    ):
        return {
            "nodeId": f"ax-{index}",
            "parentId": None if parent is None else f"ax-{parent}",
            "backendDOMNodeId": index + 1,
            "role": {"value": role},
            "name": {"value": name if name is not None else f"item {index}"},
            "ignored": ignored,
            "properties": list(properties),
        }

    def test_dense_tree_keeps_actionables_compacts_and_rebuilds_relationships(self):
        count = MAX_BROWSER_AX_RECORDS + 1_003
        actionable_indexes = {7, MAX_BROWSER_AX_RECORDS + 100, count - 1}
        stateful_index = MAX_BROWSER_AX_RECORDS + 200
        nodes = [
            self._node(
                index,
                role=(
                    "button"
                    if index in actionable_indexes
                    else "generic"
                    if index == stateful_index
                    else "StaticText"
                ),
                name="" if index == stateful_index else None,
                parent=index - 1 if index else None,
                properties=(
                    [{"name": "expanded", "value": {"value": False}}]
                    if index == stateful_index
                    else []
                ),
            )
            for index in range(count)
        ]
        page = object()
        trees = [({"id": "main", "url": "https://example.test/dense"}, nodes)]

        records, summary = _compile_ax_records(page, trees, self._remember)
        repeated, repeated_summary = _compile_ax_records(page, trees, self._remember)

        self.assertEqual(len(records), MAX_BROWSER_AX_RECORDS)
        self.assertEqual(summary["total_records"], count)
        self.assertEqual(summary["retained_records"], MAX_BROWSER_AX_RECORDS)
        self.assertEqual(summary["truncated_records"], 1_003)
        self.assertEqual(summary["budget_truncated_records"], 1_003)
        self.assertTrue(summary["truncated"])
        self.assertEqual(summary, repeated_summary)
        self.assertEqual(records, repeated)

        retained_backend_ids = [int(record["ref"].split("-")[1]) for record in records]
        self.assertEqual(retained_backend_ids, sorted(retained_backend_ids))
        for index in actionable_indexes:
            ref = f"ref-{index + 1}"
            record = next(item for item in records if item["ref"] == ref)
            self.assertIn("invoke", record["advertised_actions"])
        stateful = next(
            item for item in records if item["ref"] == f"ref-{stateful_index + 1}"
        )
        self.assertIs(stateful["states"]["expanded"], False)

        refs = {record["ref"] for record in records}
        by_ref = {record["ref"]: record for record in records}
        for record in records:
            if record["parent_ref"] is not None:
                self.assertIn(record["parent_ref"], refs)
                self.assertIn(record["ref"], by_ref[record["parent_ref"]]["child_refs"])
            self.assertTrue(set(record["child_refs"]).issubset(refs))
            for child_ref in record["child_refs"]:
                self.assertEqual(by_ref[child_ref]["parent_ref"], record["ref"])

    def test_ax_disabled_and_readonly_states_use_canonical_names(self):
        states = _normalized_ax_states({"disabled": True, "readonly": True})

        self.assertIs(states["enabled"], False)
        self.assertIs(states["read_only"], True)

    def test_ignored_and_semantically_empty_nodes_are_removed_without_dangling_refs(self):
        nodes = [
            self._node(0, role="RootWebArea", name=""),
            self._node(1, role="button", name="ignored", parent=0, ignored=True),
            self._node(2, role="generic", name="", parent=1),
            self._node(3, role="button", name="Keep button", parent=2),
            self._node(4, role="link", name="Keep link", parent=2),
        ]
        records, summary = _compile_ax_records(
            object(),
            [({"id": "main", "url": "https://example.test"}, nodes)],
            self._remember,
        )

        self.assertEqual([record["ref"] for record in records], ["ref-1", "ref-4", "ref-5"])
        self.assertEqual(summary["total_records"], 5)
        self.assertEqual(summary["retained_records"], 3)
        self.assertEqual(summary["ignored_records"], 1)
        self.assertEqual(summary["empty_records"], 1)
        self.assertEqual(summary["truncated_records"], 2)
        self.assertEqual(records[1]["parent_ref"], "ref-1")
        self.assertEqual(records[2]["parent_ref"], "ref-1")
        self.assertEqual(records[0]["child_refs"], ["ref-4", "ref-5"])

    def test_observation_summary_exposes_compaction_counts(self):
        adapter = object.__new__(AsyncBrowserAdapter)
        summary = {
            "total_records": 6_001,
            "retained_records": 5_000,
            "truncated_records": 1_001,
            "truncated": True,
        }

        async def fake_ax_records():
            return ([{
                "ref": "ref-1",
                "kind": "browser.element",
                "role": "button",
                "name": "Go",
                "advertised_actions": ["invoke"],
                "parent_ref": None,
                "child_refs": [],
            }], summary)

        adapter._ax_records = fake_ax_records  # type: ignore[method-assign]
        observation = asyncio.run(adapter._observe("browser.elements"))

        self.assertEqual(observation.summary["record_count"], 1)
        for key, value in summary.items():
            self.assertEqual(observation.summary[key], value)

    def test_adapter_contains_no_sync_playwright_route(self):
        source = inspect.getsource(
            __import__("envserver.semantic.browser_adapter", fromlist=["*"])
        )
        self.assertNotIn("playwright.sync_api", source)
        self.assertNotIn("from playwright.sync_api import", source)
        self.assertIn("playwright.async_api", source)

    def test_public_urls_remove_credentials_fragments_and_sensitive_values(self):
        public = _public_url(
            "https://user:pass@example.com/path?token=secret&query=kept#fragment"
        )
        self.assertEqual(
            public,
            "https://example.com/path?token=%5Bredacted%5D&query=kept",
        )
        self.assertNotIn("pass", public)
        self.assertNotIn("secret", public)

    def test_action_timeout_is_unknown_and_never_safe_to_replay(self):
        adapter = AsyncBrowserAdapter(
            "192.0.2.1", call_timeout_seconds=0.01
        )

        async def slow_action(_payload):
            await asyncio.sleep(1)

        adapter._act = slow_action  # type: ignore[method-assign]
        try:
            with self.assertRaises(ProtocolError) as caught:
                adapter.act(
                    AdapterContext("episode", "browser.page", "act", None),
                    {"target": {"ref": "x"}, "action": "navigate", "arguments": {}},
                )
            self.assertEqual(caught.exception.code, ErrorCode.UNCERTAIN)
            self.assertEqual(
                caught.exception.side_effect_state, SideEffectState.UNKNOWN
            )
        finally:
            adapter.close()


class BrowserInvokeRoutingTests(unittest.TestCase):
    @staticmethod
    def _adapter():
        adapter = object.__new__(AsyncBrowserAdapter)
        adapter._native = {
            "native-element": {
                "kind": "backend_node",
                "page": object(),
                "backend_node_id": 17,
            }
        }
        return adapter

    @staticmethod
    def _payload():
        return {
            "target": {"ref": "native-element"},
            "action": "invoke",
            "arguments": {},
        }

    def test_ordinary_invoke_uses_native_dom_click_not_private_hit_testing(self):
        adapter = self._adapter()
        declarations = []
        semantic_input_calls = []

        async def fake_call_on_node(_native, declaration, arguments=()):
            declarations.append(declaration)
            if "HTMLInputElement" in declaration:
                return False
            return True

        async def fake_semantic_input(native):
            semantic_input_calls.append(native)

        adapter._call_on_node = fake_call_on_node  # type: ignore[method-assign]
        adapter._semantic_input_invoke = fake_semantic_input  # type: ignore[method-assign]

        result = asyncio.run(adapter._act(self._payload()))

        self.assertEqual(result.result["execution_path"], "native_api")
        self.assertEqual(len(declarations), 2)
        self.assertIn("HTMLInputElement", declarations[0])
        self.assertIn("this.click()", declarations[1])
        self.assertEqual(semantic_input_calls, [])

    def test_file_input_invoke_keeps_trusted_semantic_input_route(self):
        adapter = self._adapter()
        declarations = []
        semantic_input_calls = []

        async def fake_call_on_node(_native, declaration, arguments=()):
            declarations.append(declaration)
            return True

        async def fake_semantic_input(native):
            semantic_input_calls.append(native)

        adapter._call_on_node = fake_call_on_node  # type: ignore[method-assign]
        adapter._semantic_input_invoke = fake_semantic_input  # type: ignore[method-assign]

        result = asyncio.run(adapter._act(self._payload()))

        self.assertEqual(result.result["execution_path"], "semantic_input")
        self.assertEqual(len(declarations), 1)
        self.assertIn("HTMLInputElement", declarations[0])
        self.assertEqual(len(semantic_input_calls), 1)


class _OpenTabPage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.closed = False
        self.front_calls = 0

    def is_closed(self) -> bool:
        return self.closed

    async def goto(self, url: str, *, wait_until: str) -> None:
        self.url = url
        assert wait_until == "domcontentloaded"

    async def bring_to_front(self) -> None:
        self.front_calls += 1


class _OpenTabContext:
    def __init__(self, pages: list[_OpenTabPage]) -> None:
        self.pages = pages
        self.created: _OpenTabPage | None = None

    async def new_page(self) -> _OpenTabPage:
        self.created = _OpenTabPage("about:blank")
        self.pages.append(self.created)
        return self.created


class BrowserOpenTabContractTests(unittest.TestCase):
    def test_open_tab_proves_creation_and_preserves_every_existing_tab(self) -> None:
        existing = [
            _OpenTabPage("https://one.example.test/"),
            _OpenTabPage("https://two.example.test/"),
        ]
        context = _OpenTabContext(existing)
        adapter = object.__new__(AsyncBrowserAdapter)
        adapter._native = {
            "native-existing": {"kind": "tab", "page": existing[1]},
        }
        adapter._surface_ids = {}
        adapter._active = existing[1]

        async def current_context():
            return context

        adapter._context = current_context  # type: ignore[method-assign]

        result = asyncio.run(adapter._act({
            "target": {"ref": "native-existing"},
            "action": "open_tab",
            "arguments": {"url": "https://billing.example.test/invoice/12"},
        }))

        self.assertEqual(result.status, Status.OK)
        self.assertTrue(result.changed)
        self.assertIsNotNone(context.created)
        self.assertEqual(context.created.front_calls, 1)
        self.assertEqual(
            result.result["opened_url"],
            "https://billing.example.test/invoice/12",
        )
        self.assertEqual(result.result["tab_count_before"], 2)
        self.assertEqual(result.result["tab_count_after"], 3)
        self.assertIs(result.result["existing_tabs_preserved"], True)
        self.assertNotEqual(
            result.result["opened_surface_id"],
            adapter._surface_id(existing[0]),
        )
        self.assertFalse(any(page.closed for page in existing))


class _WaitPage:
    def __init__(self) -> None:
        self.waits: list[int] = []

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)


class BrowserTextMutationReconciliationTests(unittest.TestCase):
    @staticmethod
    def _native(page: object | None = None):
        return {
            "kind": "backend_node",
            "page": page or _WaitPage(),
            "backend_node_id": 17,
            "semantic_identity": {
                "role": "searchbox",
                "name": "Search",
                "description": "Search the catalog",
                "frame_name": "",
            },
        }

    def test_same_backend_fresh_value_proves_uncertain_mutation(self) -> None:
        adapter = object.__new__(AsyncBrowserAdapter)
        page = _WaitPage()
        native = self._native(page)

        async def read_value(_native, _declaration, _arguments=()):
            return "needle"

        adapter._call_on_node = read_value  # type: ignore[method-assign]
        adapter._ax_records = lambda: None  # type: ignore[method-assign]

        result = asyncio.run(adapter._reconcile_text_mutation(native, "needle"))

        self.assertEqual(result, "same_backend_value")
        self.assertEqual(page.waits, [50])

    def test_unique_semantic_replacement_proves_value_after_dom_refresh(self) -> None:
        adapter = object.__new__(AsyncBrowserAdapter)
        native = self._native()

        async def stale_backend(_native, _declaration, _arguments=()):
            raise ProtocolError(ErrorCode.STALE_REF, "node replaced")

        async def fresh_ax():
            return ([{
                "role": "searchbox",
                "name": "Search",
                "description": "Search the catalog",
                "frame_name": "",
                "value": "needle",
            }], {})

        adapter._call_on_node = stale_backend  # type: ignore[method-assign]
        adapter._ax_records = fresh_ax  # type: ignore[method-assign]

        result = asyncio.run(adapter._reconcile_text_mutation(native, "needle"))

        self.assertEqual(result, "unique_replacement_value")

    def test_ambiguous_replacement_preserves_uncertainty(self) -> None:
        adapter = object.__new__(AsyncBrowserAdapter)
        native = self._native()

        async def stale_backend(_native, _declaration, _arguments=()):
            raise ProtocolError(ErrorCode.STALE_REF, "node replaced")

        replacement = {
            "role": "searchbox",
            "name": "Search",
            "description": "Search the catalog",
            "frame_name": "",
            "value": "needle",
        }

        async def ambiguous_ax():
            return ([dict(replacement), dict(replacement)], {})

        adapter._call_on_node = stale_backend  # type: ignore[method-assign]
        adapter._ax_records = ambiguous_ax  # type: ignore[method-assign]

        result = asyncio.run(adapter._reconcile_text_mutation(native, "needle"))

        self.assertIsNone(result)

    def test_set_text_returns_applied_only_after_fresh_reconciliation(self) -> None:
        adapter = object.__new__(AsyncBrowserAdapter)
        native = self._native()
        adapter._native = {"native-element": native}

        async def uncertain_mutation(_native, _declaration, _arguments=()):
            raise ProtocolError(
                ErrorCode.UNCERTAIN,
                "context replaced after mutation",
                side_effect_state=SideEffectState.UNKNOWN,
            )

        async def reconcile(_native, expected):
            self.assertEqual(expected, "needle")
            return "unique_replacement_value"

        adapter._call_on_node = uncertain_mutation  # type: ignore[method-assign]
        adapter._reconcile_text_mutation = reconcile  # type: ignore[method-assign]

        result = asyncio.run(adapter._act({
            "target": {"ref": "native-element"},
            "action": "set_text",
            "arguments": {"value": "needle"},
        }))

        self.assertEqual(result.status, Status.OK)
        self.assertTrue(result.changed)
        self.assertEqual(result.result["verification"], "fresh_semantic_value")
        self.assertEqual(
            result.result["reconciliation"], "unique_replacement_value"
        )

    def test_set_text_keeps_original_uncertainty_when_state_is_unproved(self) -> None:
        adapter = object.__new__(AsyncBrowserAdapter)
        native = self._native()
        adapter._native = {"native-element": native}
        original = ProtocolError(
            ErrorCode.UNCERTAIN,
            "context replaced after mutation",
            side_effect_state=SideEffectState.UNKNOWN,
        )

        async def uncertain_mutation(_native, _declaration, _arguments=()):
            raise original

        async def cannot_reconcile(_native, _expected):
            return None

        adapter._call_on_node = uncertain_mutation  # type: ignore[method-assign]
        adapter._reconcile_text_mutation = cannot_reconcile  # type: ignore[method-assign]

        with self.assertRaises(ProtocolError) as caught:
            asyncio.run(adapter._act({
                "target": {"ref": "native-element"},
                "action": "set_text",
                "arguments": {"value": "needle"},
            }))

        self.assertIs(caught.exception, original)
        self.assertEqual(caught.exception.code, ErrorCode.UNCERTAIN)
        self.assertEqual(
            caught.exception.side_effect_state, SideEffectState.UNKNOWN
        )

    def test_pre_mutation_stale_ref_is_not_reconciled(self) -> None:
        adapter = object.__new__(AsyncBrowserAdapter)
        native = self._native()
        adapter._native = {"native-element": native}
        stale = ProtocolError(
            ErrorCode.STALE_REF, "node was stale before mutation", retryable=True,
        )

        async def stale_before_mutation(_native, _declaration, _arguments=()):
            raise stale

        async def forbidden_reconciliation(_native, _expected):
            raise AssertionError("pre-mutation errors must not be reconciled")

        adapter._call_on_node = stale_before_mutation  # type: ignore[method-assign]
        adapter._reconcile_text_mutation = forbidden_reconciliation  # type: ignore[method-assign]

        with self.assertRaises(ProtocolError) as caught:
            asyncio.run(adapter._act({
                "target": {"ref": "native-element"},
                "action": "set_text",
                "arguments": {"value": "needle"},
            }))

        self.assertIs(caught.exception, stale)
        self.assertEqual(caught.exception.side_effect_state, SideEffectState.NONE)


if __name__ == "__main__":
    unittest.main()
