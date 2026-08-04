"""Canonical evaluator-blind verification over semantic query results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .protocol import ErrorCode, ProtocolError
from .query import QueryPage, _MISSING, compare, get_path
from .state import ActionReconciliationReceipt, EpisodeState, VerificationReceipt


MAX_VERIFY_NODES = 512
MAX_VERIFY_DEPTH = 32
VERDICTS = frozenset({"pass", "fail", "unknown"})


@dataclass(frozen=True)
class VerificationResult:
    verification_id: str
    verdict: str
    claims: tuple[Mapping[str, Any], ...]
    dependencies: tuple[Mapping[str, Any], ...]
    evidence: tuple[Mapping[str, Any], ...]
    observed_at: str
    receipt: VerificationReceipt
    reconciliation: ActionReconciliationReceipt | None = None

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "verification_id": self.verification_id,
            "verdict": self.verdict,
            "claims": [dict(claim) for claim in self.claims],
            "dependencies": [dict(dependency) for dependency in self.dependencies],
            "evidence": [dict(entry) for entry in self.evidence],
            "observed_at": self.observed_at,
        }
        if self.reconciliation is not None:
            result["reconciliation"] = self.reconciliation.to_dict()
        return result


class _Evaluation:
    def __init__(self) -> None:
        self.nodes = 0
        self.claims: list[dict[str, Any]] = []
        self.evidence: list[dict[str, Any]] = []
        self.dependencies: list[dict[str, Any]] = []
        self.internal_dependencies: list[dict[str, Any]] = []
        self.claim_ids: set[str] = set()

    def take(self, depth: int) -> None:
        self.nodes += 1
        if self.nodes > MAX_VERIFY_NODES or depth > MAX_VERIFY_DEPTH:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED, "verification expression is too large"
            )


QueryResolver = Callable[[Mapping[str, Any]], QueryPage]


class VerificationEngine:
    """Verify user-supplied claims without evaluator or expected-answer access."""

    def verify(
        self,
        *,
        state: EpisodeState,
        adapter_id: str,
        payload: Mapping[str, Any],
        query: QueryResolver,
        consume_budget: bool = True,
    ) -> VerificationResult:
        if consume_budget:
            state.consume_operation()
        if not isinstance(payload, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "verify payload must be an object")
        mode = payload.get("mode")
        assertions = payload.get("assertions")
        freshness = payload.get("freshness")
        if set(payload) - {"mode", "assertions", "freshness", "reconcile_action"}:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "verify payload has unknown fields")
        if mode not in {"all", "any"}:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "verify mode must be all or any")
        if freshness != "live":
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "verification freshness must be live")
        if not isinstance(assertions, (list, tuple)) or not 1 <= len(assertions) <= 128:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "verify assertions must contain 1..128 entries"
            )
        reconcile = payload.get("reconcile_action")
        if "reconcile_action" in payload and reconcile is None:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "reconcile_action cannot be null"
            )
        action_receipt_id: str | None = None
        reconciliation_outcome: str | None = None
        if reconcile is not None:
            if not isinstance(reconcile, Mapping) or set(reconcile) != {
                "receipt_id", "outcome"
            }:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "reconcile_action requires exact receipt_id and outcome fields",
                )
            action_receipt_id = reconcile.get("receipt_id")
            reconciliation_outcome = reconcile.get("outcome")
            if not isinstance(action_receipt_id, str) or not action_receipt_id:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, "reconcile_action receipt_id is invalid"
                )
            if reconciliation_outcome not in {"none", "applied"}:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "reconcile_action outcome must be none or applied",
                )
            action = state.get_action(action_receipt_id)
            if action.side_effect_state.value != "unknown":
                raise ProtocolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "only an uncertain action receipt can be reconciled",
                )
        evaluation = _Evaluation()
        verdicts = [
            self._expression(
                state=state,
                query=query,
                expression=assertion,
                evaluation=evaluation,
                depth=0,
            )
            for assertion in assertions
        ]
        verdict = self._combine(mode, verdicts)
        # Preserve first-seen dependency order while removing duplicates.
        dependencies: list[dict[str, Any]] = []
        seen_dependencies: set[str] = set()
        for dependency in evaluation.dependencies:
            key = json.dumps(dependency, sort_keys=True, separators=(",", ":"))
            if key not in seen_dependencies:
                seen_dependencies.add(key)
                dependencies.append(dependency)
        # A verification may read several scoped views of one resource.  They
        # share one mutation epoch even when their query/view revisions differ.
        # Reject the whole verification if that epoch changed between leaves;
        # otherwise store one compact dependency per resource.
        internal_dependencies: list[dict[str, Any]] = []
        seen_internal: set[tuple[str, str]] = set()
        for dependency in evaluation.internal_dependencies:
            adapter_dependency = str(dependency["adapter_id"])
            resource_dependency = str(dependency["resource"])
            revision_dependency = str(dependency["revision"])
            if state.dependency_revision(
                adapter_dependency, resource_dependency
            ) != revision_dependency:
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "resource changed while verification evidence was collected",
                    retryable=True,
                )
            key = (adapter_dependency, resource_dependency)
            if key not in seen_internal:
                seen_internal.add(key)
                internal_dependencies.append(dict(dependency))
        receipt = state.record_verification(
            adapter_id=adapter_id,
            resource=None,
            revision=None,
            passed=verdict == "pass",
            assertion=payload,
            evidence={
                "verdict": verdict,
                "claims": evaluation.claims,
                "dependencies": dependencies,
                "evidence": evaluation.evidence,
                "internal_dependencies": internal_dependencies,
            },
            action_receipt_id=action_receipt_id,
        )
        reconciliation = None
        if verdict == "pass" and action_receipt_id is not None:
            reconciliation = state.reconcile_action(
                action_receipt_id=action_receipt_id,
                verification_id=receipt.receipt_id,
                outcome=str(reconciliation_outcome),
            )
        return VerificationResult(
            verification_id=receipt.receipt_id,
            verdict=verdict,
            claims=tuple(evaluation.claims),
            dependencies=tuple(dependencies),
            evidence=tuple(evaluation.evidence),
            observed_at=receipt.observed_at,
            receipt=receipt,
            reconciliation=reconciliation,
        )

    def _expression(
        self,
        *,
        state: EpisodeState,
        query: QueryResolver,
        expression: Any,
        evaluation: _Evaluation,
        depth: int,
    ) -> str:
        evaluation.take(depth)
        if not isinstance(expression, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "verify expression must be an object")
        keys = set(expression)
        if keys == {"all"} or keys == {"any"}:
            mode = next(iter(keys))
            children = expression[mode]
            if not isinstance(children, (list, tuple)) or not 1 <= len(children) <= 128:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"{mode} expression requires 1..128 children"
                )
            return self._combine(
                mode,
                [
                    self._expression(
                        state=state,
                        query=query,
                        expression=child,
                        evaluation=evaluation,
                        depth=depth + 1,
                    )
                    for child in children
                ],
            )
        if keys == {"not"}:
            verdict = self._expression(
                state=state,
                query=query,
                expression=expression["not"],
                evaluation=evaluation,
                depth=depth + 1,
            )
            return {"pass": "fail", "fail": "pass", "unknown": "unknown"}[verdict]
        required = {"claim_id", "query", "assert"}
        if keys != required:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "verification leaf must contain claim_id, query, and assert",
            )
        claim_id = expression.get("claim_id")
        query_payload = expression.get("query")
        assertion = expression.get("assert")
        if not isinstance(claim_id, str) or not claim_id:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "claim_id must be an opaque string")
        if claim_id in evaluation.claim_ids:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"duplicate claim_id: {claim_id}")
        evaluation.claim_ids.add(claim_id)
        if not isinstance(query_payload, Mapping) or not isinstance(assertion, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "claim query/assert must be objects")
        evidence_id = state.issue_evidence_id()
        try:
            page = query(query_payload)
            if not isinstance(page, QueryPage):
                raise ProtocolError(
                    ErrorCode.INTERNAL_ERROR, "verification query returned an invalid page"
                )
            verdict, observed = self._assert(page, assertion)
            if not page.adapter_id or not page.resource:
                raise ProtocolError(
                    ErrorCode.INTERNAL_ERROR,
                    "verification query omitted its dependency identity",
                )
            dependency_revision = state.dependency_revision(
                page.adapter_id,
                page.resource,
                initialize_from=page.revision,
            )
            dependency = {
                "surface": str(query_payload.get("resource", "unknown")),
                "revision": dependency_revision,
            }
            evaluation.dependencies.append(dependency)
            evaluation.internal_dependencies.append({
                "adapter_id": page.adapter_id,
                "resource": page.resource,
                "revision": dependency_revision,
            })
            evidence = {
                "evidence_id": evidence_id,
                "claim_id": claim_id,
                "resource": query_payload.get("resource"),
                "record_count": len(page.records),
                "total": page.total,
                "truncated": page.truncated,
            }
        except ProtocolError as exc:
            if exc.code in {
                ErrorCode.INVALID_REQUEST,
                ErrorCode.POLICY_VIOLATION,
                ErrorCode.BUDGET_EXHAUSTED,
            }:
                raise
            verdict, observed = "unknown", None
            evidence = {
                "evidence_id": evidence_id,
                "claim_id": claim_id,
                "resource": query_payload.get("resource"),
                "error": exc.to_dict(),
            }
        evaluation.evidence.append(evidence)
        evaluation.claims.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "observed": observed,
                "evidence_ids": [evidence_id],
            }
        )
        return verdict

    def _assert(
        self, page: QueryPage, assertion: Mapping[str, Any]
    ) -> tuple[str, Any]:
        allowed_keys = {"op", "field", "value", "tolerance"}
        if set(assertion) - allowed_keys:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "unknown assertion field")
        operation = assertion.get("op")
        field = assertion.get("field")
        if operation not in {
            "exists",
            "absent",
            "eq",
            "ne",
            "contains",
            "matches",
            "count",
            "approx",
            "parseable",
        }:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "unsupported assertion op")
        if field is not None and not isinstance(field, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "assertion field must be a string")
        records = page.records
        if operation == "count":
            if page.total is None and page.truncated:
                return "unknown", len(records)
            observed = page.total if page.total is not None else len(records)
            if "value" not in assertion or not isinstance(assertion["value"], (int, float)):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "count requires numeric value")
            return ("pass" if observed == assertion["value"] else "fail"), observed

        if field is None:
            values: list[Any] = [dict(record) for record in records]
        else:
            values = [get_path(record, field) for record in records]
        present = [value for value in values if value is not _MISSING]
        if operation == "exists":
            observed = bool(present if field is not None else records)
            return ("pass" if observed else "fail"), observed
        if operation == "absent":
            observed = bool(present if field is not None else records)
            return ("fail" if observed else "pass"), observed
        if not present:
            return "unknown", None
        observed: Any = present[0] if len(present) == 1 else present
        expected = assertion.get("value")
        if operation in {"eq", "ne", "contains", "matches"}:
            if "value" not in assertion:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"{operation} assertion requires value"
                )
            verdict = "pass" if compare(observed, operation, expected) else "fail"
            return verdict, observed
        if operation == "approx":
            tolerance = assertion.get("tolerance", 0)
            if (
                not isinstance(tolerance, (int, float))
                or isinstance(tolerance, bool)
                or tolerance < 0
                or not isinstance(expected, (int, float))
                or isinstance(expected, bool)
                or not isinstance(observed, (int, float))
                or isinstance(observed, bool)
            ):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "approx requires numeric value, observed field, and nonnegative tolerance",
                )
            return (
                "pass" if abs(observed - expected) <= tolerance else "fail",
                observed,
            )
        # parseable: ``value`` names the requested generic representation.
        target = assertion.get("value")
        if target not in {"json", "int", "float", "bool", "url", "iso8601"}:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "parseable value must be json, int, float, bool, url, or iso8601",
            )
        passed = self._parseable(observed, target)
        return ("pass" if passed else "fail"), observed

    @staticmethod
    def _parseable(value: Any, target: str) -> bool:
        try:
            if target == "json":
                json.loads(value) if isinstance(value, str) else json.dumps(value)
            elif target == "int":
                int(value)
            elif target == "float":
                float(value)
            elif target == "bool":
                if isinstance(value, bool):
                    return True
                if not isinstance(value, str) or value.casefold() not in {"true", "false"}:
                    return False
            elif target == "url":
                parsed = urlparse(str(value))
                return parsed.scheme in {"http", "https", "file"} and bool(
                    parsed.netloc or parsed.scheme == "file"
                )
            elif target == "iso8601":
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return True
        except (TypeError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _combine(mode: str, verdicts: list[str]) -> str:
        if not verdicts or any(verdict not in VERDICTS for verdict in verdicts):
            raise ProtocolError(ErrorCode.INTERNAL_ERROR, "invalid verification verdict set")
        if mode == "all":
            if "fail" in verdicts:
                return "fail"
            if "unknown" in verdicts:
                return "unknown"
            return "pass"
        if "pass" in verdicts:
            return "pass"
        if "unknown" in verdicts:
            return "unknown"
        return "fail"
