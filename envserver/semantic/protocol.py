"""Wire-level protocol envelopes and structured semantic errors."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Sequence


PROTOCOL_VERSION = "1.0"
MAX_RESPONSE_CHARS = 12_000
MAX_FIELD_CHARS = 2_000
MAX_COLLECTION_ITEMS = 5_000


class Status(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    REJECTED = "rejected"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


class SideEffectState(str, Enum):
    NONE = "none"
    APPLIED = "applied"
    UNKNOWN = "unknown"


class ErrorCode(str, Enum):
    INVALID_REQUEST = "invalid_request"
    UNKNOWN_RESOURCE = "unknown_resource"
    UNSUPPORTED = "unsupported"
    REPRESENTATION_GAP = "representation_gap"
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    STALE_REF = "stale_ref"
    REVISION_CONFLICT = "revision_conflict"
    PRECONDITION_FAILED = "precondition_failed"
    POSTCONDITION_FAILED = "postcondition_failed"
    NO_EFFECT = "no_effect"
    UNCERTAIN = "uncertain"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    ARTIFACT_CONFLICT = "artifact_conflict"
    POLICY_VIOLATION = "policy_violation"
    BUDGET_EXHAUSTED = "budget_exhausted"
    INTERNAL_ERROR = "internal_error"


OPERATIONS = frozenset({"query", "act", "verify", "run"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"value is not protocol-serializable: {type(value).__name__}")


def _bounded(value: Any, *, depth: int = 0) -> Any:
    """Return a deterministic JSON-safe value inside field/collection limits."""

    if depth > 32:
        return "[truncated: nesting limit]"
    value = _plain(value)
    if isinstance(value, str):
        if len(value) <= MAX_FIELD_CHARS:
            return value
        return value[: MAX_FIELD_CHARS - 28] + "...[truncated field]"
    if isinstance(value, list):
        clipped = value[:MAX_COLLECTION_ITEMS]
        out = [_bounded(item, depth=depth + 1) for item in clipped]
        if len(value) > MAX_COLLECTION_ITEMS:
            out.append({"truncated_items": len(value) - MAX_COLLECTION_ITEMS})
        return out
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_COLLECTION_ITEMS:
                out["_truncated_items"] = len(value) - MAX_COLLECTION_ITEMS
                break
            out[key[:MAX_FIELD_CHARS]] = _bounded(item, depth=depth + 1)
        return out
    return value


@dataclass(frozen=True)
class Recovery:
    allowed_operations: tuple[str, ...] = ()
    suggested_resource: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "allowed_operations": list(self.allowed_operations),
        }
        if self.suggested_resource is not None:
            payload["suggested_resource"] = self.suggested_resource
        return payload


class ProtocolError(Exception):
    """A typed, model-actionable semantic protocol failure."""

    def __init__(
        self,
        code: ErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        side_effect_state: SideEffectState | str = SideEffectState.NONE,
        missing_capability: str | None = None,
        candidates: Sequence[Mapping[str, Any]] = (),
        recovery: Recovery | None = None,
    ) -> None:
        self.code = ErrorCode(code)
        self.message = str(message)
        self.retryable = bool(retryable)
        self.side_effect_state = SideEffectState(side_effect_state)
        self.missing_capability = missing_capability
        self.candidates = tuple(dict(candidate) for candidate in candidates)
        self.recovery = recovery or Recovery()
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": _bounded(self.message),
            "retryable": self.retryable,
            "side_effect_state": self.side_effect_state.value,
            "missing_capability": _bounded(self.missing_capability),
            "candidates": [_bounded(candidate) for candidate in self.candidates],
            "recovery": _bounded(self.recovery.to_dict()),
        }


@dataclass(frozen=True)
class RequestEnvelope:
    protocol_version: str
    request_id: str
    episode_id: str
    operation: str
    payload: Mapping[str, Any]

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> "RequestEnvelope":
        if not isinstance(raw, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "request must be an object")
        version = raw.get("protocol_version")
        request_id = raw.get("request_id")
        episode_id = raw.get("episode_id")
        operation = raw.get("operation")
        payload = raw.get("payload")
        if version != PROTOCOL_VERSION:
            raise ProtocolError(
                ErrorCode.UNSUPPORTED,
                f"unsupported protocol_version: {version!r}",
            )
        if not isinstance(request_id, str) or not request_id.strip():
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "request_id must be a string")
        if not isinstance(episode_id, str) or not episode_id.strip():
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "episode_id must be a string")
        if operation not in OPERATIONS:
            raise ProtocolError(
                ErrorCode.UNSUPPORTED,
                f"unsupported operation: {operation!r}",
                recovery=Recovery(tuple(sorted(OPERATIONS))),
            )
        if not isinstance(payload, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "payload must be an object")
        try:
            _plain(payload)
        except (TypeError, ValueError) as exc:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, str(exc)) from exc
        return cls(version, request_id, episode_id, operation, dict(payload))


@dataclass(frozen=True)
class ResponseEnvelope:
    request_id: str
    status: Status | str
    adapter_id: str
    before_revision: str | None
    after_revision: str | None
    result: Mapping[str, Any] | None = field(default_factory=dict)
    provenance: Sequence[Mapping[str, Any]] = field(default_factory=tuple)
    error: ProtocolError | None = None
    observed_at: str = field(default_factory=utc_now)
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        status = Status(self.status)
        payload: dict[str, Any] = {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "status": status.value,
            "adapter_id": self.adapter_id,
            "observed_at": self.observed_at,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "result": _bounded(dict(self.result)) if self.result is not None else None,
            "provenance": _bounded(list(self.provenance)),
            "error": self.error.to_dict() if self.error else None,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) <= MAX_RESPONSE_CHARS:
            return payload

        # Preserve the protocol/error/revision contract and fail visibly rather
        # than silently returning an oversized response.  The result remains
        # useful as a retry signal but never leaks a giant adapter field.
        payload["status"] = Status.PARTIAL.value
        payload["result"] = {
            "truncated": True,
            "reason": "response exceeded serialized limit",
        }
        payload["provenance"] = []
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > MAX_RESPONSE_CHARS:
            raise ProtocolError(
                ErrorCode.INTERNAL_ERROR,
                "protocol envelope exceeds serialized response limit",
            )
        return payload


def ok_response(
    *,
    request_id: str,
    adapter_id: str,
    result: Mapping[str, Any],
    before_revision: str | None,
    after_revision: str | None,
    provenance: Sequence[Mapping[str, Any]] = (),
    status: Status | str = Status.OK,
) -> ResponseEnvelope:
    return ResponseEnvelope(
        request_id=request_id,
        status=status,
        adapter_id=adapter_id,
        before_revision=before_revision,
        after_revision=after_revision,
        result=result,
        provenance=provenance,
    )


def error_response(
    *,
    request_id: str,
    adapter_id: str,
    error: ProtocolError,
    before_revision: str | None = None,
    after_revision: str | None = None,
    result: Mapping[str, Any] | None = None,
    provenance: Sequence[Mapping[str, Any]] = (),
    status: Status | str | None = None,
) -> ResponseEnvelope:
    if status is None:
        if error.code in {
            ErrorCode.INVALID_REQUEST,
            ErrorCode.UNSUPPORTED,
            ErrorCode.PERMISSION_DENIED,
            ErrorCode.POLICY_VIOLATION,
            ErrorCode.BUDGET_EXHAUSTED,
        }:
            status = Status.REJECTED
        elif error.side_effect_state is SideEffectState.UNKNOWN or error.code is ErrorCode.UNCERTAIN:
            status = Status.UNCERTAIN
        else:
            status = Status.FAILED
    return ResponseEnvelope(
        request_id=request_id,
        status=status,
        adapter_id=adapter_id,
        before_revision=before_revision,
        after_revision=after_revision,
        result=result,
        provenance=provenance,
        error=error,
    )
