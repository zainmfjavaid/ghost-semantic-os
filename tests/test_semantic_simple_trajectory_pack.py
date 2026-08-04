#!/usr/bin/env python3
"""Static safety/coverage audit for semantic-simple real-VM fixtures."""

from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "infra" / "semantic_simple_trajectories"
REQUIRED_COVERAGE = {
    "chrome", "webpage", "web-form", "select", "iframe", "chrome-settings",
    "gnome", "settings", "dialog", "file-chooser", "writer", "calc", "impress",
    "thunderbird", "vscode", "vlc", "pdf", "evince", "terminal", "gimp", "multi-app",
    "public-e2e",
}
EXPECTED_ZERO_IMAGE_FIELDS = {
    "screenshots_captured", "image_parts_created", "image_parts_in_session",
    "image_parts_sent", "pixels_sent_to_policy_model", "visual_sidecar_calls",
}
PUBLIC_STEP_FIELDS = {
    "read": {"op", "query", "within", "cursor", "expect"},
    "click": {"op", "match", "expect"},
    "type": {"op", "match", "text", "expect"},
}
REQUIRED_ACTION_COUNTS = {
    "02-chrome-form.json": {"click": 1, "type": 1},
    "09-writer.json": {"type": 2},
    "10-calc.json": {"type": 3},
    "14-vlc.json": {"click": 1, "type": 1},
    "18-multi-app.json": {"click": 4},
    "19-public-cross-app-e2e.json": {"click": 7, "type": 2},
}


class SemanticSimpleTrajectoryPackTest(unittest.TestCase):
    def test_pack_has_broad_existing_osworld_setups_and_safe_operations(self) -> None:
        fixtures = sorted(PACK.glob("[0-9][0-9]-*.json"))
        self.assertEqual(len(fixtures), 19)
        coverage: set[str] = set()
        task_paths: set[str] = set()
        for fixture_path in fixtures:
            fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
            self.assertIsInstance(fixture.get("name"), str, fixture_path)
            task_path = fixture.get("task_path")
            self.assertIsInstance(task_path, str, fixture_path)
            self.assertTrue((ROOT / task_path).is_file(), task_path)
            self.assertNotIn(task_path, task_paths, task_path)
            task_paths.add(task_path)
            coverage.update(fixture.get("coverage") or [])
            constraints = fixture.get("quality_constraints") or {}
            self.assertEqual(constraints.get("min_chars"), 1, fixture_path)
            self.assertEqual(constraints.get("max_chars"), 10_000, fixture_path)
            self.assertEqual(constraints.get("max_estimated_tokens"), 2_500, fixture_path)
            self.assertEqual(constraints.get("max_duplicate_lines"), 0, fixture_path)
            self.assertIs(constraints.get("forbid_protocol_jargon"), True, fixture_path)
            self.assertIs(constraints.get("zero_images"), True, fixture_path)
            steps = fixture.get("steps")
            self.assertIsInstance(steps, list, fixture_path)
            self.assertGreaterEqual(len(steps), 2, fixture_path)
            for index, step in enumerate(steps):
                operation = step.get("op")
                self.assertIn(operation, PUBLIC_STEP_FIELDS, fixture_path)
                self.assertLessEqual(
                    set(step), PUBLIC_STEP_FIELDS.get(operation, set()),
                    f"{fixture_path}: step {index + 1} has a non-public field",
                )
                self.assertNotIn("model", step, fixture_path)
                self.assertNotIn("selector", step, fixture_path)
                self.assertNotIn("coordinate", step, fixture_path)
                self.assertNotIn("key", step, fixture_path)
                self.assertNotIn("resource", step, fixture_path)
                self.assertNotIn("ref", step, fixture_path)
                if operation in {"click", "type"}:
                    self.assertGreater(index, 0, fixture_path)
                    self.assertEqual(
                        steps[index - 1].get("op"), "read",
                        f"{fixture_path}: action step {index + 1} must use the immediately prior read",
                    )
                    self.assertNotIn("element", step, fixture_path)
                    self.assertIsInstance((step.get("match") or {}).get("contains"), str)
                if operation == "type":
                    self.assertIsInstance(step.get("text"), str, fixture_path)
                expected = step.get("expect") or {}
                self.assertIn("COMPUTER", expected.get("contains", []), fixture_path)
                self.assertGreaterEqual(expected.get("min_surface_count", 0), 1, fixture_path)
                if index == 0:
                    if fixture.get("startup_active_surface_nondeterministic") is True:
                        self.assertIn("active_header_not_contains", expected, fixture_path)
                    else:
                        self.assertIn("active_header_contains", expected, fixture_path)
                    self.assertGreaterEqual(expected.get("min_returned_elements", 0), 1, fixture_path)
        self.assertTrue(REQUIRED_COVERAGE.issubset(coverage), REQUIRED_COVERAGE - coverage)

    def test_public_action_trajectories_cover_major_mutation_routes(self) -> None:
        fixtures = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in PACK.glob("[0-9][0-9]-*.json")
        }
        for fixture_name, expected_counts in REQUIRED_ACTION_COUNTS.items():
            operations = [step["op"] for step in fixtures[fixture_name]["steps"]]
            for operation, expected_count in expected_counts.items():
                self.assertEqual(
                    operations.count(operation), expected_count,
                    f"{fixture_name}: {operation}",
                )

        calc_types = [
            step["text"] for step in fixtures["10-calc.json"]["steps"]
            if step["op"] == "type"
        ]
        self.assertEqual(calc_types, ["42", "=SUM(E3:E4)", "1\t2\n3\t4"])

        surface_matches = [
            step["match"]["contains"]
            for step in fixtures["18-multi-app.json"]["steps"]
            if step["op"] == "click"
        ]
        self.assertEqual(surface_matches, [
            "— exam",
            "grades.xlsx",
            "ReferenceAnswers.docx",
            "— exam",
        ])

        writer = fixtures["09-writer.json"]
        self.assertNotIn("read_only_gaps", writer)
        self.assertIn("writer-insert", writer["coverage"])
        self.assertTrue(any(
            step.get("op") == "type"
            and step.get("match", {}).get("contains") == 'input "Document end"'
            for step in writer["steps"]
        ))

    def test_cross_app_e2e_is_public_model_free_and_exactly_sequenced(self) -> None:
        fixture_path = PACK / "19-public-cross-app-e2e.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        task = json.loads((ROOT / fixture["task_path"]).read_text(encoding="utf-8"))

        self.assertEqual([step["op"] for step in fixture["steps"]], [
            "read", "click", "read", "type", "read", "click", "read", "click",
            "read", "click", "read", "click", "read", "click", "read",
            "click", "read", "type", "read",
        ])
        self.assertEqual([
            step.get("match", {}).get("contains")
            for step in fixture["steps"]
            if step["op"] in {"click", "type"}
        ], [
            "Chrome — Semantic Public Journey",
            'textbox "Message" type=replace',
            'button "Apply message" click',
            "LibreOffice Writer — Untitled",
            "[A] Desktop",
            'application "Settings" click',
            "Chrome — Semantic Public Journey",
            'button "Attach fixture file"',
            'input "Choose exact guest path" type=replace',
        ])
        self.assertEqual(
            [step["text"] for step in fixture["steps"] if step["op"] == "type"],
            ["semantic canary", "/home/user/share/semantic-upload.txt"],
        )

        self.assertEqual(
            {entry["type"] for entry in task["config"]},
            {"execute", "launch", "sleep", "activate_window"},
        )
        self.assertEqual(task.get("evaluator"), {"func": "infeasible"})
        self.assertNotIn("expected", task)
        self.assertNotIn("result", task)
        serialized_setup = json.dumps(task["config"])
        self.assertIn("semantic-public-journey.html", serialized_setup)
        self.assertIn("semantic-upload.txt", serialized_setup)
        self.assertIn("libreoffice", serialized_setup)
        self.assertIn("google-chrome", serialized_setup)

    def test_dialog_fixtures_launch_real_gtk_and_act_only_from_prior_render(self) -> None:
        cases = {
            "07-gnome-dialog.json": ("--question", "click"),
            "08-gnome-file-chooser.json": ("--file-selection", "read"),
        }
        for filename, (zenity_mode, action_name) in cases.items():
            fixture = json.loads(PACK.joinpath(filename).read_text(encoding="utf-8"))
            self.assertTrue(
                fixture["task_path"].startswith("infra/semantic_simple_trajectories/tasks/"),
                fixture["task_path"],
            )
            task = json.loads(
                ROOT.joinpath(fixture["task_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(task["snapshot"], "os")
            self.assertEqual(task["related_apps"], ["os"])
            self.assertEqual(task["evaluator"], {"func": "infeasible"})
            self.assertNotIn("expected", task["evaluator"])
            self.assertNotIn("result", task["evaluator"])
            launches = [
                operation for operation in task["config"]
                if operation.get("type") == "launch"
            ]
            self.assertEqual(len(launches), 1)
            command = launches[0]["parameters"]["command"]
            self.assertIsInstance(command, list)
            self.assertEqual(command[0], "zenity")
            self.assertIn(zenity_mode, command)

            self.assertEqual(fixture["steps"][0]["op"], "read")
            self.assertEqual(fixture["steps"][1]["op"], action_name)
            if action_name == "read":
                continue
            action = fixture["steps"][1]
            self.assertNotIn("element", action)
            literal = action["match"]["contains"]
            self.assertTrue(literal)
            preceding_expectations = fixture["steps"][0]["expect"]["contains"]
            if isinstance(preceding_expectations, str):
                preceding_expectations = [preceding_expectations]
            self.assertTrue(
                any(
                    expected in literal or literal in expected
                    for expected in preceding_expectations
                ),
                f"{filename} action match is not grounded in its prior public read",
            )

        chooser = json.loads(
            PACK.joinpath("08-gnome-file-chooser.json").read_text(encoding="utf-8")
        )
        self.assertEqual(chooser["steps"][1]["op"], "read")
        self.assertEqual(chooser["steps"][1]["query"], "button")
        self.assertNotIn("Choose exact guest path", json.dumps(chooser["steps"]))
        gaps = chooser.get("read_only_gaps")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["status"], "representation_gap")

    def test_readme_documents_all_tasks_and_zero_image_contract(self) -> None:
        readme = PACK.joinpath("README.md").read_text(encoding="utf-8")
        for fixture in sorted(PACK.glob("[0-9][0-9]-*.json")):
            self.assertIn(f"`{fixture.name}`", readme)
        for field in EXPECTED_ZERO_IMAGE_FIELDS:
            # The README intentionally groups the counters in prose while the
            # runner owns exact field names. Verify that contract at source.
            runner = ROOT.joinpath("infra/gcp_semantic_simple_canary.py").read_text(
                encoding="utf-8"
            )
            self.assertIn(f'"{field}"', runner)
        self.assertIn("model-free", readme)
        self.assertIn("No fixture calls a model", readme)


if __name__ == "__main__":
    unittest.main()
