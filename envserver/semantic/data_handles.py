"""Opaque, bounded, episode-local storage for large semantic collections.

Handles deliberately contain no encoded locator, path, URL, or task identity.
They are intended to be owned by one :class:`SemanticRuntime` and discarded
with that runtime.  Adapters use them to keep large documents and research
collections out of the policy-model context without losing re-queryability.
"""

from __future__ import annotations

import secrets
import time
import json
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Mapping, Sequence

from .protocol import ErrorCode, ProtocolError, utc_now


@dataclass(frozen=True)
class DataHandleRecord:
    handle: str
    kind: str
    records: tuple[Mapping[str, Any], ...]
    created_at: str
    expires_at: float
    metadata: Mapping[str, Any]


class DataHandleStore:
    """A size/TTL bounded store whose identifiers are random capabilities."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        max_handles: int = 128,
        max_records_per_handle: int = 5_000,
        max_serialized_chars: int = 32_000_000,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if ttl_seconds <= 0 or max_handles < 1 or max_records_per_handle < 1 or max_serialized_chars < 1:
            raise ValueError("data-handle limits must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_handles = int(max_handles)
        self.max_records_per_handle = int(max_records_per_handle)
        self.max_serialized_chars = int(max_serialized_chars)
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(18))
        self._records: dict[str, DataHandleRecord] = {}
        self._lock = RLock()

    def _prune(self) -> None:
        now = self._clock()
        for handle, record in list(self._records.items()):
            if record.expires_at <= now:
                self._records.pop(handle, None)

    def create(
        self,
        kind: str,
        records: Sequence[Mapping[str, Any]],
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> DataHandleRecord:
        if not isinstance(kind, str) or not kind or len(kind) > 128:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "data-handle kind is invalid")
        if len(records) > self.max_records_per_handle:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED,
                "data-handle collection exceeds its record limit",
            )
        try:
            encoded = json.dumps(records, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "data-handle records must be JSON") from error
        if len(encoded) > self.max_serialized_chars:
            raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "data-handle payload exceeds its byte limit")
        normalized = tuple(json.loads(encoded))
        with self._lock:
            self._prune()
            if len(self._records) >= self.max_handles:
                oldest = min(self._records.values(), key=lambda value: value.expires_at)
                self._records.pop(oldest.handle, None)
            for _ in range(20):
                handle = f"data_{self._token_factory()}"
                if handle not in self._records:
                    break
            else:  # pragma: no cover - cryptographic collision defense
                raise ProtocolError(ErrorCode.INTERNAL_ERROR, "data handle collision")
            record = DataHandleRecord(
                handle=handle,
                kind=kind,
                records=normalized,
                created_at=utc_now(),
                expires_at=self._clock() + self.ttl_seconds,
                metadata=dict(metadata or {}),
            )
            self._records[handle] = record
            return record

    def get(self, handle: str, *, kind: str | None = None) -> DataHandleRecord:
        with self._lock:
            self._prune()
            record = self._records.get(handle)
            if record is None:
                raise ProtocolError(
                    ErrorCode.NOT_FOUND,
                    "data handle is missing or expired",
                    retryable=False,
                )
            if kind is not None and record.kind != kind:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "data handle kind mismatch")
            return record

    def describe(self) -> list[dict[str, Any]]:
        with self._lock:
            self._prune()
            return [
                {
                    "data_handle": record.handle,
                    "kind": record.kind,
                    "record_count": len(record.records),
                    "created_at": record.created_at,
                    **dict(record.metadata),
                }
                for record in sorted(self._records.values(), key=lambda value: value.created_at)
            ]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
