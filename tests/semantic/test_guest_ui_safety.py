from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from envserver.semantic.protocol import ErrorCode, SideEffectState
from envserver.semantic.runtime import _agent_error
from guest_agent import semantic_agent


class _State:
    def contains(self, _flag):
        return True


class _Component:
    def __init__(self, hit):
        self.hit = hit

    def getExtents(self, _coordinates):
        return SimpleNamespace(x=10, y=20, width=100, height=30)

    def getAccessibleAtPoint(self, _x, _y, _coordinates):
        return self.hit


class _Target:
    name = "Target"
    description = ""

    def __init__(self, *, hit=None, action_interface=None):
        self.hit = self if hit is None else hit
        self.action_interface = action_interface

    def __str__(self):
        return f"stable-target-{id(self)}"

    def __repr__(self):
        return f"stable-target-{id(self)}"

    def getState(self):
        return _State()

    def queryComponent(self):
        return _Component(self.hit)

    def queryAction(self):
        if self.action_interface is None:
            raise RuntimeError("no action")
        return self.action_interface


class _Registry:
    events = []

    @classmethod
    def generateMouseEvent(cls, x, y, event):
        cls.events.append((x, y, event))


def _fake_atspi():
    return SimpleNamespace(
        Registry=_Registry,
        DESKTOP_COORDS=0,
        STATE_ENABLED=1,
        STATE_VISIBLE=2,
        STATE_SHOWING=3,
    )


class GuestSemanticInputSafetyTests(unittest.TestCase):
    def setUp(self):
        _Registry.events.clear()

    def test_fallback_requires_exact_private_hit_and_exposes_no_geometry(self):
        target = _Target()
        ref = semantic_agent.STATE.ref_for(target, {"kind": "test"})
        with mock.patch.object(semantic_agent, "_atspi_module", _fake_atspi):
            result = semantic_agent._ui_action(ref, "invoke", {})
        self.assertEqual(result["execution_path"], "semantic_input")
        self.assertEqual(result["private_hit_test"], "matched")
        self.assertNotIn("x", result)
        self.assertNotIn("y", result)
        self.assertEqual(len(_Registry.events), 1)

    def test_mismatched_hit_fails_before_private_input(self):
        target = _Target(hit=_Target())
        ref = semantic_agent.STATE.ref_for(target, {"kind": "test-mismatch"})
        with mock.patch.object(semantic_agent, "_atspi_module", _fake_atspi):
            with self.assertRaises(semantic_agent.AgentError) as caught:
                semantic_agent._ui_action(ref, "invoke", {})
        self.assertEqual(caught.exception.code, "precondition_failed")
        self.assertEqual(_Registry.events, [])

    def test_false_direct_action_is_uncertain_and_never_falls_back(self):
        interface = SimpleNamespace(
            nActions=1,
            getName=lambda _index: "click",
            doAction=lambda _index: False,
        )
        target = _Target(action_interface=interface)
        ref = semantic_agent.STATE.ref_for(target, {"kind": "test-direct"})
        with mock.patch.object(semantic_agent, "_atspi_module", _fake_atspi):
            with self.assertRaises(semantic_agent.AgentError) as caught:
                semantic_agent._ui_action(ref, "invoke", {})
        self.assertEqual(caught.exception.code, "uncertain")
        self.assertEqual(caught.exception.side_effect_state, "unknown")
        self.assertEqual(_Registry.events, [])

    def test_outer_kernel_preserves_guest_unknown_side_effect_state(self):
        error = _agent_error({
            "error": {
                "code": "uncertain",
                "message": "mutation result unknown",
                "side_effect_state": "unknown",
            }
        }, during_action=True)
        self.assertEqual(error.code, ErrorCode.UNCERTAIN)
        self.assertEqual(error.side_effect_state, SideEffectState.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
