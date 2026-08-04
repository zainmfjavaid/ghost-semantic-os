from __future__ import annotations

import json
import unittest
from pathlib import Path

from envserver.semantic.models import (
    SemanticRequest,
    SemanticResponse,
    load_canonical_schema,
)
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "protocol" / "fixtures" / "semantic-v1-conformance.json"


class SchemaParityTests(unittest.TestCase):
    def test_pydantic_accepts_and_rejects_shared_fixtures(self):
        fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
        for value in fixtures["valid"]:
            model = SemanticRequest if "operation" in value else SemanticResponse
            model.model_validate(value)
        for value in fixtures["invalid"]:
            with self.assertRaises((ValidationError, ValueError)):
                SemanticRequest.model_validate(value)

    def test_generated_schema_is_checked_2020_12_artifact(self):
        schema = load_canonical_schema()
        self.assertEqual(
            schema["$id"], "https://ghost.ai/protocol/semantic-v1.schema.json"
        )

    def test_jsonschema_accepts_and_rejects_same_fixtures_when_installed(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is installed by infra/gcp_setup.sh")
        validator = Draft202012Validator(load_canonical_schema())
        fixtures = json.loads(FIXTURES.read_text(encoding="utf-8"))
        for value in fixtures["valid"]:
            self.assertEqual(list(validator.iter_errors(value)), [])
        for value in fixtures["invalid"]:
            self.assertTrue(list(validator.iter_errors(value)))


if __name__ == "__main__":
    unittest.main()
