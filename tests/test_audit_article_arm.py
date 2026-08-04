"""Regression tests for article-audit trace and identity parsing."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from infra.audit_article_arm import (
    episode_issues,
    log_wall_metrics,
    parse_result,
    payload_identity_issues,
)


class ArticleAuditParsingTests(unittest.TestCase):
    @staticmethod
    def payload(*, concurrency: int, model: str = "qwen/model"):
        return {
            "harnessRevision": "harness-1",
            "provider": "openrouter",
            "model": model,
            "variant": "semantic-rc4",
            "thinkingLevel": "medium",
            "maxToolCalls": 100,
            "concurrency": concurrency,
            "som": False,
            "semanticDesktop": False,
            "visionOnly": False,
            "web": False,
            "webTextOnly": False,
            "webFirst": False,
            "verifyGate": False,
            "noDesktop": False,
            "compactWeb": False,
            "browserPrompt": False,
            "codeFirst": False,
            "budgetHints": False,
            "runtime": "semantic-v1",
            "semanticProtocolVersion": "1.0",
            "parentCommit": "parent-1",
            "nestedOSWorldCommit": "osworld-1",
            "runtimeManifestSha256": "manifest-1",
            "runtimeFilesSha256": "runtime-1",
            "taskPoolSha256": "pool-1",
        }

    def test_shard_concurrency_may_differ_without_identity_failure(self):
        issues = payload_identity_issues([
            self.payload(concurrency=6),
            self.payload(concurrency=5),
        ], mode="semantic-v1")
        self.assertEqual(issues, [])

    def test_real_cross_shard_identity_disagreement_still_fails(self):
        issues = payload_identity_issues([
            self.payload(concurrency=6),
            self.payload(concurrency=5, model="another/model"),
        ], mode="semantic-v1")
        self.assertEqual(issues, ["payload_disagreement:model"])

    def test_invalid_shard_concurrency_still_fails(self):
        issues = payload_identity_issues([
            self.payload(concurrency=6),
            self.payload(concurrency=0),
        ], mode="semantic-v1")
        self.assertEqual(issues, ["invalid_shard_concurrency"])

    def test_typed_result_allows_private_details_trailer(self):
        result = parse_result(
            '{"protocol_version":"1.0","status":"ok"}'
            '\nDETAILS: {"semantic":{"resource":"browser.tabs"}}'
        )
        self.assertEqual(
            result,
            {"protocol_version": "1.0", "status": "ok"},
        )

    def test_invalid_or_non_object_result_is_rejected(self):
        self.assertIsNone(parse_result("not-json"))
        self.assertIsNone(parse_result("[]"))

    def test_wall_metric_reads_completion_line_before_results_path(self):
        with TemporaryDirectory() as raw:
            arm = Path(raw)
            (arm / "host.log").write_text(
                "variant: 2/17 in 58.4 min\nResults: /tmp/result\n",
                encoding="utf-8",
            )
            metrics = log_wall_metrics(arm)
        self.assertEqual(metrics["critical_path_minutes_approx"], 58.4)

    def test_strict_semantic_framework_error_is_an_untyped_integrity_failure(self):
        episode = {
            "taskId": "task-1",
            "score": 0,
            "toolCalls": 0,
            "toolAttempts": 1,
            "elapsedMs": 1,
            "costUsd": 0,
            "runtime": "semantic-v1",
            "semanticProtocolVersion": "1.0",
            "semanticPolicy": {
                **{key: 0 for key in (
                    "screenshotsCaptured", "imagePartsCreated", "imagePartsInSession",
                    "imagePartsSent", "pixelsSentToPolicyModel", "visualSidecarCalls",
                )},
                "semanticOperations": 0,
            },
            "environmentIdentity": {
                "outer_provider": "gcp",
                "outer_vm_name": "host-1",
                "nested_guest_machine_id": "guest-1",
                "guest_platform": "linux",
                "guest_os_release_hash": "os-1",
                "guest_image_digest": "image-1",
                "display_identity": "display-1",
                "semantic_guest_bundle_hash": "bundle-1",
            },
            "trace": [{
                "kind": "tool_start", "toolName": "computer.query", "toolCallId": "call-1",
            }, {
                "kind": "tool_end", "toolName": "computer.query", "toolCallId": "call-1",
                "isError": True, "resultText": "Validation failed for tool computer.query",
            }],
        }
        issues = episode_issues(
            episode, mode="semantic-v1", max_tool_calls=100,
            duplicate_ids=set(), expected_ids={"task-1"}, source_host="host-1",
        )
        self.assertIn("untyped_semantic_tool_result", issues)


if __name__ == "__main__":
    unittest.main()
