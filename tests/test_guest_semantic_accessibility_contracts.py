"""Focused regressions for native semantic accessibility execution routes."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from guest_agent import semantic_agent


class _ActionInterface:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def doAction(self, index: int) -> bool:
        self.calls.append(index)
        return True


class _ActionNode:
    def __init__(self) -> None:
        self.interface = _ActionInterface()

    def queryAction(self) -> _ActionInterface:
        return self.interface


class _EditableCapabilityNode:
    def __init__(self, *, supports_interface: bool) -> None:
        self.supports_interface = supports_interface
        self.queries = 0

    def queryEditableText(self) -> object:
        self.queries += 1
        if not self.supports_interface:
            raise RuntimeError("EditableText unavailable")
        return object()


class _NamedActionInterface:
    def __init__(self, names: list[str], *, result: bool = True) -> None:
        self.names = names
        self.result = result
        self.calls: list[int] = []
        self.nActions = len(names)

    def getName(self, index: int) -> str:
        return self.names[index]

    def doAction(self, index: int) -> bool:
        self.calls.append(index)
        return self.result


class _HyperlinkInterface:
    def __init__(self, uris: list[str]) -> None:
        self.uris = uris
        self.nAnchors = len(uris)

    def getURI(self, index: int) -> str:
        return self.uris[index]


class _HyperlinkNode:
    def __init__(self, uris: list[str] | None) -> None:
        self.uris = uris

    def queryHyperlink(self) -> _HyperlinkInterface:
        if self.uris is None:
            raise RuntimeError("no hyperlink interface")
        return _HyperlinkInterface(self.uris)


class _AccessibleHyperlinkNode(_HyperlinkNode):
    name = "Billing details"
    description = ""
    childCount = 0

    def getRoleName(self) -> str:
        return "link"

    def getState(self) -> None:
        raise RuntimeError("state unavailable")

    def queryAction(self) -> None:
        raise RuntimeError("action unavailable")

    def queryText(self) -> None:
        raise RuntimeError("text unavailable")

    def queryValue(self) -> None:
        raise RuntimeError("value unavailable")


class _WindowState:
    def __init__(self, enabled: set[str]) -> None:
        self.enabled = enabled

    def contains(self, flag: str) -> bool:
        return flag in self.enabled


class _FocusComponent:
    def __init__(self, *, result: bool = True) -> None:
        self.result = result
        self.calls = 0

    def grabFocus(self) -> bool:
        self.calls += 1
        return self.result


class _WindowNode:
    def __init__(
        self,
        states: list[set[str]],
        *,
        actions: _NamedActionInterface | None = None,
        component: _FocusComponent | None = None,
    ) -> None:
        self.states = states
        self.action_interface = actions
        self.component = component
        self.state_reads = 0

    def getState(self) -> _WindowState:
        index = min(self.state_reads, len(self.states) - 1)
        self.state_reads += 1
        return _WindowState(self.states[index])

    def queryAction(self) -> _NamedActionInterface:
        if self.action_interface is None:
            raise RuntimeError("no action interface")
        return self.action_interface

    def queryComponent(self) -> _FocusComponent:
        if self.component is None:
            raise AssertionError("component fallback must not be used")
        return self.component


class _UnverifiableWindowNode(_WindowNode):
    def getState(self) -> _WindowState:
        raise RuntimeError("state interface unavailable")


def _window_atspi():
    return type(
        "FakeAtspi",
        (),
        {"STATE_ACTIVE": "active", "STATE_FOCUSED": "focused"},
    )()


class _TransportApplication:
    def __init__(self, bus_name: str) -> None:
        self.bus_name = bus_name


class _TransportIdentity:
    def __init__(self, bus_name: str, path: str) -> None:
        self.app = _TransportApplication(bus_name)
        self.path = path


class _FreshBusProxy:
    def __init__(self, bus_name: str, path: str) -> None:
        self.parent = _TransportIdentity(bus_name, path)
        self.name = "Browser"

    def getRoleName(self) -> str:
        return "frame"

    def get_process_id(self) -> int:
        return 900


class _StructuralProxy:
    def __init__(
        self, role: str, name: str, index: int, parent: "_StructuralProxy | None",
    ) -> None:
        self.role_name = role
        self.name = name
        self.index = index
        self.semantic_parent = parent

    def getRoleName(self) -> str:
        return self.role_name

    def getName(self) -> str:
        return self.name

    def get_process_id(self) -> int:
        return 901

    def getIndexInParent(self) -> int:
        return self.index

    def getParent(self) -> "_StructuralProxy | None":
        return self.semantic_parent


class GuestSemanticAccessibilityContracts(unittest.TestCase):
    def test_hyperlink_uri_requires_one_exact_public_web_anchor(self) -> None:
        self.assertEqual(
            semantic_agent._hyperlink_uri(
                _HyperlinkNode(["https://example.test/invoice?month=12"])
            ),
            "https://example.test/invoice?month=12",
        )
        for uris in (
            None,
            [],
            ["mailto:billing@example.test"],
            ["https://user:secret@example.test/private"],
            ["https://one.test/", "https://two.test/"],
        ):
            with self.subTest(uris=uris):
                self.assertEqual(
                    semantic_agent._hyperlink_uri(_HyperlinkNode(uris)), ""
                )

    def test_accessibility_record_carries_proved_hyperlink_uri(self) -> None:
        node = _AccessibleHyperlinkNode([
            "https://billing.example.test/invoice/12"
        ])
        registry = type(
            "Registry", (), {"getDesktop": staticmethod(lambda _index: object())},
        )
        with patch.object(
            semantic_agent,
            "_atspi_module",
            return_value=type("Atspi", (), {"Registry": registry})(),
        ):
            records, _revision = semantic_agent._walk_accessibility(roots=[node])

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["role"], "link")
        self.assertEqual(
            records[0]["url"], "https://billing.example.test/invoice/12"
        )

    def test_text_actions_require_proved_editable_text_interface(self) -> None:
        executable = _EditableCapabilityNode(supports_interface=True)
        state_only = _EditableCapabilityNode(supports_interface=False)

        self.assertEqual(
            semantic_agent._executable_text_actions(
                executable, {"editable": True, "read_only": False},
            ),
            ["set_text", "insert_text", "replace_text"],
        )
        self.assertEqual(
            semantic_agent._executable_text_actions(
                state_only, {"editable": True, "read_only": False},
            ),
            [],
        )
        self.assertEqual(executable.queries, 1)
        self.assertEqual(state_only.queries, 1)

    def test_read_only_or_noneditable_nodes_do_not_probe_editable_text(self) -> None:
        node = _EditableCapabilityNode(supports_interface=True)

        self.assertEqual(
            semantic_agent._executable_text_actions(
                node, {"editable": True, "read_only": True},
            ),
            [],
        )
        self.assertEqual(
            semantic_agent._executable_text_actions(node, {"editable": False}),
            [],
        )
        self.assertEqual(node.queries, 0)

    def test_record_actions_remove_unexecutable_text_named_atspi_actions(self) -> None:
        state_only = _EditableCapabilityNode(supports_interface=False)
        executable = _EditableCapabilityNode(supports_interface=True)

        with patch.object(
            semantic_agent,
            "_actions",
            return_value=["activate", "set-text", "replace_text"],
        ):
            self.assertEqual(
                semantic_agent._record_actions(state_only, {"editable": True}),
                ["activate"],
            )
            self.assertEqual(
                semantic_agent._record_actions(executable, {"editable": True}),
                ["activate", "set_text", "insert_text", "replace_text"],
            )

    def test_file_chooser_filter_adds_only_authoritative_named_modes(self) -> None:
        cases = (
            ("Open File", None, "open"),
            ("Select a File", None, "select"),
            ("Choose Folder", None, "choose"),
            ("Save As", None, "save"),
            ("", "Open File", "open"),
            ("Attach a File", None, None),
            ("Open or Save File", None, None),
            ("File Chooser", None, None),
        )
        for name, title, expected_mode in cases:
            with self.subTest(name=name, title=title):
                record = {
                    "ref": "chooser",
                    "role": "file chooser",
                    "name": name,
                    "state": {"visible": True},
                    # A stale/upstream claim must not override the visible
                    # top-level chooser identity.
                    "mode": "open",
                }
                if title is not None:
                    record["title"] = title

                filtered = semantic_agent._surface_filter(
                    "os.file_choosers", [record]
                )

                self.assertEqual(len(filtered), 1)
                self.assertEqual(filtered[0].get("mode"), expected_mode)
                self.assertIsNot(filtered[0], record)

    def test_file_chooser_filter_does_not_classify_an_ordinary_dialog(self) -> None:
        record = {
            "ref": "preferences",
            "role": "dialog",
            "name": "Open preferences",
            "state": {"visible": True},
        }

        self.assertEqual(
            semantic_agent._surface_filter("os.file_choosers", [record]), []
        )

    def test_action_index_never_uses_an_unrelated_sole_action(self) -> None:
        interface = _NamedActionInterface(["scroll into view"])

        with self.assertRaises(semantic_agent.AgentError) as caught:
            semantic_agent._atspi_action_index(interface, "invoke", "")

        self.assertEqual(caught.exception.code, "unsupported")
        self.assertEqual(interface.calls, [])

    def test_action_index_dispatches_expand_and_collapse_exactly(self) -> None:
        interface = _NamedActionInterface(["expand", "collapse"])

        self.assertEqual(semantic_agent._atspi_action_index(interface, "expand", ""), 0)
        self.assertEqual(semantic_agent._atspi_action_index(interface, "collapse", ""), 1)

    def test_action_index_accepts_link_aliases_and_normalizes_separators(self) -> None:
        for action_name in ("link.open", "open_link", "jump", "do-default"):
            with self.subTest(action_name=action_name):
                interface = _NamedActionInterface([action_name])
                self.assertEqual(
                    semantic_agent._atspi_action_index(interface, "invoke", ""), 0
                )
                self.assertEqual(
                    semantic_agent._atspi_action_index(
                        interface, "invoke", action_name.replace(".", "_")
                    ),
                    0,
                )

    def test_file_chooser_link_uses_advertised_action_not_private_click(self) -> None:
        node = _ActionNode()
        with patch.object(
            semantic_agent,
            "_private_semantic_click",
            side_effect=AssertionError("private click must not be used"),
        ):
            applied = semantic_agent._invoke_chooser_node(node, ["link.open"])
        self.assertTrue(applied)
        self.assertEqual(node.interface.calls, [0])

    def test_activate_window_prefers_direct_action_and_verifies_active_state(self) -> None:
        interface = _NamedActionInterface(["activate"])
        node = _WindowNode([set(), {"active"}], actions=interface)
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-direct"})

        with patch.object(semantic_agent, "_atspi_module", _window_atspi):
            result = semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(interface.calls, [0])
        self.assertEqual(result["status"], "applied")
        self.assertTrue(result["changed"])
        self.assertEqual(result["activation_path"], "atspi_action")
        self.assertEqual(result["verification"], "active_or_focused")

    def test_activate_window_falls_back_to_component_focus_without_matching_action(self) -> None:
        interface = _NamedActionInterface(["close"])
        component = _FocusComponent()
        node = _WindowNode(
            [set(), {"focused"}], actions=interface, component=component,
        )
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-focus"})

        with patch.object(semantic_agent, "_atspi_module", _window_atspi):
            result = semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(interface.calls, [])
        self.assertEqual(component.calls, 1)
        self.assertEqual(result["activation_path"], "component_focus")
        self.assertEqual(result["verification"], "active_or_focused")

    def test_failed_direct_window_action_is_uncertain_and_never_focuses(self) -> None:
        interface = _NamedActionInterface(["activate"], result=False)
        component = _FocusComponent()
        node = _WindowNode(
            [set()], actions=interface, component=component,
        )
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-false"})

        with patch.object(semantic_agent, "_atspi_module", _window_atspi):
            with self.assertRaises(semantic_agent.AgentError) as caught:
                semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(caught.exception.code, "uncertain")
        self.assertEqual(caught.exception.side_effect_state, "unknown")
        self.assertEqual(interface.calls, [0])
        self.assertEqual(component.calls, 0)

    def test_already_active_window_returns_typed_no_effect_without_dispatch(self) -> None:
        interface = _NamedActionInterface(["activate"])
        node = _WindowNode([{"active"}], actions=interface)
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-active"})

        with patch.object(semantic_agent, "_atspi_module", _window_atspi):
            with self.assertRaises(semantic_agent.AgentError) as caught:
                semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(caught.exception.code, "no_effect")
        self.assertEqual(caught.exception.side_effect_state, "none")
        self.assertEqual(interface.calls, [])

    def test_window_manager_overrides_stale_atspi_active_during_switch(self) -> None:
        interface = _NamedActionInterface(["activate"])
        # The target's stale AT-SPI state claims it is active throughout.  The
        # same WM authority used by ui.surfaces says another window is active
        # before dispatch and the requested target is active afterward.
        node = _WindowNode([{"active"}], actions=interface)
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-stale-active"})

        with patch.object(
            semantic_agent, "_atspi_module", _window_atspi,
        ), patch.object(
            semantic_agent,
            "_authoritative_active_surface_ref",
            side_effect=[(True, "different-window"), (True, ref)],
        ):
            result = semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(interface.calls, [0])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["verification"], "window_manager")

    def test_window_activation_waits_for_delayed_wm_propagation(self) -> None:
        component = _FocusComponent(result=True)
        node = _WindowNode([set()], component=component)
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-wm-delayed"})

        with patch.object(
            semantic_agent, "_atspi_module", _window_atspi,
        ), patch.object(
            semantic_agent,
            "_authoritative_active_surface_ref",
            side_effect=[
                (True, "current-window"),
                (True, "current-window"),
                (True, "current-window"),
                (True, ref),
            ],
        ), patch.object(
            semantic_agent, "_wm_activate_accessible",
            side_effect=AssertionError("ordinary WM propagation must win"),
        ), patch.object(semantic_agent.time, "sleep") as sleep:
            result = semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(component.calls, 1)
        self.assertEqual(result["activation_path"], "component_focus")
        self.assertEqual(result["verification"], "window_manager")
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.1)

    def test_true_component_focus_falls_back_to_unique_wm_activation(self) -> None:
        component = _FocusComponent(result=True)
        node = _WindowNode([set()], component=component)
        ref = semantic_agent.STATE.ref_for(
            node, {"kind": "window-focus-not-raised"}
        )

        with patch.object(
            semantic_agent, "_atspi_module", _window_atspi,
        ), patch.object(
            semantic_agent,
            "_authoritative_active_surface_ref",
            side_effect=[(True, "current-window")] * 9 + [(True, ref)],
        ), patch.object(
            semantic_agent, "_wm_activate_accessible", return_value=True,
        ) as wm_activate, patch.object(semantic_agent.time, "sleep") as sleep:
            result = semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(component.calls, 1)
        wm_activate.assert_called_once_with(node)
        self.assertEqual(result["execution_path"], "semantic_input")
        self.assertEqual(
            result["activation_path"], "component_focus_then_wm_semantic"
        )
        self.assertEqual(result["verification"], "window_manager")
        self.assertEqual(sleep.call_count, 7)

    def test_window_manager_active_target_returns_no_effect_without_dispatch(self) -> None:
        interface = _NamedActionInterface(["activate"])
        node = _WindowNode([set()], actions=interface)
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-wm-active"})

        with patch.object(
            semantic_agent, "_atspi_module", _window_atspi,
        ), patch.object(
            semantic_agent,
            "_authoritative_active_surface_ref",
            return_value=(True, ref),
        ):
            with self.assertRaises(semantic_agent.AgentError) as caught:
                semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(caught.exception.code, "no_effect")
        self.assertEqual(caught.exception.side_effect_state, "none")
        self.assertEqual(interface.calls, [])

    def test_unconfirmed_window_activation_returns_typed_postcondition_failure(self) -> None:
        component = _FocusComponent()
        node = _WindowNode([set()], component=component)
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-unconfirmed"})

        with patch.object(
            semantic_agent, "_atspi_module", _window_atspi,
        ), patch.object(semantic_agent.time, "sleep"):
            with self.assertRaises(semantic_agent.AgentError) as caught:
                semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(caught.exception.code, "postcondition_failed")
        self.assertEqual(caught.exception.side_effect_state, "unknown")
        self.assertEqual(component.calls, 1)

    def test_unverifiable_window_activation_returns_typed_uncertainty(self) -> None:
        interface = _NamedActionInterface(["activate"])
        node = _UnverifiableWindowNode([set()], actions=interface)
        ref = semantic_agent.STATE.ref_for(node, {"kind": "window-unverifiable"})

        with patch.object(
            semantic_agent, "_atspi_module", _window_atspi,
        ), patch.object(semantic_agent.time, "sleep"):
            with self.assertRaises(semantic_agent.AgentError) as caught:
                semantic_agent._ui_action(ref, "activate_window", {})

        self.assertEqual(caught.exception.code, "uncertain")
        self.assertEqual(caught.exception.side_effect_state, "unknown")
        self.assertEqual(interface.calls, [0])

    def test_fresh_atspi_proxies_for_same_bus_object_retain_ref(self) -> None:
        state = semantic_agent.AgentState()
        first = _FreshBusProxy(":1.42", "/org/a11y/atspi/accessible/17")
        second = _FreshBusProxy(":1.42", "/org/a11y/atspi/accessible/17")

        first_ref = state.ref_for(first, {"role": "frame", "name": "Browser"})
        second_ref = state.ref_for(second, {"role": "frame", "name": "Browser"})

        self.assertEqual(first_ref, second_ref)
        self.assertIs(state.refs[first_ref], second)

    def test_structural_identity_keeps_same_looking_siblings_distinct_and_stable(self) -> None:
        state = semantic_agent.AgentState()

        def tree() -> tuple[_StructuralProxy, _StructuralProxy]:
            app = _StructuralProxy("application", "Editor", 0, None)
            return (
                _StructuralProxy("button", "Open", 0, app),
                _StructuralProxy("button", "Open", 1, app),
            )

        first_left, first_right = tree()
        left_ref = state.ref_for(first_left, {"role": "button", "name": "Open"})
        right_ref = state.ref_for(first_right, {"role": "button", "name": "Open"})
        second_left, second_right = tree()

        self.assertNotEqual(left_ref, right_ref)
        self.assertEqual(
            left_ref,
            state.ref_for(second_left, {"role": "button", "name": "Open"}),
        )
        self.assertEqual(
            right_ref,
            state.ref_for(second_right, {"role": "button", "name": "Open"}),
        )


if __name__ == "__main__":
    unittest.main()
