"""Canonical recursive filtering, ordering, projection, and pagination."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .protocol import ErrorCode, ProtocolError
from .state import EpisodeState, canonical_fingerprint

try:  # A true regex timeout when the optional dependency is present.
    import regex as _timeout_regex  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - minimal benchmark image fallback
    _timeout_regex = None


DEFAULT_LIMIT = 30
MAX_LIMIT = 100
MAX_ORDER_FIELDS = 2
MAX_REGEX_CHARS = 256
MAX_FILTER_NODES = 512
MAX_FILTER_DEPTH = 32
_MISSING = object()
_FIELD_PART = re.compile(r"^(?:[A-Za-z][A-Za-z0-9_-]*|[0-9]+)$")


@dataclass(frozen=True)
class QueryPage:
    records: tuple[Mapping[str, Any], ...]
    next_cursor: str | None
    truncated: bool
    total: int | None
    revision: str
    adapter_id: str | None = None
    resource: str | None = None
    data_handle: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Exact QueryResultSchema shape.  The revision belongs in the response
        # envelope/dependency receipt, not inside the query result.
        result = {
            "records": [dict(item) for item in self.records],
            "next_cursor": self.next_cursor,
            "truncated": self.truncated,
            "total": self.total,
        }
        if self.data_handle is not None:
            # ``overflow_handle`` is the canonical name for a kernel-owned
            # serialization handle.  Keep ``data_handle`` as a wire-compatible
            # alias for frozen v1 traces, but adapter-owned collections use the
            # distinct ``collection_handle`` field inside their records.
            result["overflow_handle"] = self.data_handle
            result["data_handle"] = self.data_handle
        return result


def _validate_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or len(path) > 256:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "field path is invalid")
    parts = tuple(path.split("."))
    if any(
        not _FIELD_PART.fullmatch(part) or part.startswith("__") for part in parts
    ):
        raise ProtocolError(
            ErrorCode.POLICY_VIOLATION, f"unsafe field path: {path!r}"
        )
    return parts


def get_path(value: Any, path: str, *, missing: Any = _MISSING) -> Any:
    current = value
    for part in _validate_path(path):
        if isinstance(current, Mapping):
            if part not in current:
                return missing
            current = current[part]
        elif isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return missing
            current = current[index]
        else:
            return missing
    return current


def _safe_regex_search(pattern: str, text: str) -> bool:
    if len(pattern) > MAX_REGEX_CHARS:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "regex exceeds 256 characters")
    if "\x00" in pattern:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, "regex contains NUL")
    if _timeout_regex is not None:
        try:
            return _timeout_regex.search(pattern, text, timeout=0.02) is not None
        except TimeoutError as exc:
            raise ProtocolError(
                ErrorCode.TIMEOUT, "regex evaluation exceeded its safety timeout"
            ) from exc
        except _timeout_regex.error as exc:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid regex: {exc}") from exc
    # Stdlib ``re`` has no timeout.  Allow a small non-grouping subset and cap
    # input, preventing the nested-repeat/backreference cases that go nonlinear.
    if (
        any(token in pattern for token in ("(", ")", "|", "(?"))
        or re.search(r"\\[1-9]", pattern)
        or re.search(r"(?:[*+?]|\{\d+(?:,\d*)?\})(?:[*+?]|\{)", pattern)
    ):
        raise ProtocolError(
            ErrorCode.POLICY_VIOLATION,
            "regex uses constructs unavailable in the timeout-safe subset",
        )
    try:
        return re.search(pattern, text[:65_536]) is not None
    except re.error as exc:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid regex: {exc}") from exc


def compare(actual: Any, operation: str, expected: Any = None) -> bool:
    if actual is _MISSING:
        return False
    if operation == "eq":
        return actual == expected
    if operation == "ne":
        return actual != expected
    if operation == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, (list, tuple, set, frozenset, dict)):
            return expected in actual
        return False
    if operation == "starts_with":
        return (
            isinstance(actual, str)
            and isinstance(expected, str)
            and actual.startswith(expected)
        )
    if operation == "ends_with":
        return (
            isinstance(actual, str)
            and isinstance(expected, str)
            and actual.endswith(expected)
        )
    if operation == "matches":
        return (
            isinstance(actual, str)
            and isinstance(expected, str)
            and _safe_regex_search(expected, actual)
        )
    if operation == "in":
        return isinstance(
            expected, (str, list, tuple, set, frozenset, dict)
        ) and actual in expected
    if operation == "has":
        return isinstance(actual, (list, tuple, set, frozenset, dict)) and expected in actual
    if operation == "is_true":
        return actual is True
    if operation == "is_false":
        return actual is False
    if operation in {"gt", "gte", "lt", "lte"}:
        if isinstance(actual, bool) or isinstance(expected, bool):
            return False
        try:
            if operation == "gt":
                return actual > expected
            if operation == "gte":
                return actual >= expected
            if operation == "lt":
                return actual < expected
            return actual <= expected
        except TypeError:
            return False
    raise ProtocolError(
        ErrorCode.INVALID_REQUEST, f"unsupported query operator: {operation}"
    )


class _FilterBudget:
    def __init__(self) -> None:
        self.nodes = 0

    def take(self, depth: int) -> None:
        self.nodes += 1
        if self.nodes > MAX_FILTER_NODES or depth > MAX_FILTER_DEPTH:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED, "query filter expression is too large"
            )


def evaluate_filter(
    item: Mapping[str, Any], where: Mapping[str, Any] | None
) -> bool:
    budget = _FilterBudget()

    def evaluate(raw: Mapping[str, Any] | None, depth: int) -> bool:
        budget.take(depth)
        if raw is None or raw == {}:
            return True
        if not isinstance(raw, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "where must be an object")
        operation = raw.get("op")
        if operation in {"all", "any"}:
            if set(raw) != {"op", "filters"}:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"{operation} filter has unknown fields"
                )
            filters = raw.get("filters")
            if not isinstance(filters, (list, tuple)) or not 1 <= len(filters) <= 128:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"{operation} requires 1..128 filters"
                )
            if not all(isinstance(value, Mapping) for value in filters):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "nested filter must be an object")
            outcomes = [evaluate(value, depth + 1) for value in filters]
            return all(outcomes) if operation == "all" else any(outcomes)
        if operation == "not":
            if set(raw) != {"op", "filter"}:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, "not filter has unknown fields"
                )
            child = raw.get("filter")
            if not isinstance(child, Mapping):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "not requires filter")
            return not evaluate(child, depth + 1)
        field = raw.get("field")
        if not isinstance(field, str) or not isinstance(operation, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "leaf filter requires op and field")
        allowed = {
            "eq",
            "ne",
            "contains",
            "starts_with",
            "ends_with",
            "matches",
            "gt",
            "gte",
            "lt",
            "lte",
            "in",
            "has",
            "is_true",
            "is_false",
        }
        if operation not in allowed:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, f"unsupported query operator: {operation}"
            )
        if operation not in {"is_true", "is_false"} and "value" not in raw:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, f"{operation} filter requires value"
            )
        expected_keys = (
            {"op", "field"}
            if operation in {"is_true", "is_false"}
            else {"op", "field", "value"}
        )
        if set(raw) != expected_keys:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, f"{operation} filter has unknown fields"
            )
        return compare(get_path(item, field), operation, raw.get("value"))

    return evaluate(where, 0)


def _sort_key(value: Any) -> tuple[int, Any]:
    if isinstance(value, bool):
        return 0, int(value)
    if isinstance(value, (int, float)) and math.isfinite(value):
        return 1, value
    if isinstance(value, str):
        return 2, value.casefold()
    if isinstance(value, (list, tuple)):
        return 3, repr(value)
    return 4, repr(value)


def _is_absent(value: Any) -> bool:
    return value is _MISSING or value is None


class QueryEngine:
    def query(
        self,
        *,
        state: EpisodeState,
        adapter_id: str,
        resource: str,
        items: Sequence[Mapping[str, Any]],
        payload: Mapping[str, Any],
        consume_budget: bool = True,
    ) -> QueryPage:
        if consume_budget:
            state.consume_operation()
        if len(items) > state.limits.max_collection_items:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED, "query collection exceeds 5000 items"
            )
        if not all(isinstance(item, Mapping) for item in items):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "query records must be objects")
        revision = state.current_revision(adapter_id, resource)
        if revision is None:
            raise ProtocolError(
                ErrorCode.PRECONDITION_FAILED,
                "resource has no live observation; observe it before querying",
                retryable=True,
            )

        required = {"resource", "scope", "order_by", "parameters", "freshness"}
        allowed_payload = required | {"where", "fields", "limit", "cursor"}
        unknown = set(payload) - allowed_payload
        if unknown:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                f"query payload has unknown fields: {sorted(unknown)!r}",
            )
        missing = required - set(payload)
        if missing:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                f"query payload missing fields: {sorted(missing)!r}",
            )
        if payload.get("resource") != resource:
            raise ProtocolError(
                ErrorCode.UNKNOWN_RESOURCE, "query resource does not match adapter resource"
            )
        scope = payload.get("scope")
        where = payload.get("where", {})
        fields = payload.get("fields", ())
        order_by = payload.get("order_by")
        parameters = payload.get("parameters")
        freshness = payload.get("freshness")
        cursor = payload.get("cursor")
        limit = payload.get("limit", DEFAULT_LIMIT)
        if not isinstance(scope, Mapping) or not isinstance(parameters, Mapping):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "scope and parameters must be objects"
            )
        if set(scope) - {"adapter", "surface", "ref", "path", "document"}:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "scope has unknown fields")
        if not isinstance(where, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "where must be an object")
        if not isinstance(fields, (list, tuple)) or len(fields) > 128 or not all(
            isinstance(field, str) for field in fields
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "fields must be a string array")
        if len(set(fields)) != len(fields):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "fields must be unique")
        if not isinstance(order_by, (list, tuple)) or len(order_by) > MAX_ORDER_FIELDS:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "at most two order fields are allowed")
        if freshness not in {"live", "cache_ok"}:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid query freshness")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "limit must be between 1 and 100")
        if cursor is not None and not isinstance(cursor, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "cursor must be an opaque string")

        normalized_order: list[dict[str, str]] = []
        for entry in order_by:
            if not isinstance(entry, Mapping):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "order field must be an object")
            if set(entry) != {"field", "direction"}:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "order field has unknown fields")
            field, direction = entry.get("field"), entry.get("direction")
            if not isinstance(field, str) or direction not in {"asc", "desc"}:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid order field/direction")
            _validate_path(field)
            normalized_order.append({"field": field, "direction": direction})
        for field in fields:
            _validate_path(field)
        # Validate the recursive filter even when there are no records.
        evaluate_filter({}, where)

        signature = canonical_fingerprint(
            {
                "resource": resource,
                "scope": dict(scope),
                "where": dict(where),
                "fields": list(fields),
                "order_by": normalized_order,
                "parameters": dict(parameters),
                "limit": limit,
                "freshness": freshness,
            }
        )
        offset = 0
        if cursor is not None:
            offset = state.resolve_cursor(
                cursor,
                adapter_id=adapter_id,
                resource=resource,
                revision=revision,
                query_fingerprint=signature,
            ).offset

        selected = [dict(item) for item in items if evaluate_filter(item, where)]
        # Stable multi-key sort, keeping missing/null fields last in both
        # directions rather than reversing them to the front for descending.
        for entry in reversed(normalized_order):
            present = [
                item
                for item in selected
                if not _is_absent(get_path(item, entry["field"]))
            ]
            absent = [
                item
                for item in selected
                if _is_absent(get_path(item, entry["field"]))
            ]
            present.sort(
                key=lambda item, field=entry["field"]: _sort_key(get_path(item, field)),
                reverse=entry["direction"] == "desc",
            )
            selected = present + absent
        total = len(selected)
        page_records = selected[offset : offset + limit]
        if fields:
            projected: list[dict[str, Any]] = []
            for item in page_records:
                # Identity, state, actions, and relationships are part of the
                # semantic record contract, not optional projection fields.
                # Preserve them even when the model asks for an unavailable
                # application-specific field; a bad projection must never
                # collapse a live entity into an ungrounded ``{}`` record.
                output: dict[str, Any] = {
                    field: item[field]
                    for field in (
                        "ref", "kind", "states", "advertised_actions",
                        "parent_ref", "child_refs", "owner_ref",
                        "label_for_ref", "labelled_by_ref",
                        "controller_for_ref", "revision", "source", "freshness",
                    )
                    if field in item
                }
                for field in fields:
                    value = get_path(item, field)
                    if value is not _MISSING:
                        output[field] = value
                projected.append(output)
            page_records = projected
        next_offset = offset + len(page_records)
        next_cursor = None
        if next_offset < total:
            next_cursor = state.issue_cursor(
                adapter_id=adapter_id,
                resource=resource,
                revision=revision,
                query_fingerprint=signature,
                offset=next_offset,
            )
        return QueryPage(
            records=tuple(page_records),
            next_cursor=next_cursor,
            truncated=next_cursor is not None,
            total=total,
            revision=revision,
            adapter_id=adapter_id,
            resource=resource,
        )
