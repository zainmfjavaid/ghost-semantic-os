from __future__ import annotations

import json
import unittest
from pathlib import Path

from infra.audit_semantic_application_coverage import build


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "OSWorld" / "protocol" / "linux-app-inventory.json"
COVERAGE = ROOT / "protocol" / "semantic-v1-application-coverage.json"


class ApplicationCoverageTests(unittest.TestCase):
    def test_all_369_linux_tasks_map_to_implemented_adapter_or_typed_gap(self) -> None:
        inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
        coverage = build(INVENTORY)
        self.assertEqual(len(inventory["tasks"]), 369)
        self.assertEqual(coverage["inventory_task_count"], 369)
        self.assertEqual(coverage["summary"]["missing_families"], 0)
        self.assertEqual(coverage["summary"]["family_count"], 15)
        for family in coverage["families"]:
            self.assertIn(family["disposition"], {"implemented", "representation_gap"})
            self.assertTrue(family["adapters"])

    def test_checked_coverage_artifact_matches_live_descriptors(self) -> None:
        expected = json.dumps(build(INVENTORY), indent=2, sort_keys=True) + "\n"
        self.assertEqual(COVERAGE.read_text(encoding="utf-8"), expected)


if __name__ == "__main__":
    unittest.main()
