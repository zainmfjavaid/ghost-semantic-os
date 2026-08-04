#!/usr/bin/env python3
"""Hermetic contract test for the model-free semantic-simple canary runner."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "infra" / "gcp_semantic_simple_canary.py"
SPEC = importlib.util.spec_from_file_location("gcp_semantic_simple_canary", MODULE_PATH)
assert SPEC and SPEC.loader
CANARY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CANARY
SPEC.loader.exec_module(CANARY)


def _view(text: str) -> dict[str, Any]:
    return {
        "ok": True,
        "text": text,
        "active_surface": "A",
        "surface_count": 1,
        "element_count": 2,
        "returned_elements": 2,
        "next_cursor": None,
    }


class FakeEnvHandler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else None

    def _send(self, value: dict[str, Any]) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self.calls.append(("POST", self.path, body))
        if self.path == "/episodes":
            self._send({
                "episode_id": "ep_simple_test",
                "runtime": "semantic-simple-v1",
                "screenshots_captured": 0,
            })
        elif self.path.endswith("/simple/read"):
            self._send(_view('COMPUTER\n\nSurfaces\n[A] Browser — active\n\nActive Surface [A]\n[A1] button "Search" click'))
        elif self.path.endswith("/simple/click"):
            self._send(_view('Clicked [A1] "Search".\nExecution: accessibility\n\nCOMPUTER\n[A2] textbox "Search" type=replace'))
        elif self.path.endswith("/simple/type"):
            self._send(_view('Typed 5 characters into [A2] "Search" (replace).\nCOMPUTER\n[A2] textbox value="hello"'))
        elif self.path.endswith("/evaluate"):
            self._send({"score": 1, "steps": 3})
        else:
            self.send_error(404)

    def do_GET(self) -> None:  # noqa: N802
        self.calls.append(("GET", self.path, None))
        if self.path.endswith("/semantic/state"):
            self._send({field: 0 for field in CANARY.ZERO_IMAGE_FIELDS})
        else:
            self.send_error(404)

    def do_DELETE(self) -> None:  # noqa: N802
        self.calls.append(("DELETE", self.path, None))
        self._send({"errors": []})


class SemanticSimpleCanaryTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeEnvHandler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeEnvHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_full_trajectory_writes_exact_review_bundle_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "trajectory.json"
            fixture.write_text(json.dumps({
                "name": "Simple form",
                "task_path": "/guest/task.json",
                "steps": [
                    {"op": "read", "query": "Search", "expect": {"contains": "Search"}},
                    {"op": "click", "match": {"contains": "button \"Search\""}},
                    {"op": "type", "match": {"contains": "textbox \"Search\""}, "text": "hello"},
                ],
            }), encoding="utf-8")
            client = CANARY.SimpleCanaryClient(
                f"http://127.0.0.1:{self.server.server_port}", request_timeout=2,
            )
            bundle = CANARY.run_trajectory(fixture, root / "output", client)
            self.assertEqual(bundle["status"], "passed", bundle["failures"])
            self.assertEqual(bundle["model_calls"], 0)
            self.assertTrue(bundle["zero_image"]["pass"])
            self.assertEqual(bundle["evaluation"]["status"], "skipped")
            self.assertIn("interaction surface", bundle["evaluation"]["reason"])
            rendered = root / "output" / "simple-form" / "rendered-text" / "001-read.txt"
            self.assertEqual(rendered.read_text(encoding="utf-8"), bundle["steps"][0]["response"]["text"])
            self.assertTrue((root / "output" / "simple-form" / "bundle.json").is_file())
            self.assertTrue((root / "output" / "simple-form" / "human-review.md").is_file())
            paths = [(method, path) for method, path, _body in FakeEnvHandler.calls]
            self.assertEqual(paths[0], ("POST", "/episodes"))
            self.assertIn(("GET", "/episodes/ep_simple_test/semantic/state"), paths)
            self.assertNotIn(("POST", "/episodes/ep_simple_test/evaluate"), paths)
            self.assertEqual(paths[-1], ("DELETE", "/episodes/ep_simple_test"))
            create_body = FakeEnvHandler.calls[0][2]
            self.assertEqual(create_body["runtime"], "semantic-simple-v1")
            self.assertFalse(create_body["require_screenshot"])
            self.assertFalse(create_body["initial_observation"])
            simple_calls = [
                (path, body) for method, path, body in FakeEnvHandler.calls
                if method == "POST" and "/simple/" in path
            ]
            self.assertEqual(simple_calls[1][1], {"element": "A1"})
            self.assertEqual(simple_calls[2][1], {"element": "A2", "text": "hello"})
            self.assertEqual(bundle["steps"][1]["resolution"], {
                "contains": 'button "Search"',
                "line": '[A1] button "Search" click',
                "element": "A1",
            })
            self.assertEqual(bundle["steps"][2]["resolution"]["element"], "A2")

    def test_evaluator_requires_explicit_opt_in_and_is_labeled_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = root / "trajectory.json"
            fixture.write_text(json.dumps({
                "name": "Evaluator opt in",
                "task_path": "/guest/task.json",
                "steps": [{"op": "read", "expect": {"contains": "Search"}}],
            }), encoding="utf-8")
            client = CANARY.SimpleCanaryClient(
                f"http://127.0.0.1:{self.server.server_port}", request_timeout=2,
            )
            bundle = CANARY.run_trajectory(
                fixture, root / "output", client, run_evaluator=True,
            )
            self.assertEqual(bundle["status"], "passed", bundle["failures"])
            self.assertEqual(bundle["evaluation"], {
                "status": "completed",
                "result": {"score": 1, "steps": 3},
            })
            paths = [(method, path) for method, path, _body in FakeEnvHandler.calls]
            self.assertEqual(paths[-2], ("POST", "/episodes/ep_simple_test/evaluate"))
            self.assertEqual(paths[-1], ("DELETE", "/episodes/ep_simple_test"))

    def test_parser_does_not_enable_evaluator_by_default(self) -> None:
        default = CANARY.build_parser().parse_args([
            "--base-url", "http://127.0.0.1:8079",
            "--trajectory", "fixture.json",
            "--output", "output",
        ])
        self.assertFalse(default.evaluate)
        opted_in = CANARY.build_parser().parse_args([
            "--base-url", "http://127.0.0.1:8079",
            "--trajectory", "fixture.json",
            "--output", "output",
            "--evaluate",
        ])
        self.assertTrue(opted_in.evaluate)

    def test_dynamic_resolution_is_literal_immediate_and_exactly_one(self) -> None:
        rendered = (
            "COMPUTER\n[A] Chrome — active\n"
            "[A1] button \"Save\" click\n[A2] textbox \"Name\" click type=replace"
        )
        self.assertEqual(
            CANARY.resolve_rendered_capability(rendered, 'textbox "Name"'),
            {
                "contains": 'textbox "Name"',
                "line": '[A2] textbox "Name" click type=replace',
                "element": "A2",
            },
        )
        with self.assertRaisesRegex(CANARY.CanaryFailure, "found zero"):
            CANARY.resolve_rendered_capability(rendered, "name")  # Literal and case-sensitive.
        with self.assertRaisesRegex(CANARY.CanaryFailure, "ambiguous"):
            CANARY.resolve_rendered_capability(
                '[A1] button "Save" click\n[A2] button "Save as" click', "button",
            )
        with self.assertRaisesRegex(CANARY.CanaryFailure, "immediately prior"):
            CANARY.resolve_rendered_capability(None, "Save")

    def test_exact_surface_match_does_not_click_same_named_file_element(self) -> None:
        rendered = (
            "COMPUTER\n\nSurfaces\n"
            "[A] org.gnome.Nautilus — exam — active\n"
            "[C] soffice — grades.xlsx - LibreOffice Calc\n\n"
            "Active Surface [A] org.gnome.Nautilus — exam — active\n"
            "[A48] canvas \"grades.xlsx\" disabled click"
        )
        self.assertEqual(
            CANARY.resolve_rendered_capability(
                rendered, "soffice — grades.xlsx - LibreOffice Calc",
            )["element"],
            "C",
        )
        with self.assertRaisesRegex(CANARY.CanaryFailure, "ambiguous"):
            CANARY.resolve_rendered_capability(rendered, "grades.xlsx")

    def test_read_steps_reject_http_only_limit_not_available_to_model(self) -> None:
        with self.assertRaisesRegex(CANARY.CanaryFailure, "non-public fields"):
            CANARY._step_payload(
                {"op": "read", "query": "Save", "limit": 200}, None,
            )
        operation, payload, resolution = CANARY._step_payload(
            {"op": "read", "query": "Save"}, None,
        )
        self.assertEqual(operation, "read")
        self.assertEqual(payload, {"query": "Save"})
        self.assertIsNone(resolution)

    def test_expectations_bind_app_name_to_active_header_not_surface_list(self) -> None:
        wrong_active = _view(
            "COMPUTER\n\nSurfaces\n[A] gnome-shell — active\n"
            "[B] Thunderbird — Inbox\n\nActive Surface [A] gnome-shell — active\n"
            "[A1] button \"Activities\" click"
        )
        failures = CANARY.check_expectations(wrong_active, {
            "contains": "Thunderbird",
            "active_header_contains": "Thunderbird",
            "active_header_not_contains": "gnome-shell",
            "min_returned_elements": 1,
        })
        self.assertEqual(len(failures), 2)
        self.assertIn("does not contain 'Thunderbird'", failures[0])
        self.assertIn("unexpectedly contains 'gnome-shell'", failures[1])

        correct_active = dict(wrong_active)
        correct_active["text"] = (
            "COMPUTER\n\nSurfaces\n[A] gnome-shell\n"
            "[B] Thunderbird — Inbox — active\n\n"
            "Active Surface [B] Thunderbird — Inbox — active\n"
            "[B1] tree item \"Inbox\" click"
        )
        correct_active["active_surface"] = "B"
        self.assertEqual(CANARY.check_expectations(correct_active, {
            "active_header_contains": ["Thunderbird", "Inbox"],
            "active_header_not_contains": "gnome-shell",
            "min_returned_elements": 1,
        }), [])

    def test_quality_audit_detects_all_requested_failure_classes(self) -> None:
        audit = CANARY.audit_rendered_text(
            "adapter_id=guest-os@1\nresource=os.windows\nref=ref_opaque\nDUP\nDUP"
        )
        self.assertEqual(audit["duplicate_lines"], [{"line": "DUP", "count": 2}])
        self.assertGreaterEqual(len(audit["forbidden_jargon"]), 3)
        self.assertFalse(audit["empty"])
        self.assertTrue(CANARY.audit_rendered_text("")["empty"])
        self.assertTrue(CANARY.audit_rendered_text("x" * 10_001)["oversized"])

    def test_public_id_stability_catches_surface_and_element_rebinding(self) -> None:
        before = (
            "Surfaces\n[C] Chrome — Example — active\n"
            "Active Surface [C] Chrome — Example — active\n"
            "[C1] button \"Save\" click\n[C2] text text=\"unchanged\""
        )
        stable = CANARY.audit_public_id_stability(
            before,
            "Surfaces\n[C] Chrome — Example — active\n"
            "Active Surface [C] Chrome — Example — active\n"
            "[C1] button \"Save\" click",
        )
        self.assertTrue(stable["pass"])
        self.assertEqual(stable["comparable_rows"], 2)

        churned = CANARY.audit_public_id_stability(
            before,
            "Surfaces\n[D] Chrome — Example — active\n"
            "Active Surface [D] Chrome — Example — active\n"
            "[D9] button \"Save\" click",
        )
        self.assertFalse(churned["pass"])
        self.assertEqual(churned["mismatches"], [
            {"label": "Chrome — Example", "before": "C", "after": "D"},
            {"label": 'button "Save" click', "before": "C1", "after": "D9"},
        ])


if __name__ == "__main__":
    unittest.main()
