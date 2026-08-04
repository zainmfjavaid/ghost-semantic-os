from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from guest_agent import semantic_agent


class GuestArchiveTests(unittest.TestCase):
    def test_extract_archive_is_bounded_and_preserves_structure(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "extension.zip"
            destination = root / "desktop"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("extension/manifest.json", '{"manifest_version":3}')
            result = semantic_agent.act({
                "action": "extract_archive",
                "arguments": {"source": str(source), "destination": str(destination)},
            })
            self.assertEqual(result["member_count"], 1)
            self.assertTrue((destination / "extension" / "manifest.json").is_file())

    def test_extract_archive_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "unsafe.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("../escape", "no")
            with self.assertRaises(semantic_agent.AgentError) as raised:
                semantic_agent.act({
                    "action": "extract_archive",
                    "arguments": {
                        "source": str(source),
                        "destination": str(root / "destination"),
                    },
                })
            self.assertEqual(raised.exception.code, "policy_violation")
            self.assertFalse((root / "escape").exists())


if __name__ == "__main__":
    unittest.main()
