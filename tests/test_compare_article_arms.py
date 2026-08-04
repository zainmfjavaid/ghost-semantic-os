from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "infra" / "compare_article_arms.py"
SPEC = importlib.util.spec_from_file_location("compare_article_arms", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def episode(task_id: str, score: float, calls: int, *, semantic: bool = False):
    row = {
        "taskId": task_id,
        "score": score,
        "toolCalls": calls,
        "toolAttempts": calls,
        "tokensTotal": calls * 100,
        "tokensInput": calls * 80,
        "tokensOutput": calls * 20,
        "costUsd": calls / 100,
        "elapsedMs": calls * 1_000,
        "stopReason": "task_complete" if score else "step_limit",
    }
    if semantic:
        row["semanticPolicy"] = {
            key: 0 for key in MODULE.IMAGE_COUNTERS
        } | {"semanticOperations": calls}
    return row


def result(rows, *, mode: str, max_calls: int = 40):
    return {
        "payloads": [],
        "rows_by_id": {row["taskId"]: [row] for row in rows},
        "duplicate_ids": [],
        "max_tool_calls": max_calls,
        "modes": [mode],
        "models": [mode],
    }


def audit(mode: str, *, invalid=None):
    invalid = invalid or []
    counters = {key: 0 for key in MODULE.IMAGE_COUNTERS} if mode == "semantic-v1" else None
    return {
        "mode": mode,
        "publishable": not invalid,
        "arm_issues": ["invalid_or_incomplete_episodes"] if invalid else [],
        "invalid_or_incomplete_episodes": invalid,
        "missing_task_ids": [],
        "duplicate_task_ids": [],
        "zero_image_counters_all_attempted": counters,
        "wall_time": {},
    }


class CompareArticleArmsTest(unittest.TestCase):
    def test_paired_denominator_excludes_invalid_from_either_arm(self):
        ids = ["a", "69ac", "c"]
        metadata = {
            "a": {"difficulty": "one_app", "apps": ["chrome"]},
            "69ac": {
                "difficulty": "three_app",
                "apps": ["chrome", "terminal", "code"],
            },
            "c": {"difficulty": "two_app", "apps": ["chrome", "writer"]},
        }
        left = result([
            episode("a", 0, 40),
            episode("69ac", 0, 40),
            episode("c", 1, 10),
        ], mode="hybrid-v15")
        right = result([
            episode("a", 1, 8, semantic=True),
            episode("69ac", 1, 4, semantic=True),
            episode("c", 1, 6, semantic=True),
        ], mode="semantic-v1")
        report = MODULE.paired_comparison(
            left_results=left,
            left_audit=audit("hybrid-v15", invalid=[{
                "task_id": "69ac", "reasons": ["evaluationError"]
            }]),
            right_results=right,
            right_audit=audit("semantic-v1"),
            expected_ids=ids,
            metadata=metadata,
            left_label="baseline",
            right_label="semantic",
            pool_sha256="pool",
        )
        self.assertEqual(report["planned_denominator"], 3)
        self.assertEqual(report["paired_valid_denominator"], 2)
        self.assertFalse(
            report["comparison_publishable"],
            "paired exclusion must not override a non-publishable arm audit",
        )
        self.assertEqual(report["arm_audit_publishable"], {
            "baseline": False,
            "semantic": True,
        })
        self.assertEqual(report["excluded_from_pair"], [{
            "task_id": "69ac",
            "baseline": ["evaluationError"],
            "semantic": [],
        }])
        self.assertEqual(
            report["arms"]["baseline"]["raw"]["score"]["score_total"], 1
        )
        self.assertEqual(
            report["arms"]["semantic"]["raw"]["score"]["score_total"], 3
        )
        self.assertEqual(
            report["arms"]["baseline"]["paired_valid"]["score"]["score_rate"],
            0.5,
        )
        self.assertEqual(
            report["arms"]["semantic"]["paired_valid"]["score"]["score_rate"],
            1,
        )
        self.assertEqual(report["paired_score_rate_delta"], 0.5)
        self.assertEqual(report["difficulty"]["three_app"]["paired_valid"], 0)
        self.assertEqual(
            report["arms"]["semantic"]["zero_image_all_observed"]["proof"],
            "pass",
        )

    def test_publishable_requires_both_single_arm_audits_to_pass(self):
        ids = ["a"]
        metadata = {"a": {"difficulty": "one_app", "apps": ["chrome"]}}
        left = result([episode("a", 1, 1)], mode="hybrid-v15")
        right = result([episode("a", 1, 1, semantic=True)], mode="semantic-v1")

        report = MODULE.paired_comparison(
            left_results=left,
            left_audit=audit("hybrid-v15"),
            right_results=right,
            right_audit=audit("semantic-v1"),
            expected_ids=ids,
            metadata=metadata,
            left_label="baseline",
            right_label="semantic",
            pool_sha256="pool",
        )
        self.assertTrue(report["comparison_publishable"])

        right_not_publishable = audit("semantic-v1")
        right_not_publishable["publishable"] = False
        failed = MODULE.paired_comparison(
            left_results=left,
            left_audit=audit("hybrid-v15"),
            right_results=right,
            right_audit=right_not_publishable,
            expected_ids=ids,
            metadata=metadata,
            left_label="baseline",
            right_label="semantic",
            pool_sha256="pool",
        )
        self.assertEqual(failed["paired_valid_denominator"], 1)
        self.assertFalse(failed["comparison_publishable"])
        self.assertEqual(failed["arm_audit_publishable"], {
            "baseline": True,
            "semantic": False,
        })

    def test_missing_semantic_counter_fails_zero_image_proof(self):
        rows = [episode("a", 1, 1, semantic=True)]
        del rows[0]["semanticPolicy"]["pixelsSentToPolicyModel"]
        proof = MODULE.zero_image_summary(
            rows, mode="semantic-v1", audit=audit("semantic-v1")
        )
        self.assertEqual(proof["proof"], "fail")
        self.assertEqual(proof["missing_episode_counters"], {
            "a": ["pixelsSentToPolicyModel"]
        })

    def test_usage_reports_budget_tokens_cost_and_calls(self):
        summary = MODULE.usage_summary(
            [episode("a", 1, 40), episode("b", 0, 10)], max_tool_calls=40
        )
        self.assertEqual(summary["tool_calls_total"], 50)
        self.assertEqual(summary["tool_calls_mean"], 25)
        self.assertEqual(summary["budget_hits"], 1)
        self.assertEqual(summary["tokens_total"], 5_000)
        self.assertEqual(summary["cost_usd"], 0.5)


if __name__ == "__main__":
    unittest.main()
