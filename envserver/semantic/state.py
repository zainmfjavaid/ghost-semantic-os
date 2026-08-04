"""Per-episode semantic identity, revision, cursor, and receipt state."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable, Mapping, Sequence

from .protocol import (
    ErrorCode,
    ProtocolError,
    Recovery,
    SideEffectState,
    utc_now,
)


@dataclass(frozen=True)
class StateLimits:
    max_collection_items: int = 5_000
    # Entity refs are an episode-scoped capability cache, not a cumulative
    # issuance budget.  Keep the live set bounded independently from the
    # maximum size of one legal observation and retire least-recently-issued
    # capabilities when it fills.
    # Eight capacity-sized observations matches the provider context
    # governor's maximum expanded-observation window.  This keeps refs still
    # visible to the policy live while preventing unbounded episode growth.
    max_live_refs: int = 40_000
    cursor_ttl_seconds: float = 600.0
    max_idempotency_key_chars: int = 256
    max_receipts: int = 5_000
    max_ref_tombstones: int = 5_000


@dataclass(frozen=True)
class RevisionRecord:
    revision: str
    adapter_id: str
    resource: str
    sequence: int
    observed_at: str


@dataclass(frozen=True)
class RefRecord:
    ref: str
    adapter_id: str
    resource: str
    revision: str
    locator: Mapping[str, Any]
    fingerprint: Mapping[str, Any]
    observation_id: str | None
    created_at: str

    def public(self) -> dict[str, Any]:
        # Locator data is private adapter state.  The public ref carries only
        # the identity needed by subsequent semantic operations.
        return {
            "ref": self.ref,
            "adapter_id": self.adapter_id,
            "resource": self.resource,
            "revision": self.revision,
            "fingerprint": dict(self.fingerprint),
        }


@dataclass(frozen=True)
class RefTombstone:
    """Bounded evidence that an episode-scoped ref once existed.

    Tombstones intentionally retain neither the native locator nor semantic
    fingerprint.  They distinguish a stale capability from a fabricated one
    without keeping a deleted target alive or making it eligible for
    retargeting.
    """

    ref: str
    adapter_id: str
    resource: str
    revision: str
    reason: str
    retired_at: str


@dataclass(frozen=True)
class CursorRecord:
    cursor: str
    adapter_id: str
    resource: str
    revision: str
    query_fingerprint: str
    offset: int
    expires_at: float


@dataclass(frozen=True)
class DataHandleRecord:
    handle: str
    adapter_id: str
    resource: str
    revision: str
    serialized: str
    content_hash: str
    expires_at: float
    created_at: str

    def public(self) -> dict[str, Any]:
        return {
            "data_handle": self.handle,
            "source_adapter": self.adapter_id,
            "source_resource": self.resource,
            "source_revision": self.revision,
            "content_hash": self.content_hash,
            "characters": len(self.serialized),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class ObservationReceipt:
    receipt_id: str
    adapter_id: str
    resource: str
    revision: str
    refs: tuple[str, ...]
    summary: Mapping[str, Any]
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "adapter_id": self.adapter_id,
            "resource": self.resource,
            "revision": self.revision,
            "refs": list(self.refs),
            "summary": dict(self.summary),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ActionReceipt:
    receipt_id: str
    adapter_id: str
    resource: str
    action: str
    target_ref: str | None
    before_revision: str | None
    after_revision: str | None
    changed: bool | None
    side_effect_state: SideEffectState
    idempotency_key: str | None
    request_fingerprint: str | None
    result: Mapping[str, Any]
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "adapter_id": self.adapter_id,
            "resource": self.resource,
            "action": self.action,
            "target_ref": self.target_ref,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "changed": self.changed,
            "side_effect_state": self.side_effect_state.value,
            "idempotency_key": self.idempotency_key,
            "result": dict(self.result),
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class ActionReconciliationReceipt:
    """Immutable proof resolving a previously uncertain action receipt."""

    receipt_id: str
    action_receipt_id: str
    verification_id: str
    verification_fingerprint: str
    outcome: SideEffectState
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.receipt_id,
            "action_receipt_id": self.action_receipt_id,
            "verification_id": self.verification_id,
            "verification_fingerprint": self.verification_fingerprint,
            "outcome": self.outcome.value,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True)
class VerificationReceipt:
    receipt_id: str
    adapter_id: str
    resource: str
    revision: str | None
    passed: bool
    assertion: Mapping[str, Any]
    evidence: Mapping[str, Any]
    action_receipt_id: str | None
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "adapter_id": self.adapter_id,
            "resource": self.resource,
            "revision": self.revision,
            "passed": self.passed,
            "assertion": dict(self.assertion),
            "evidence": dict(self.evidence),
            "action_receipt_id": self.action_receipt_id,
            "observed_at": self.observed_at,
        }


def canonical_fingerprint(value: Any) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST, "value is not canonical JSON"
        ) from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EpisodeState:
    """All volatile semantic state belonging to one OS episode.

    Refs, revisions, and cursors contain no encoded locator information.  They
    are random handles into this server-local object and fail closed when the
    relevant resource revision changes.
    """

    def __init__(
        self,
        episode_id: str,
        *,
        max_tool_calls: int,
        limits: StateLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(episode_id, str) or not episode_id:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "episode_id is required")
        if not isinstance(max_tool_calls, int) or max_tool_calls < 1:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "max_tool_calls must be a positive integer"
            )
        self.episode_id = episode_id
        self.limits = limits or StateLimits()
        self.semantic_budget = min(max_tool_calls * 10, 1_000)
        self.semantic_operations = 0
        self._clock = clock
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(18))
        self._revisions: dict[tuple[str, str], RevisionRecord] = {}
        self._native_revisions: dict[tuple[str, str], str] = {}
        # Query revisions identify one materialized resource view.  They may
        # legitimately differ when the same resource is queried with another
        # range, path, document, or surface scope, even though no state has
        # changed.  Verification freshness therefore uses a separate mutation
        # epoch.  Read-only observations initialize but never advance it;
        # applied/uncertain mutations do.  This keeps multi-view evidence
        # current without weakening stale detection after real actions.
        self._dependency_revisions: dict[tuple[str, str], str] = {}
        self._refs: dict[str, RefRecord] = {}
        self._ref_index: dict[tuple[str, str, str, str], str] = {}
        self._ref_tombstones: dict[str, RefTombstone] = {}
        self._cursors: dict[str, CursorRecord] = {}
        self._data_handles: dict[str, DataHandleRecord] = {}
        self._observations: dict[str, ObservationReceipt] = {}
        self._actions: dict[str, ActionReceipt] = {}
        self._action_reconciliations: dict[str, ActionReconciliationReceipt] = {}
        self._reconciliations_by_action: dict[str, str] = {}
        self._verifications: dict[str, VerificationReceipt] = {}
        self._idempotency: dict[str, str] = {}
        self._ids: set[str] = set()
        self._lock = RLock()

    def _new_id(self, prefix: str) -> str:
        for _ in range(20):
            value = f"{prefix}_{self._token_factory()}"
            if value not in self._ids:
                self._ids.add(value)
                return value
        raise ProtocolError(ErrorCode.INTERNAL_ERROR, "opaque ID generator collided")

    def consume_operation(self, count: int = 1) -> None:
        if not isinstance(count, int) or count < 1:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "operation count is invalid")
        with self._lock:
            if self.semantic_operations + count > self.semantic_budget:
                raise ProtocolError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    "episode semantic operation budget exhausted",
                )
            self.semantic_operations += count

    @staticmethod
    def _key(adapter_id: str, resource: str) -> tuple[str, str]:
        if not adapter_id or not resource:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "adapter_id and resource are required"
            )
        return adapter_id, resource

    def current_revision(self, adapter_id: str, resource: str) -> str | None:
        with self._lock:
            record = self._revisions.get(self._key(adapter_id, resource))
            return record.revision if record else None

    def dependency_revision(
        self,
        adapter_id: str,
        resource: str,
        *,
        initialize_from: str | None = None,
    ) -> str:
        """Return the stable verification epoch for a mutable resource.

        ``current_revision`` belongs to a query view and can change when only
        the view parameters change.  A dependency revision instead answers
        whether a mutation has occurred since evidence was observed.
        """

        key = self._key(adapter_id, resource)
        with self._lock:
            revision = self._dependency_revisions.get(key)
            if revision is None:
                revision = initialize_from or self._new_id("dep")
                self._dependency_revisions[key] = revision
            return revision

    def advance_dependency_revision(self, adapter_id: str, resource: str) -> str:
        """Invalidate verification evidence after a possible mutation."""

        key = self._key(adapter_id, resource)
        with self._lock:
            revision = self._new_id("dep")
            self._dependency_revisions[key] = revision
            return revision

    @staticmethod
    def _ref_identity(record: RefRecord) -> tuple[str, str, str, str]:
        return (
            record.adapter_id,
            record.resource,
            record.revision,
            canonical_fingerprint(
                {
                    "locator": record.locator,
                    "fingerprint": record.fingerprint,
                }
            ),
        )

    def _retire_ref_locked(self, record: RefRecord, *, reason: str) -> None:
        self._refs.pop(record.ref, None)
        self._ref_index.pop(self._ref_identity(record), None)
        if self.limits.max_ref_tombstones <= 0:
            return
        # Refreshing a tombstone moves it to the newest end of insertion order.
        self._ref_tombstones.pop(record.ref, None)
        self._ref_tombstones[record.ref] = RefTombstone(
            ref=record.ref,
            adapter_id=record.adapter_id,
            resource=record.resource,
            revision=record.revision,
            reason=reason,
            retired_at=utc_now(),
        )
        while len(self._ref_tombstones) > self.limits.max_ref_tombstones:
            oldest = next(iter(self._ref_tombstones))
            self._ref_tombstones.pop(oldest, None)

    def _retire_resource_refs_locked(
        self, adapter_id: str, resource: str, *, reason: str
    ) -> None:
        for record in tuple(self._refs.values()):
            if record.adapter_id == adapter_id and record.resource == resource:
                self._retire_ref_locked(record, reason=reason)

    def _make_ref_capacity_locked(self) -> None:
        """Make room for one new live capability without weakening identity.

        ``_refs`` preserves insertion order.  Existing identities are moved to
        the newest end whenever they are issued again, so the first record is
        the least recently issued capability.  Capacity eviction tombstones
        that exact opaque ID; it can therefore never be rebound to a different
        entity and resolves as ``stale_ref`` while the bounded tombstone is
        retained.
        """

        if self.limits.max_live_refs < 1:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED,
                "episode live opaque-ref capacity is disabled",
            )
        while len(self._refs) >= self.limits.max_live_refs:
            oldest = next(iter(self._refs.values()))
            self._retire_ref_locked(oldest, reason="capacity_evicted")

    @staticmethod
    def _stale_ref_error(resource: str, message: str) -> ProtocolError:
        return ProtocolError(
            ErrorCode.STALE_REF,
            message,
            retryable=True,
            recovery=Recovery(
                allowed_operations=("computer.query",),
                suggested_resource=resource,
            ),
        )

    def advance_revision(self, adapter_id: str, resource: str) -> str:
        key = self._key(adapter_id, resource)
        with self._lock:
            prior = self._revisions.get(key)
            if prior is not None:
                self._retire_resource_refs_locked(
                    adapter_id, resource, reason="revision_invalidated"
                )
            revision = self._new_id("rev")
            self._revisions[key] = RevisionRecord(
                revision=revision,
                adapter_id=adapter_id,
                resource=resource,
                sequence=(prior.sequence + 1 if prior else 1),
                observed_at=utc_now(),
            )
            return revision

    def synchronize_revision(
        self, adapter_id: str, resource: str, native_revision: str | None
    ) -> str:
        """Reflect native state changes without making observation a mutation."""

        key = self._key(adapter_id, resource)
        normalized = native_revision or "unversioned"
        with self._lock:
            current = self._revisions.get(key)
            if current is None or self._native_revisions.get(key) != normalized:
                revision = self.advance_revision(adapter_id, resource)
                self._native_revisions[key] = normalized
                return revision
            return current.revision

    def assert_revision(
        self, adapter_id: str, resource: str, expected_revision: str | None
    ) -> str | None:
        actual = self.current_revision(adapter_id, resource)
        if expected_revision != actual:
            raise ProtocolError(
                ErrorCode.REVISION_CONFLICT,
                "resource revision changed; observe again before acting",
                retryable=True,
                candidates=[{"current_revision": actual}] if actual else (),
            )
        return actual

    def issue_ref(
        self,
        *,
        adapter_id: str,
        resource: str,
        revision: str,
        locator: Mapping[str, Any],
        fingerprint: Mapping[str, Any] | None = None,
        observation_id: str | None = None,
    ) -> RefRecord:
        with self._lock:
            self.assert_revision(adapter_id, resource, revision)
            identity = (
                adapter_id,
                resource,
                revision,
                canonical_fingerprint({
                    "locator": locator,
                    "fingerprint": fingerprint or {},
                }),
            )
            existing = self._ref_index.get(identity)
            if existing is not None and existing in self._refs:
                # A ref present in a new observation is live model context.
                # Refresh its recency so unrelated future queries are evicted
                # first.  The opaque ID itself remains stable.
                record = self._refs.pop(existing)
                self._refs[existing] = record
                return record
            self._make_ref_capacity_locked()
            canonical_fingerprint(locator)
            canonical_fingerprint(fingerprint or {})
            ref = self._new_id("ref")
            record = RefRecord(
                ref=ref,
                adapter_id=adapter_id,
                resource=resource,
                revision=revision,
                locator=dict(locator),
                fingerprint=dict(fingerprint or {}),
                observation_id=observation_id,
                created_at=utc_now(),
            )
            self._refs[ref] = record
            self._ref_index[identity] = ref
            return record

    def resolve_ref(
        self,
        ref: str,
        *,
        adapter_id: str | None = None,
        resource: str | None = None,
    ) -> RefRecord:
        with self._lock:
            record = self._refs.get(ref)
            if record is None:
                tombstone = self._ref_tombstones.get(ref)
                if tombstone is not None:
                    raise self._stale_ref_error(
                        tombstone.resource,
                        "ref is stale; query the resource again",
                    )
                raise ProtocolError(ErrorCode.NOT_FOUND, "opaque ref was not found")
            if adapter_id is not None and record.adapter_id != adapter_id:
                raise self._stale_ref_error(
                    record.resource, "ref belongs to another adapter"
                )
            if resource is not None and record.resource != resource:
                raise self._stale_ref_error(
                    record.resource, "ref belongs to another resource"
                )
            current = self.current_revision(record.adapter_id, record.resource)
            if current != record.revision:
                self._retire_ref_locked(record, reason="revision_invalidated")
                raise self._stale_ref_error(
                    record.resource, "ref is stale; query the resource again"
                )
            return record

    def retire_ref(self, ref: str, *, reason: str = "deleted") -> bool:
        """Retire a known ref without allowing it to be silently rebound.

        Adapters may use this when a native entity is deleted without changing
        an independently versioned sibling resource.  Repeated retirement is
        idempotent; a genuinely unknown ref remains unknown.
        """

        with self._lock:
            record = self._refs.get(ref)
            if record is None:
                return ref in self._ref_tombstones
            self._retire_ref_locked(record, reason=reason)
            return True

    def record_observation(
        self,
        *,
        adapter_id: str,
        resource: str,
        entries: Sequence[Mapping[str, Any]] = (),
        summary: Mapping[str, Any] | None = None,
        native_revision: str | None = None,
    ) -> tuple[ObservationReceipt, tuple[RefRecord, ...]]:
        if len(entries) > self.limits.max_collection_items:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED, "observation collection limit exceeded"
            )
        if len(entries) > self.limits.max_live_refs:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED,
                "observation exceeds live opaque-ref capacity",
            )
        validated: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for entry in entries:
            locator = entry.get("locator")
            if not isinstance(locator, Mapping):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "observation entry locator must be an object",
                )
            fingerprint = entry.get("fingerprint") or {}
            if not isinstance(fingerprint, Mapping):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "observation entry fingerprint must be an object",
                )
            canonical_fingerprint(locator)
            canonical_fingerprint(fingerprint)
            validated.append((locator, fingerprint))
        with self._lock:
            revision = self.synchronize_revision(
                adapter_id, resource, native_revision
            )
            self.dependency_revision(
                adapter_id, resource, initialize_from=revision
            )
            receipt_id = self._new_id("obs")
            refs: list[RefRecord] = []
            for locator, fingerprint in validated:
                refs.append(
                    self.issue_ref(
                        adapter_id=adapter_id,
                        resource=resource,
                        revision=revision,
                        locator=locator,
                        fingerprint=fingerprint,
                        observation_id=receipt_id,
                    )
                )
            receipt = ObservationReceipt(
                receipt_id=receipt_id,
                adapter_id=adapter_id,
                resource=resource,
                revision=revision,
                refs=tuple(item.ref for item in refs),
                summary=dict(summary or {}),
                observed_at=utc_now(),
            )
            self._observations[receipt_id] = receipt
            self._trim(self._observations)
            return receipt, tuple(refs)

    def issue_cursor(
        self,
        *,
        adapter_id: str,
        resource: str,
        revision: str,
        query_fingerprint: str,
        offset: int,
    ) -> str:
        if offset < 0:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "cursor offset is invalid")
        with self._lock:
            self.assert_revision(adapter_id, resource, revision)
            if len(self._cursors) >= self.limits.max_collection_items:
                self._purge_expired_cursors()
            if len(self._cursors) >= self.limits.max_collection_items:
                raise ProtocolError(ErrorCode.BUDGET_EXHAUSTED, "cursor limit exhausted")
            cursor = self._new_id("cur")
            self._cursors[cursor] = CursorRecord(
                cursor=cursor,
                adapter_id=adapter_id,
                resource=resource,
                revision=revision,
                query_fingerprint=query_fingerprint,
                offset=offset,
                expires_at=self._clock() + self.limits.cursor_ttl_seconds,
            )
            return cursor

    def resolve_cursor(
        self,
        cursor: str,
        *,
        adapter_id: str,
        resource: str,
        revision: str,
        query_fingerprint: str,
    ) -> CursorRecord:
        with self._lock:
            record = self._cursors.get(cursor)
            if record is None:
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "cursor is unknown; restart the query from the live revision",
                    retryable=True,
                )
            if self._clock() > record.expires_at:
                self._cursors.pop(cursor, None)
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "cursor expired; restart the query from the live revision",
                    retryable=True,
                )
            if (
                record.adapter_id != adapter_id
                or record.resource != resource
                or record.revision != revision
                or record.query_fingerprint != query_fingerprint
            ):
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "cursor does not match the current query revision",
                    retryable=True,
                )
            self.assert_revision(adapter_id, resource, revision)
            return record

    def create_data_handle(
        self,
        *,
        adapter_id: str,
        resource: str,
        revision: str,
        value: Any,
    ) -> DataHandleRecord:
        self.assert_revision(adapter_id, resource, revision)
        try:
            serialized = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "data handle value is not canonical JSON"
            ) from error
        # A handle is a context-control mechanism, not an unbounded database.
        # 1.25 MiB fits at least 5,000 256-character chunks.
        if len(serialized) > 1_250_000:
            raise ProtocolError(
                ErrorCode.BUDGET_EXHAUSTED, "data handle exceeds 1,250,000 characters"
            )
        with self._lock:
            self._purge_expired_data_handles()
            if len(self._data_handles) >= self.limits.max_receipts:
                raise ProtocolError(
                    ErrorCode.BUDGET_EXHAUSTED, "data handle capacity exhausted"
                )
            handle = self._new_id("data")
            record = DataHandleRecord(
                handle=handle,
                adapter_id=adapter_id,
                resource=resource,
                revision=revision,
                serialized=serialized,
                content_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                expires_at=self._clock() + self.limits.cursor_ttl_seconds,
                created_at=utc_now(),
            )
            self._data_handles[handle] = record
            return record

    def resolve_data_handle(self, handle: str) -> DataHandleRecord:
        with self._lock:
            record = self._data_handles.get(handle)
            if record is None:
                raise ProtocolError(ErrorCode.NOT_FOUND, "data handle was not found")
            if self._clock() > record.expires_at:
                self._data_handles.pop(handle, None)
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "data handle expired; re-query the source resource",
                    retryable=True,
                )
            if self.current_revision(record.adapter_id, record.resource) != record.revision:
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "data handle source revision changed; re-query the source resource",
                    retryable=True,
                )
            return record

    def active_data_handles(self) -> list[dict[str, Any]]:
        with self._lock:
            self._purge_expired_data_handles()
            return [
                record.public()
                for _, record in sorted(self._data_handles.items())
                if self.current_revision(record.adapter_id, record.resource) == record.revision
            ]

    def _purge_expired_cursors(self) -> None:
        now = self._clock()
        for key, record in list(self._cursors.items()):
            if now > record.expires_at:
                self._cursors.pop(key, None)

    def _purge_expired_data_handles(self) -> None:
        now = self._clock()
        for key, record in list(self._data_handles.items()):
            if now > record.expires_at:
                self._data_handles.pop(key, None)

    def replay_action(
        self, idempotency_key: str | None, request_fingerprint: str
    ) -> ActionReceipt | None:
        if idempotency_key is None:
            return None
        self._validate_idempotency_key(idempotency_key)
        with self._lock:
            receipt_id = self._idempotency.get(idempotency_key)
            if receipt_id is None:
                return None
            receipt = self._actions[receipt_id]
            if receipt.request_fingerprint != request_fingerprint:
                raise ProtocolError(
                    ErrorCode.ARTIFACT_CONFLICT,
                    "idempotency key was reused for a different action",
                )
            return receipt

    def record_action(
        self,
        *,
        adapter_id: str,
        resource: str,
        action: str,
        target_ref: str | None,
        expected_revision: str | None,
        changed: bool | None,
        side_effect_state: SideEffectState | str,
        result: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        native_revision: str | None = None,
    ) -> ActionReceipt:
        effect = SideEffectState(side_effect_state)
        canonical_fingerprint(result or {})
        if idempotency_key is not None:
            self._validate_idempotency_key(idempotency_key)
            if not request_fingerprint:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "idempotent action requires a request fingerprint",
                )
            replay = self.replay_action(idempotency_key, request_fingerprint)
            if replay is not None:
                return replay
        with self._lock:
            before = self.assert_revision(adapter_id, resource, expected_revision)
            if target_ref is not None:
                self.resolve_ref(target_ref, adapter_id=adapter_id, resource=resource)
            if effect is SideEffectState.UNKNOWN:
                after = self.advance_revision(adapter_id, resource)
                self._native_revisions.pop(self._key(adapter_id, resource), None)
            elif effect is SideEffectState.APPLIED or changed:
                after = (
                    self.synchronize_revision(adapter_id, resource, native_revision)
                    if native_revision is not None
                    else self.advance_revision(adapter_id, resource)
                )
            else:
                after = before
            if effect in {SideEffectState.APPLIED, SideEffectState.UNKNOWN} or changed:
                self.advance_dependency_revision(adapter_id, resource)
            receipt = ActionReceipt(
                receipt_id=self._new_id("act"),
                adapter_id=adapter_id,
                resource=resource,
                action=action,
                target_ref=target_ref,
                before_revision=before,
                after_revision=after,
                changed=changed,
                side_effect_state=effect,
                idempotency_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                result=dict(result or {}),
                observed_at=utc_now(),
            )
            self._actions[receipt.receipt_id] = receipt
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = receipt.receipt_id
            self._trim_actions()
            return receipt

    def get_action(self, receipt_id: str) -> ActionReceipt:
        with self._lock:
            receipt = self._actions.get(receipt_id)
        if receipt is None:
            raise ProtocolError(ErrorCode.NOT_FOUND, "action receipt was not found")
        return receipt

    def get_verification(self, receipt_id: str) -> VerificationReceipt:
        with self._lock:
            receipt = self._verifications.get(receipt_id)
        if receipt is None:
            raise ProtocolError(
                ErrorCode.NOT_FOUND, "verification receipt was not found"
            )
        return receipt

    def verification_is_current(self, receipt_id: str) -> bool:
        receipt = self.get_verification(receipt_id)
        if not receipt.passed:
            return False
        dependencies = receipt.evidence.get("internal_dependencies", ())
        if not isinstance(dependencies, (list, tuple)) or not dependencies:
            return False
        for dependency in dependencies:
            if not isinstance(dependency, Mapping):
                return False
            adapter_id = dependency.get("adapter_id")
            resource = dependency.get("resource")
            revision = dependency.get("revision")
            if not all(
                isinstance(value, str) and value
                for value in (adapter_id, resource, revision)
            ):
                return False
            if self.dependency_revision(adapter_id, resource) != revision:
                return False
        return True

    def uncertain_actions(self) -> list[ActionReceipt]:
        with self._lock:
            return [
                receipt
                for receipt in self._actions.values()
                if receipt.side_effect_state is SideEffectState.UNKNOWN
                and receipt.receipt_id not in self._reconciliations_by_action
            ]

    def reconcile_action(
        self,
        *,
        action_receipt_id: str,
        verification_id: str,
        outcome: SideEffectState | str,
    ) -> ActionReconciliationReceipt:
        """Resolve UNKNOWN bookkeeping from current evidence without replaying."""

        try:
            resolved = SideEffectState(outcome)
        except ValueError as error:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "reconciliation outcome must be none or applied",
            ) from error
        if resolved not in {SideEffectState.NONE, SideEffectState.APPLIED}:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "reconciliation outcome must be none or applied",
            )
        with self._lock:
            action = self.get_action(action_receipt_id)
            if action.side_effect_state is not SideEffectState.UNKNOWN:
                raise ProtocolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "only an uncertain action receipt can be reconciled",
                )
            prior_id = self._reconciliations_by_action.get(action_receipt_id)
            if prior_id is not None:
                raise ProtocolError(
                    ErrorCode.ARTIFACT_CONFLICT,
                    "action receipt already has an immutable reconciliation",
                    candidates=({"reconciliation_id": prior_id},),
                )
            verification = self.get_verification(verification_id)
            if verification.action_receipt_id != action_receipt_id:
                raise ProtocolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "verification is not linked to the exact action receipt",
                )
            if not self.verification_is_current(verification_id):
                raise ProtocolError(
                    ErrorCode.PRECONDITION_FAILED,
                    "reconciliation requires a current passing verification",
                )
            receipt = ActionReconciliationReceipt(
                receipt_id=self._new_id("recon"),
                action_receipt_id=action_receipt_id,
                verification_id=verification_id,
                verification_fingerprint=canonical_fingerprint(
                    verification.to_dict()
                ),
                outcome=resolved,
                observed_at=utc_now(),
            )
            self._action_reconciliations[receipt.receipt_id] = receipt
            self._reconciliations_by_action[action_receipt_id] = receipt.receipt_id
            return receipt

    def action_reconciliations(self) -> list[ActionReconciliationReceipt]:
        with self._lock:
            return list(self._action_reconciliations.values())

    def receipt_counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "observations": len(self._observations),
                "actions": len(self._actions),
                "verifications": len(self._verifications),
                "action_reconciliations": len(self._action_reconciliations),
                "data_handles": len(self.active_data_handles()),
                "uncertain_actions": len(self.uncertain_actions()),
            }

    def record_verification(
        self,
        *,
        adapter_id: str,
        resource: str | None,
        revision: str | None,
        passed: bool,
        assertion: Mapping[str, Any],
        evidence: Mapping[str, Any],
        action_receipt_id: str | None = None,
    ) -> VerificationReceipt:
        with self._lock:
            if resource is not None:
                self.assert_revision(adapter_id, resource, revision)
            if action_receipt_id is not None:
                self.get_action(action_receipt_id)
            receipt = VerificationReceipt(
                receipt_id=self._new_id("ver"),
                adapter_id=adapter_id,
                resource=resource or "multi_resource",
                revision=revision,
                passed=bool(passed),
                assertion=dict(assertion),
                evidence=dict(evidence),
                action_receipt_id=action_receipt_id,
                observed_at=utc_now(),
            )
            self._verifications[receipt.receipt_id] = receipt
            self._trim(self._verifications)
            return receipt

    def issue_evidence_id(self) -> str:
        """Create an opaque ID for evidence retained by an adapter/kernel layer."""

        with self._lock:
            return self._new_id("ev")

    def _validate_idempotency_key(self, key: str) -> None:
        if (
            not isinstance(key, str)
            or not key
            or len(key) > self.limits.max_idempotency_key_chars
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid idempotency key")

    def _trim(self, values: dict[str, Any]) -> None:
        while len(values) > self.limits.max_receipts:
            oldest = next(iter(values))
            values.pop(oldest, None)

    def _trim_actions(self) -> None:
        while len(self._actions) > self.limits.max_receipts:
            oldest = next(iter(self._actions))
            receipt = self._actions.pop(oldest)
            reconciliation_id = self._reconciliations_by_action.pop(
                receipt.receipt_id, None
            )
            if reconciliation_id is not None:
                self._action_reconciliations.pop(reconciliation_id, None)
            if receipt.idempotency_key is not None:
                self._idempotency.pop(receipt.idempotency_key, None)


class EpisodeStore:
    def __init__(self) -> None:
        self._episodes: dict[str, EpisodeState] = {}
        self._lock = RLock()

    def create(self, episode_id: str, *, max_tool_calls: int) -> EpisodeState:
        with self._lock:
            if episode_id in self._episodes:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    f"semantic state already exists for episode: {episode_id}",
                )
            state = EpisodeState(episode_id, max_tool_calls=max_tool_calls)
            self._episodes[episode_id] = state
            return state

    def get(self, episode_id: str) -> EpisodeState:
        with self._lock:
            state = self._episodes.get(episode_id)
        if state is None:
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                f"semantic state does not exist for episode: {episode_id}",
            )
        return state

    def delete(self, episode_id: str) -> bool:
        with self._lock:
            return self._episodes.pop(episode_id, None) is not None
