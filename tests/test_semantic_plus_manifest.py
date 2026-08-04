"""The semantic-plus manifest uses the same semantic runtime identity inputs."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import hashlib


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_semantic_runtime_manifest",
    ROOT / "infra" / "build_semantic_runtime_manifest.py",
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SemanticPlusManifestTests(unittest.TestCase):
    def test_semantic_plus_is_a_semantic_runtime(self) -> None:
        self.assertIn("semantic-v1", MODULE.SEMANTIC_RUNTIMES)
        self.assertIn("semantic-plus-v1", MODULE.SEMANTIC_RUNTIMES)
        self.assertIn("semantic-simple-v1", MODULE.SEMANTIC_RUNTIMES)
        self.assertTrue(MODULE.SEMANTIC_RUNTIMES <= MODULE.RUNTIMES)

    def test_semantic_plus_manifest_has_semantic_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_config = Path(temporary) / "model.json"
            model_config.write_text(
                '{"provider":"test","model":"text-only","input":["text"]}',
                encoding="utf-8",
            )
            args = SimpleNamespace(
                runtime="semantic-plus-v1",
                source_root=str(ROOT),
                environment_root=str(ROOT),
                task_pool=None,
                task_pool_sha256=None,
                task_pool_name=None,
                model_config=str(model_config),
                parent_commit="a" * 40,
                nested_osworld_commit="b" * 40,
                parent_branch=None,
                nested_osworld_branch=None,
            )
            manifest = MODULE.build(args)

        self.assertEqual(manifest["runtime"], "semantic-plus-v1")
        self.assertEqual(manifest["semantic_protocol_version"], "1.0")
        self.assertEqual(manifest["parent_branch"], "feature/semantic-os-v1")
        self.assertEqual(manifest["nested_osworld_branch"], "ghost/semantic-os-v1")
        self.assertIsNotNone(manifest["guest_bundle_sha256"])
        self.assertIn("integration_files_sha256", manifest["application_integrations"])
        self.assertEqual(
            manifest["browser_extension"]["status"],
            "not_installed_native_routes_used",
        )
        self.assertEqual(manifest["system_prompt"], {"status": "not_applicable"})

    def test_semantic_simple_manifest_includes_facade_and_text_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_config = Path(temporary) / "model.json"
            model_config.write_text(
                '{"provider":"test","model":"text-only","input":["text"]}',
                encoding="utf-8",
            )
            args = SimpleNamespace(
                runtime="semantic-simple-v1",
                source_root=str(ROOT),
                environment_root=str(ROOT),
                task_pool=None,
                task_pool_sha256=None,
                task_pool_name=None,
                model_config=str(model_config),
                parent_commit="a" * 40,
                nested_osworld_commit="b" * 40,
                parent_branch=None,
                nested_osworld_branch=None,
            )
            manifest = MODULE.build(args)

        self.assertEqual(manifest["runtime"], "semantic-simple-v1")
        self.assertEqual(manifest["semantic_protocol_version"], "1.0")
        self.assertIn("envserver/semantic/simple_facade.py", manifest["runtime_files"])
        self.assertEqual(manifest["model_endpoint"]["input"], ["text"])
        prompt = ROOT / "harness/prompts/semantic-simple-v1.4.txt"
        self.assertEqual(manifest["system_prompt"], {
            "version": "1.2",
            "source": "harness/prompts/semantic-simple-v1.4.txt",
            "sha256": hashlib.sha256(prompt.read_bytes()).hexdigest(),
        })
        self.assertEqual(
            manifest["runtime_files"]["harness/prompts/semantic-simple-v1.4.txt"],
            manifest["system_prompt"]["sha256"],
        )


if __name__ == "__main__":
    unittest.main()
