"""Per-episode dispatcher for the canonical Semantic Computer Protocol v1.

This module is deliberately evaluator-blind.  It receives only generic adapter
capabilities and live application state, and it never imports task or evaluator
modules.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .adapters import (
    AdapterActionResult,
    AdapterContext,
    AdapterObservation,
    AdapterRegistry,
    CapabilitySet,
    SemanticAdapter,
)
from .interpreter import SemanticInterpreter
from .models import validate_payload, validate_request, validate_response
from .protocol import (
    ErrorCode,
    ProtocolError,
    RequestEnvelope,
    SideEffectState,
    Status,
    error_response,
    ok_response,
)
from .query import QueryEngine, QueryPage
from .state import EpisodeState, canonical_fingerprint
from .source_artifact import PublicSourceArtifactStager, source_provenance
from .verify import VerificationEngine


KERNEL_ADAPTER_ID = "semantic.kernel@1"
SYSTEM_RESOURCES = frozenset({
    "system.capabilities",
    "system.capability",
    "system.surfaces",
    "system.health",
    "system.pending_state",
    "system.data_handle",
})

VERIFY_OPERATORS = (
    "exists", "absent", "eq", "ne", "contains", "matches", "count",
    "approx", "parseable",
)

_TARGET_PROVENANCE_ACTIONS = frozenset({"invoke", "submit"})
_TARGET_PROVENANCE_EXECUTION_PATHS = frozenset({"accessibility", "semantic_input"})
_GUEST_PUSHDOWN_FIELD_PART = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_GUEST_UI_STRING_FIELDS = frozenset({"role", "name", "description", "text"})


def _guest_where_pushdown(
    where: Any,
    *,
    contains_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Translate a safe canonical-filter subset to the guest wire dialect.

    Guest filtering is only a transport optimization. The QueryEngine always
    applies the original canonical expression again. Consequently a pushed
    predicate must be equal to, or a superset of, the requested result set;
    unsupported branches fall back instead of risking false negatives.

    Scalar ``eq`` on mapping-only field paths is exact. A caller may also name
    fields whose adapter contract guarantees string values: guest case-folded
    ``contains`` is then a superset of canonical case-sensitive substring
    matching, and the canonical outer pass removes its false positives.
    """

    nodes = 0

    def translate(raw: Any, depth: int) -> tuple[dict[str, Any] | None, bool]:
        nonlocal nodes
        nodes += 1
        if nodes > 512 or depth > 32 or not isinstance(raw, Mapping):
            return None, False
        operation = raw.get("op")
        if operation == "eq":
            if set(raw) != {"op", "field", "value"}:
                return None, False
            field = raw.get("field")
            value = raw.get("value")
            parts = field.split(".") if isinstance(field, str) else []
            safe_value = (
                isinstance(value, (str, int, float, bool))
                and value is not None
                and not (isinstance(value, float) and not math.isfinite(value))
            )
            if (
                not parts
                or not all(_GUEST_PUSHDOWN_FIELD_PART.fullmatch(part) for part in parts)
                or not safe_value
            ):
                return None, False
            return {"field": field, "eq": value}, True
        if operation == "contains":
            if set(raw) != {"op", "field", "value"}:
                return None, False
            field = raw.get("field")
            value = raw.get("value")
            if (
                not isinstance(field, str)
                or field not in contains_fields
                or not isinstance(value, str)
            ):
                return None, False
            # Python string case-folding is concatenation-preserving: every
            # exact substring remains a substring after case-folding. The
            # guest may include additional case-insensitive matches, never
            # exclude a canonical match.
            return {"field": field, "contains": value}, False
        if operation in {"all", "any"}:
            if set(raw) != {"op", "filters"}:
                return None, False
            filters = raw.get("filters")
            if not isinstance(filters, (list, tuple)) or not 1 <= len(filters) <= 128:
                return None, False
            translated = [translate(child, depth + 1) for child in filters]
            if operation == "any" and any(child is None for child, _exact in translated):
                # Omitting one OR branch could omit true matches.
                return None, False
            available = [child for child, _exact in translated if child is not None]
            if not available:
                return None, False
            exact = len(available) == len(translated) and all(
                child_exact for _child, child_exact in translated
            )
            return {operation: available}, exact
        if operation == "not":
            if set(raw) != {"op", "filter"}:
                return None, False
            child, exact = translate(raw.get("filter"), depth + 1)
            # Negating a safe superset would create a subset and miss matches.
            if child is None or not exact:
                return None, False
            return {"not": child}, True
        return None, False

    translated, _exact = translate(where, 0)
    return translated or {}

def _now() -> float:
    return time.monotonic()


def _bounded_query_value(value: Any, *, depth: int = 0) -> Any:
    """Keep decision-bearing record fields while bounding provider payloads."""

    if depth > 16:
        return "[truncated: nesting limit]"
    if isinstance(value, str):
        return value if len(value) <= 2_000 else value[:1_972] + "...[truncated field]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_query_value(item, depth=depth + 1)
            for key, item in list(value.items())[:512]
        }
    if isinstance(value, (list, tuple)):
        return [
            _bounded_query_value(item, depth=depth + 1)
            for item in list(value)[:512]
        ]
    return value


def _fit_query_record(
    record: Mapping[str, Any], *, max_chars: int = 5_500
) -> dict[str, Any]:
    """Fit one useful semantic record inside a serialized character budget.

    Overflow results remain paginated behind a data handle, but the inline
    preview must still be sufficient to choose the next query or action.  A
    fixed ref/name skeleton is not sufficient for text, cell, capability, or
    UI records.  Preserve fields in a stable decision-oriented order and trim
    only the first collection that cannot fit.
    """

    bounded = _bounded_query_value(record)
    if not isinstance(bounded, dict):  # pragma: no cover - Mapping above
        return {}
    priority = (
        "ref", "kind", "collection_handle", "capability_type", "resource",
        "adapter_id", "name",
        "role", "text", "value", "display", "formula", "path", "sha256",
        "url", "title", "states", "advertised_actions", "actions",
        "resources", "revision", "source", "freshness",
    )
    keys = [key for key in priority if key in bounded]
    keys.extend(key for key in bounded if key not in keys)
    fitted: dict[str, Any] = {}

    def fits(candidate: Mapping[str, Any]) -> bool:
        return len(json.dumps(
            candidate, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False,
        )) <= max_chars

    for key in keys:
        value = bounded[key]
        candidate = {**fitted, key: value}
        if fits(candidate):
            fitted[key] = value
            continue
        if isinstance(value, list):
            prefix: list[Any] = []
            for item in value:
                item_candidate = {
                    **fitted, key: [*prefix, item], f"{key}_truncated": True,
                }
                if not fits(item_candidate):
                    break
                prefix.append(item)
            if prefix:
                fitted[key] = prefix
            fitted[f"{key}_truncated"] = True
        elif isinstance(value, Mapping):
            prefix_mapping: dict[str, Any] = {}
            for item_key, item in value.items():
                item_candidate = {
                    **fitted,
                    key: {**prefix_mapping, item_key: item},
                    f"{key}_truncated": True,
                }
                if not fits(item_candidate):
                    break
                prefix_mapping[item_key] = item
            if prefix_mapping:
                fitted[key] = prefix_mapping
            fitted[f"{key}_truncated"] = True
        else:
            fitted[f"{key}_truncated"] = True
        if not fits(fitted):
            fitted.pop(f"{key}_truncated", None)
        # Once the record budget is exhausted, later fields cannot add useful
        # detail. Identity and decision-bearing fields have already had first
        # claim on the budget through the priority ordering above.
        if len(json.dumps(fitted, ensure_ascii=False)) >= max_chars - 128:
            break
    return fitted


def _public_error(error: ProtocolError) -> dict[str, Any]:
    return error.to_dict()


def _agent_error(payload: Mapping[str, Any], *, during_action: bool = False) -> ProtocolError:
    raw = payload.get("error") if isinstance(payload, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}
    try:
        code = ErrorCode(str(raw.get("code") or "internal_error"))
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    raw_side_effect = str(raw.get("side_effect_state") or "none")
    unknown = during_action and (
        code is ErrorCode.UNCERTAIN
        or raw_side_effect == "unknown"
        or code in {
        ErrorCode.TIMEOUT, ErrorCode.ADAPTER_UNAVAILABLE, ErrorCode.INTERNAL_ERROR,
        }
    )
    if raw_side_effect == "applied":
        side_effect_state = SideEffectState.APPLIED
    elif unknown:
        side_effect_state = SideEffectState.UNKNOWN
    else:
        side_effect_state = SideEffectState.NONE
    return ProtocolError(
        ErrorCode.UNCERTAIN if unknown else code,
        str(raw.get("message") or "semantic guest operation failed")[:2_000],
        retryable=bool(raw.get("retryable", False)) and not unknown,
        side_effect_state=side_effect_state,
        missing_capability=(
            str(raw["missing_capability"])
            if raw.get("missing_capability") is not None else None
        ),
    )


class GuestProxyAdapter(SemanticAdapter):
    """One authoritative namespace backed by the versioned guest daemon."""

    def __init__(
        self,
        descriptor: Mapping[str, Any],
        request: Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]],
    ) -> None:
        adapter_id = descriptor.get("adapter_id")
        resources = descriptor.get("resources")
        actions = descriptor.get("actions", ())
        execution_paths = descriptor.get("execution_paths", ("native_api",))
        if not isinstance(adapter_id, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "guest adapter_id must be a string")
        if not isinstance(resources, (list, tuple)) or not all(
            isinstance(value, str) for value in resources
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "guest resources must be an array")
        if not isinstance(actions, (list, tuple)) or not all(
            isinstance(value, str) for value in actions
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "guest actions must be an array")
        if not isinstance(execution_paths, (list, tuple)) or not all(
            isinstance(value, str) for value in execution_paths
        ):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "guest execution_paths must be an array"
            )
        self.adapter_id = adapter_id
        self.resources = frozenset(resources)
        self.capabilities = CapabilitySet(
            actions
        )
        self._descriptor = dict(descriptor)
        self._request = request
        self.application = str(descriptor.get("application") or "guest")
        supported_versions = descriptor.get("supported_versions", ("*",))
        if not isinstance(supported_versions, (list, tuple)) or not all(
            isinstance(value, str) for value in supported_versions
        ):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "guest supported_versions must be an array"
            )
        self.supported_versions = tuple(supported_versions)
        self.execution_paths = tuple(execution_paths)
        raw_resource_schemas = descriptor.get("resource_schemas") or {}
        raw_resource_field_schemas = descriptor.get("resource_field_schemas") or {}
        raw_resource_actions = descriptor.get("resource_actions") or {}
        raw_action_schemas = descriptor.get("action_schemas") or {}
        risk_classes = descriptor.get("risk_classes") or {}
        idempotent_actions = descriptor.get("idempotent_actions", ())
        gaps = descriptor.get("known_representation_gaps", ())
        if not all(
            isinstance(value, Mapping)
            for value in (
                raw_resource_schemas, raw_resource_field_schemas,
                raw_resource_actions, raw_action_schemas, risk_classes,
            )
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "guest schemas must be objects")
        if not all(isinstance(value, Mapping) for value in raw_resource_schemas.values()):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "guest resource schema entries must be objects"
            )
        if not all(isinstance(value, Mapping) for value in raw_action_schemas.values()):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "guest action schema entries must be objects"
            )
        if not all(isinstance(value, Mapping) for value in raw_resource_field_schemas.values()):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "guest resource field schema entries must be objects",
            )
        if not all(
            isinstance(value, (list, tuple))
            and all(isinstance(action, str) for action in value)
            for value in raw_resource_actions.values()
        ):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "guest resource action entries must be arrays",
            )
        if not isinstance(idempotent_actions, (list, tuple)) or not all(
            isinstance(value, str) for value in idempotent_actions
        ):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "guest idempotent_actions must be an array"
            )
        if not isinstance(gaps, (list, tuple)) or not all(
            isinstance(value, Mapping) for value in gaps
        ):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "guest representation gaps must be typed objects",
            )
        self.resource_schemas = {
            str(key): dict(value)
            for key, value in raw_resource_schemas.items()
            if isinstance(value, Mapping)
        }
        self.resource_field_schemas = {
            str(key): dict(value)
            for key, value in raw_resource_field_schemas.items()
            if isinstance(value, Mapping)
        }
        self.resource_actions = {
            str(key): tuple(str(action) for action in value)
            for key, value in raw_resource_actions.items()
            if isinstance(value, (list, tuple))
        }
        idempotent = frozenset(
            idempotent_actions
        )
        self.action_schemas = {
            action: {
                "arguments_schema": dict(raw_action_schemas.get(action) or {}),
                "risk": str(risk_classes.get(action) or (
                    "external" if action in {"send", "submit", "publish", "purchase"}
                    else "persistent" if action in {
                        "create_directory", "copy", "move", "rename", "write_text",
                        "write_base64_atomic", "create_desktop_entry", "save", "save_as",
                        "export", "download", "set_setting", "write_clipboard", "create",
                        "update", "delete", "install", "enable", "disable", "uninstall",
                        "load_unpacked", "save_pdf", "create_shortcut", "clear_history",
                        "create_bookmark", "update_bookmark", "move_bookmark",
                        "delete_bookmark", "set_pref", "delete_history",
                        "enable_extension", "disable_extension", "uninstall_extension",
                    } else "reversible"
                )),
                "idempotent": action in idempotent,
                "execution_paths": list(self.execution_paths),
            }
            for action in self.capabilities
        }
        self.known_representation_gaps = tuple(
            dict(value) for value in gaps
        )
        accepts_target = descriptor.get(
            "accepts_entity_target", self.adapter_id == "universal-atspi@1"
        )
        if not isinstance(accepts_target, bool):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "accepts_entity_target must be boolean"
            )
        self.accepts_entity_target = accepts_target
        patch_hash = descriptor.get("patch_hash")
        self.patch_hash = str(patch_hash) if patch_hash is not None else None

    @property
    def descriptor_record(self) -> dict[str, Any]:
        return self.descriptor()

    def observe(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterObservation:
        request = dict(payload)
        # Filtering, ordering, projection and pagination are canonical kernel
        # responsibilities. A provably safe subset may also be pushed into the
        # guest to avoid transporting thousands of irrelevant AT-SPI nodes;
        # the canonical kernel still applies the original expression. Guest
        # paging remains private transport paging over that safe superset.
        pushed_where = _guest_where_pushdown(
            payload.get("where") or {},
            contains_fields=(
                _GUEST_UI_STRING_FIELDS
                if context.resource == "ui.elements"
                else frozenset()
            ),
        )
        request["where"] = pushed_where
        request["fields"] = []
        request["order_by"] = []
        request["cursor"] = None
        request["limit"] = 100
        records: list[dict[str, Any]] = []
        internal_offset = 0
        seen_offsets: set[int] = set()
        native_revision: str | None = None
        guest_total: int | None = None
        while True:
            if internal_offset in seen_offsets:
                raise ProtocolError(
                    ErrorCode.INTERNAL_ERROR, "guest query paging repeated an offset"
                )
            seen_offsets.add(internal_offset)
            page_request = {**request, "internal_offset": internal_offset}
            response = self._request("POST", "/v1/query", page_request)
            if not response.get("ok"):
                raise _agent_error(response)
            result = response.get("result")
            if not isinstance(result, Mapping):
                raise ProtocolError(ErrorCode.INTERNAL_ERROR, "guest query returned no result")
            page_records = result.get("records", ())
            if not isinstance(page_records, (list, tuple)) or not all(
                isinstance(record, Mapping) for record in page_records
            ):
                raise ProtocolError(ErrorCode.INTERNAL_ERROR, "guest query records are invalid")
            page_revision = str(result.get("revision") or "") or None
            if native_revision is None:
                native_revision = page_revision
            elif page_revision is not None and page_revision != native_revision:
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "guest resource changed while its semantic snapshot was paged",
                    retryable=True,
                )
            records.extend(dict(record) for record in page_records)
            if len(records) > 5_000:
                raise ProtocolError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    "guest semantic collection exceeds 5000 items",
                )
            raw_total = result.get("total")
            if isinstance(raw_total, int) and not isinstance(raw_total, bool):
                guest_total = raw_total
            next_offset = result.get("next_internal_offset")
            if next_offset is None:
                if result.get("truncated") is True:
                    raise ProtocolError(
                        ErrorCode.ADAPTER_UNAVAILABLE,
                        "guest truncated a resource without a private continuation",
                        retryable=True,
                    )
                break
            if (
                not isinstance(next_offset, int)
                or isinstance(next_offset, bool)
                or next_offset <= internal_offset
            ):
                raise ProtocolError(
                    ErrorCode.INTERNAL_ERROR, "guest returned an invalid private continuation"
                )
            internal_offset = next_offset
        return AdapterObservation(
            items=tuple(records),
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            summary={
                "guest_total": guest_total if guest_total is not None else len(records),
                "guest_pages": len(seen_offsets),
                "guest_truncated": False,
                "guest_filter_pushdown": bool(pushed_where),
            },
            native_revision=native_revision or canonical_fingerprint(records),
        )

    def act(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterActionResult:
        request = dict(payload)
        if not self.accepts_entity_target:
            # Native adapters act from typed arguments; their model-facing
            # target is the capability/owner, not a guest accessibility ref.
            request.pop("target", None)
        response = self._request("POST", "/v1/act", request)
        if not response.get("ok"):
            raise _agent_error(response, during_action=True)
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ProtocolError(
                ErrorCode.UNCERTAIN,
                "guest action returned no result",
                side_effect_state=SideEffectState.UNKNOWN,
            )
        return AdapterActionResult(
            changed=True,
            result=dict(result),
            provenance=({"source": self.adapter_id, "freshness": "live"},),
            status=Status.OK,
        )


class LibreOfficeGuestProxyAdapter(GuestProxyAdapter):
    """Guest LibreOffice bridge with public-source artifact composition.

    The guest remains authoritative for OXT parsing and unopkg registry truth.
    This outer layer only supplies the missing, generic public URL -> bounded
    guest artifact edge. No task, extension name, or known URL is embedded.
    """

    def __init__(
        self,
        descriptor: Mapping[str, Any],
        request: Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]],
        *,
        source_stager: PublicSourceArtifactStager | None = None,
    ) -> None:
        enhanced = dict(descriptor)
        raw_schemas = descriptor.get("action_schemas") or {}
        schemas = {
            str(key): dict(value)
            for key, value in raw_schemas.items()
            if isinstance(value, Mapping)
        }
        schemas["install_extension"] = {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute guest path to a validated local .oxt package",
                },
                "source_url": {
                    "type": "string",
                    "maxLength": 8_192,
                    "description": (
                        "Public HTTP(S) URL of one .oxt package; downloaded through "
                        "a bounded SSRF-safe stream before exact registry verification"
                    ),
                },
            },
            "oneOf": [
                {"required": ["path"]},
                {"required": ["source_url"]},
            ],
            "additionalProperties": False,
            "description": "Supply exactly one of path or source_url",
        }
        enhanced["action_schemas"] = schemas
        super().__init__(enhanced, request)
        self._source_stager = source_stager or PublicSourceArtifactStager(request)

    def validate_arguments(self, action: str, arguments: Mapping[str, Any]) -> None:
        super().validate_arguments(action, arguments)
        if action != "install_extension":
            return
        supplied = {key for key in ("path", "source_url") if key in arguments}
        if len(supplied) != 1:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "install_extension requires exactly one of path or source_url",
            )
        value = arguments[next(iter(supplied))]
        if not isinstance(value, str) or not value or len(value) > 8_192:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                f"install_extension {next(iter(supplied))} is invalid",
            )

    def act(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterActionResult:
        arguments = payload.get("arguments") or {}
        source_url = arguments.get("source_url") if isinstance(arguments, Mapping) else None
        if not isinstance(source_url, str):
            return super().act(context, payload)

        staged = self._source_stager.stage_libreoffice_extension(source_url)
        cleanup_error: ProtocolError | None = None
        try:
            applied = super().act(context, {
                **dict(payload),
                "arguments": {"path": staged.path},
            })
        finally:
            try:
                self._source_stager.remove(staged)
            except ProtocolError as error:
                cleanup_error = error
        if cleanup_error is not None:
            raise ProtocolError(
                ErrorCode.POSTCONDITION_FAILED,
                "extension was installed but its private staged artifact could not be cleaned up",
                side_effect_state=SideEffectState.APPLIED,
                candidates=({"cleanup_error": cleanup_error.to_dict()},),
            )
        installed = dict(applied.result)
        installed["source_artifact"] = {
            "requested_url": staged.requested_url,
            "final_url": staged.final_url,
            "http_status": staged.http_status,
            "fetched_at": staged.fetched_at,
            "redirect_chain": list(staged.redirect_chain),
            "content_hash": staged.sha256,
            "size": staged.size,
            "content_type": staged.content_type,
            "staged_artifact_removed": True,
        }
        return AdapterActionResult(
            changed=applied.changed,
            result=installed,
            provenance=tuple(applied.provenance) + source_provenance(staged, installed),
            status=applied.status,
            native_revision=applied.native_revision,
        )


class KernelSystemAdapter(SemanticAdapter):
    adapter_id = KERNEL_ADAPTER_ID
    resources = SYSTEM_RESOURCES
    capabilities = frozenset()

    def __init__(self, runtime: "SemanticRuntime") -> None:
        self.runtime = runtime

    def observe(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterObservation:
        resource = context.resource
        provenance: tuple[Mapping[str, Any], ...] = (
            {"source": self.adapter_id, "freshness": "live"},
        )
        native_revision: str | None = None
        if resource == "system.capabilities":
            records = self.runtime.capability_records()
        elif resource == "system.capability":
            requested = (payload.get("parameters") or {}).get("resource")
            scope_ref = (payload.get("scope") or {}).get("ref")
            if isinstance(scope_ref, str):
                resolved = self.runtime.state.resolve_ref(scope_ref)
                if (
                    resolved.adapter_id != self.adapter_id
                    or resolved.resource not in {
                        "system.capabilities", "system.capability",
                    }
                ):
                    raise ProtocolError(
                        ErrorCode.STALE_REF,
                        "ref is not a semantic capability record",
                    )
                capability_key = resolved.locator.get("native_ref")
                records = self.runtime.capability_detail_records(capability_key)
            elif isinstance(requested, str):
                records = self.runtime.capability_detail_records(requested)
            else:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "system.capability requires scope.ref or parameters.resource",
                )
            if not records:
                raise ProtocolError(
                    ErrorCode.UNKNOWN_RESOURCE,
                    str(requested or scope_ref or "capability"),
                )
        elif resource == "system.health":
            records = [{
                "ok": True,
                "protocol_version": "1.0",
                "semantic_operations": self.runtime.state.semantic_operations,
                "semantic_budget": self.runtime.state.semantic_budget,
                "adapters": self.runtime.registry.health(),
                "representation_gaps": list(self.runtime.representation_gaps),
            }]
        elif resource == "system.pending_state":
            guest_pending: Mapping[str, Any] = {}
            try:
                response = self.runtime._guest_request(
                    "POST", "/v1/query", {
                        "resource": "system.pending_state", "scope": {},
                        "where": {}, "fields": [], "order_by": [],
                        "parameters": {}, "limit": 30, "freshness": "live",
                    },
                )
                raw_result = response.get("result") if isinstance(response, Mapping) else None
                raw_records = raw_result.get("records") if isinstance(raw_result, Mapping) else None
                if isinstance(raw_records, (list, tuple)) and raw_records and isinstance(raw_records[0], Mapping):
                    guest_pending = raw_records[0]
            except Exception:
                # Kernel uncertainty remains authoritative even when optional
                # guest-side hazard reporting is temporarily unavailable.
                guest_pending = {}
            records = [{
                "uncertain_actions": [
                    receipt.receipt_id
                    for receipt in self.runtime.state.uncertain_actions()
                ],
                "modified_documents": list(guest_pending.get("modified_documents", ())),
                "running_exports": list(guest_pending.get("running_exports", ())),
                "pending_downloads": list(guest_pending.get("pending_downloads", ())),
                "live_disk_divergence": list(guest_pending.get("live_disk_divergence", ())),
                "action_reconciliations": [
                    receipt.to_dict()
                    for receipt in self.runtime.state.action_reconciliations()
                ],
            }]
        elif resource == "system.data_handle":
            scope = payload.get("scope") or {}
            handle = scope.get("ref") if isinstance(scope, Mapping) else None
            if not isinstance(handle, str):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "system.data_handle requires scope.ref",
                )
            try:
                stored = self.runtime.state.resolve_data_handle(handle)
            except ProtocolError as error:
                if error.code is not ErrorCode.NOT_FOUND:
                    raise
                adapter_observation = self.runtime.registry.resolve_data_handle(handle)
                records = [dict(record) for record in adapter_observation.items]
                provenance = tuple(adapter_observation.provenance)
                native_revision = adapter_observation.native_revision
            else:
                chunk_size = 384
                chunks = [
                    stored.serialized[index : index + chunk_size]
                    for index in range(0, len(stored.serialized), chunk_size)
                ] or [""]
                metadata = stored.public()
                records = [
                    {
                        "kind": "data_handle.chunk",
                        "data_handle": handle,
                        "index": index,
                        "chunk_count": len(chunks),
                        "text": text,
                        **({"metadata": metadata} if index == 0 else {}),
                    }
                    for index, text in enumerate(chunks)
                ]
        elif resource == "system.surfaces":
            records = self.runtime.observe_surface_records(payload)
        else:  # pragma: no cover - registry prevents this
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, resource)
        return AdapterObservation(
            items=tuple(records),
            provenance=provenance,
            # The capability catalog is immutable for an episode.  In
            # particular, querying a different detail record must not stale a
            # previously issued capability ref or its data handle.
            native_revision=(
                "immutable_data_handles_v1"
                if resource == "system.data_handle"
                else self.runtime.capability_catalog_revision()
                if resource in {"system.capabilities", "system.capability"}
                else native_revision or canonical_fingerprint(records)
            ),
        )

    def act(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterActionResult:
        raise ProtocolError(ErrorCode.UNSUPPORTED, "system resources are read-only")


@dataclass(frozen=True)
class CompletionResult:
    accepted: bool
    terminal: bool
    infeasible: bool
    warnings: tuple[str, ...] = ()
    error: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "terminal": self.terminal,
            "infeasible": self.infeasible,
            "warnings": list(self.warnings),
            "error": dict(self.error) if self.error is not None else None,
        }


class _RunComputer:
    def __init__(self, runtime: "SemanticRuntime") -> None:
        self.runtime = runtime

    @staticmethod
    def _request_payload(
        operation: str, args: tuple[Any, ...], kwargs: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Accept the same request fields as the outer semantic tools.

        Models naturally write either ``computer.query({...})`` or the more
        Pythonic ``computer.query(resource=..., scope=...)``.  The interpreter
        already allowed keyword syntax, but the runtime wrapper previously
        accepted only one positional mapping and converted the resulting
        ``TypeError`` into an opaque ``internal_error``.  Both spellings now
        normalize to the same canonical payload; mixing them remains an
        explicit, typed error.
        """

        if len(args) > 1:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                f"computer.{operation} accepts one request object or keyword fields",
            )
        if args and kwargs:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                f"computer.{operation} cannot mix a request object with keyword fields",
            )
        if args:
            payload = args[0]
            if not isinstance(payload, Mapping):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    f"computer.{operation} request must be an object",
                )
            return payload
        return dict(kwargs)

    def query(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = {
            "scope": {}, "order_by": [], "parameters": {}, "freshness": "live",
            **dict(self._request_payload("query", args, kwargs)),
        }
        if payload.get("fields") == ["*"]:
            payload["fields"] = []
        validated = validate_payload("query", payload)
        return self.runtime._query(validated, consume_budget=False)[0].to_dict()

    def act(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = {
            "arguments": {}, "preconditions": [], "postconditions": [],
            **dict(self._request_payload("act", args, kwargs)),
        }
        validated = validate_payload("act", payload)
        return self.runtime._act(validated, consume_budget=False)[0]

    def verify(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = self._request_payload("verify", args, kwargs)
        validated = validate_payload("verify", payload)
        return self.runtime._verify(validated, consume_budget=False).to_dict()


class SemanticRuntime:
    def __init__(
        self,
        *,
        episode_id: str,
        runtime_name: str = "semantic-v1",
        max_tool_calls: int,
        guest_request: Callable[
            [str, str, Mapping[str, Any] | None], Mapping[str, Any]
        ],
        guest_capabilities: Sequence[Mapping[str, Any]],
        representation_gaps: Sequence[Mapping[str, Any]] = (),
        adapters: Sequence[SemanticAdapter] = (),
    ) -> None:
        self.episode_id = episode_id
        self.runtime_name = runtime_name
        self.state = EpisodeState(episode_id, max_tool_calls=max_tool_calls)
        self.registry = AdapterRegistry()
        self.query_engine = QueryEngine()
        self.verify_engine = VerificationEngine()
        self.interpreter = SemanticInterpreter()
        self._guest_request = guest_request
        self.representation_gaps = tuple(dict(value) for value in representation_gaps)
        self.trace: list[dict[str, Any]] = []
        self.evidence_ids: set[str] = set()
        self._descriptors: dict[str, dict[str, Any]] = {}
        for descriptor in guest_capabilities:
            adapter = (
                LibreOfficeGuestProxyAdapter(descriptor, guest_request)
                if descriptor.get("adapter_id") == "libreoffice.uno@1"
                else GuestProxyAdapter(descriptor, guest_request)
            )
            self.registry.register(adapter)
            self._descriptors[adapter.adapter_id] = adapter.descriptor_record
        for adapter in adapters:
            self.registry.register(adapter)
            self._descriptors[adapter.adapter_id] = adapter.descriptor()
        system = KernelSystemAdapter(self)
        self.registry.register(system)
        system.known_representation_gaps = self.representation_gaps
        self._descriptors[system.adapter_id] = system.descriptor()

    @staticmethod
    def _capability_native_ref(capability_type: str, identifier: str) -> str:
        digest = hashlib.sha256(
            f"{capability_type}\0{identifier}".encode("utf-8")
        ).hexdigest()[:32]
        return f"capability_{capability_type}_{digest}"

    def capability_catalog_revision(self) -> str:
        return canonical_fingerprint(self._descriptors)

    def capability_records(self) -> list[dict[str, Any]]:
        """Return the compact discovery index, not whole adapter descriptors."""

        records: list[dict[str, Any]] = []
        # Put model-actionable resource records first.  The old adapter-first
        # order meant an unfiltered default query returned a page of framework
        # cards while hiding the resources the model could actually query.
        # Adapter descriptors remain available after the resource index and by
        # exact filtering; discovery is simply decision-shaped by default.
        for adapter_id in sorted(self._descriptors):
            descriptor = self._descriptors[adapter_id]
            resource_actions = descriptor.get("resource_actions") or {}
            for resource in sorted(str(value) for value in descriptor.get("resources", ())):
                actions = resource_actions.get(resource, descriptor.get("actions", ()))
                records.append({
                    "ref": self._capability_native_ref("resource", resource),
                    "kind": "system.capability",
                    "capability_type": "resource",
                    "name": resource,
                    "adapter_id": adapter_id,
                    "resource": resource,
                    # Keep the plural form for compatibility with the concise
                    # model prompt which tells the policy to use values from
                    # ``resources`` verbatim.
                    "resources": [resource],
                    "actions": sorted(str(value) for value in actions),
                    "description": "Queryable semantic resource; inspect this ref for its schemas",
                })
        for adapter_id in sorted(self._descriptors):
            descriptor = self._descriptors[adapter_id]
            resources = sorted(str(value) for value in descriptor.get("resources", ()))
            records.append({
                "ref": self._capability_native_ref("adapter", adapter_id),
                "kind": "system.capability",
                "capability_type": "adapter",
                "name": adapter_id,
                "adapter_id": adapter_id,
                "application": str(descriptor.get("application") or "generic"),
                "semantic_version": str(descriptor.get("semantic_version") or "unversioned"),
                "resources": resources,
                "description": f"Semantic adapter exposing {len(resources)} resources",
            })
        return records

    def capability_detail_records(self, key: Any) -> list[dict[str, Any]]:
        """Resolve one adapter or resource capability without catalog churn."""

        if not isinstance(key, str) or not key:
            return []
        for adapter_id in sorted(self._descriptors):
            descriptor = self._descriptors[adapter_id]
            adapter_ref = self._capability_native_ref("adapter", adapter_id)
            if key in {adapter_id, adapter_ref}:
                return [{
                    "ref": adapter_ref,
                    "kind": "system.capability",
                    "capability_type": "adapter_descriptor",
                    "name": adapter_id,
                    **dict(descriptor),
                }]
            resources = set(str(value) for value in descriptor.get("resources", ()))
            for resource in sorted(resources):
                resource_ref = self._capability_native_ref("resource", resource)
                if key not in {resource, resource_ref}:
                    continue
                resource_schemas = descriptor.get("resource_schemas") or {}
                field_schemas = descriptor.get("resource_field_schemas") or {}
                resource_actions = descriptor.get("resource_actions") or {}
                action_schemas = descriptor.get("action_schemas") or {}
                actions = sorted(str(value) for value in resource_actions.get(
                    resource, descriptor.get("actions", ())
                ))
                scoped_actions = {
                    action: dict(action_schemas[action])
                    for action in actions
                    if isinstance(action_schemas.get(action), Mapping)
                }
                return [{
                    "ref": resource_ref,
                    "kind": "system.capability",
                    "capability_type": "resource_descriptor",
                    "name": resource,
                    "adapter_id": adapter_id,
                    "application": str(descriptor.get("application") or "generic"),
                    "resource": resource,
                    "resources": [resource],
                    "field_schema": dict(field_schemas.get(resource) or {}),
                    "parameter_schema": dict(resource_schemas.get(resource) or {}),
                    "actions": actions,
                    "action_schemas": scoped_actions,
                    "verification_schema": {
                        "resource": resource,
                        "freshness": ["live"],
                        "composition": ["all", "any", "not"],
                        "operators": list(VERIFY_OPERATORS),
                    },
                }]
        return []

    def observe_surface_records(self, payload: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            adapter = self.registry.resolve("os.applications")
            observation = adapter.observe(
                AdapterContext(
                    self.episode_id, "os.applications", "system-surfaces", None
                ),
                {
                    "resource": "os.applications", "scope": {}, "where": {},
                    "fields": [], "order_by": [], "parameters": {}, "limit": 100,
                    "freshness": "live",
                },
            )
            return [dict(record) for record in observation.items]
        except ProtocolError:
            return []

    @staticmethod
    def _locator(record: Mapping[str, Any]) -> dict[str, Any]:
        native_ref = record.get("ref")
        if isinstance(native_ref, str) and native_ref:
            return {"native_ref": native_ref}
        for key in ("path", "pid", "id", "uri"):
            if key in record:
                return {key: record[key]}
        return {"record_hash": canonical_fingerprint(record)}

    @staticmethod
    def _fingerprint(record: Mapping[str, Any]) -> dict[str, Any]:
        fields = (
            "kind", "role", "name", "path", "state", "advertised_actions",
            "parent_ref", "child_count",
        )
        return {key: record[key] for key in fields if key in record}

    def _observe(
        self,
        adapter: SemanticAdapter,
        resource: str,
        payload: Mapping[str, Any],
        request_id: str,
    ) -> tuple[list[dict[str, Any]], str, AdapterObservation]:
        before = self.state.current_revision(adapter.adapter_id, resource)
        try:
            observation = adapter.query(
                AdapterContext(self.episode_id, resource, request_id, before), payload
            )
        except TimeoutError as error:
            raise ProtocolError(
                ErrorCode.TIMEOUT,
                "adapter query timed out",
                retryable=True,
                side_effect_state=SideEffectState.NONE,
            ) from error
        except ConnectionError as error:
            raise ProtocolError(
                ErrorCode.ADAPTER_UNAVAILABLE,
                "adapter transport is unavailable",
                retryable=True,
                side_effect_state=SideEffectState.NONE,
            ) from error
        query_scope = dict(payload.get("scope") or {})
        query_scope.pop("adapter", None)
        query_parameters = dict(payload.get("parameters") or {})
        entries = []
        for record in observation.items:
            locator = self._locator(record)
            # Scope and parameters are private capability state. They let an
            # action re-observe the exact path/range/surface which issued its
            # ref instead of accidentally querying a scoped adapter with an
            # empty scope. They are never included in RefRecord.public().
            # Capability records have deterministic private native refs and
            # are read-only.  Binding them to the public ref used to reach the
            # detail would make an otherwise identical capability acquire a
            # new ref on every dereference.
            if not (
                adapter.adapter_id == KERNEL_ADAPTER_ID
                and resource in {"system.capabilities", "system.capability"}
            ):
                locator["_query_scope"] = query_scope
                locator["_query_parameters"] = query_parameters
                locator["_query_where"] = dict(payload.get("where") or {})
            entries.append({
                "locator": locator,
                "fingerprint": self._fingerprint(record),
            })
        receipt, refs = self.state.record_observation(
            adapter_id=adapter.adapter_id,
            resource=resource,
            entries=entries,
            summary=observation.summary,
            native_revision=observation.native_revision,
        )
        native_to_public = {
            str(raw.get("ref")): ref.ref
            for raw, ref in zip(observation.items, refs)
            if isinstance(raw.get("ref"), str)
        }
        public: list[dict[str, Any]] = []
        for raw, ref in zip(observation.items, refs):
            record = dict(raw)
            record["ref"] = ref.ref
            for relationship in (
                "parent_ref", "owner_ref", "label_for_ref", "labelled_by_ref",
                "controller_for_ref",
            ):
                native = record.get(relationship)
                if isinstance(native, str):
                    record[relationship] = native_to_public.get(native)
            for relationship in ("child_refs", "owned_refs", "labelled_by_refs"):
                native_values = record.get(relationship)
                if isinstance(native_values, (list, tuple)):
                    record[relationship] = [
                        native_to_public[value]
                        for value in native_values
                        if isinstance(value, str) and value in native_to_public
                    ]
            # Never manufacture malformed resource names such as
            # ``system.capabilitie``. Adapter-supplied entity kinds remain
            # authoritative; otherwise preserve the exact queryable resource.
            record.setdefault("kind", resource)
            record.setdefault("states", record.pop("state", {}))
            record.setdefault("advertised_actions", [])
            record["revision"] = receipt.revision
            record["source"] = adapter.adapter_id
            record["freshness"] = "live"
            public.append(record)
        return public, receipt.revision, observation

    def _query(
        self,
        payload: Mapping[str, Any],
        *,
        consume_budget: bool,
        request_id: str = "internal-query",
    ) -> tuple[QueryPage, SemanticAdapter, AdapterObservation]:
        if not isinstance(payload, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "query payload must be an object")
        resource = payload.get("resource")
        scope = payload.get("scope")
        if not isinstance(resource, str) or not isinstance(scope, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "query requires resource and scope")
        requested_adapter = scope.get("adapter")
        if requested_adapter is not None and not isinstance(requested_adapter, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "scope.adapter must be a string")
        adapter = self.registry.resolve(resource, adapter_id=requested_adapter)
        parameters = payload.get("parameters")
        if not isinstance(parameters, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "query parameters must be an object")
        adapter.validate_parameters(resource, parameters)
        records, _revision, observation = self._observe(
            adapter, resource, payload, request_id
        )
        query_payload = dict(payload)
        if resource == "system.data_handle":
            # Keep serialized responses below the protocol envelope cap while
            # retaining ordinary opaque cursor semantics.
            query_payload["limit"] = min(int(payload.get("limit") or 30), 12)
        page = self.query_engine.query(
            state=self.state,
            adapter_id=adapter.adapter_id,
            resource=resource,
            items=records,
            payload=query_payload,
            consume_budget=consume_budget,
        )
        if resource != "system.data_handle":
            serialized_records = json.dumps(
                page.to_dict()["records"],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
            if len(serialized_records) > 8_000:
                handle = self.state.create_data_handle(
                    adapter_id=adapter.adapter_id,
                    resource=resource,
                    revision=page.revision,
                    value=page.to_dict()["records"],
                )
                compact: list[dict[str, Any]] = []
                compact_chars = 0
                for index, record in enumerate(page.records):
                    # The old overflow path kept only ref/kind/name.  That
                    # erased values, text, state, advertised actions, and even
                    # the `resource` field from capability discovery.  Keep a
                    # bounded complete record until the response budget is
                    # full; the data handle still preserves the entire page.
                    remaining = 6_000 - compact_chars
                    if remaining < 256:
                        break
                    bounded_record = _fit_query_record(
                        {**dict(record), "record_index": index},
                        max_chars=remaining,
                    )
                    if not bounded_record:
                        break
                    candidate_size = len(json.dumps(
                        bounded_record, ensure_ascii=False,
                        separators=(",", ":"), allow_nan=False,
                    ))
                    compact.append(bounded_record)
                    compact_chars += candidate_size
                page = QueryPage(
                    records=tuple(compact),
                    next_cursor=page.next_cursor,
                    truncated=True,
                    total=page.total,
                    revision=page.revision,
                    adapter_id=page.adapter_id,
                    resource=page.resource,
                    data_handle=handle.handle,
                )
        return page, adapter, observation

    @staticmethod
    def _predicate_to_verify(
        predicate: Mapping[str, Any], claim_id: str
    ) -> dict[str, Any]:
        if set(predicate) in ({"all"}, {"any"}):
            operator = next(iter(predicate))
            children = predicate[operator]
            if (
                not isinstance(children, (list, tuple))
                or not children
                or not all(isinstance(child, Mapping) for child in children)
            ):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "predicate children are invalid")
            return {
                operator: [
                    SemanticRuntime._predicate_to_verify(
                        child, f"{claim_id}-{index}"
                    )
                    for index, child in enumerate(children)
                ]
            }
        if set(predicate) == {"not"} and isinstance(predicate.get("not"), Mapping):
            return {
                "not": SemanticRuntime._predicate_to_verify(
                    predicate["not"], f"{claim_id}-not"
                )
            }
        required = {"resource", "scope", "assert"}
        if not required <= set(predicate):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "predicate is incomplete")
        return {
            "claim_id": claim_id,
            "query": {
                "resource": predicate["resource"],
                "scope": predicate["scope"],
                "where": predicate.get("where", {}),
                "fields": [],
                "order_by": [],
                "parameters": {},
                "limit": 100,
                "freshness": "live",
            },
            "assert": predicate["assert"],
        }

    def _check_predicates(
        self, predicates: Any, *, phase: str
    ) -> list[dict[str, Any]]:
        if not isinstance(predicates, (list, tuple)):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{phase}conditions must be an array")
        if not predicates:
            return []
        assertions = [
            self._predicate_to_verify(value, f"{phase}-{index}")
            for index, value in enumerate(predicates)
            if isinstance(value, Mapping)
        ]
        if len(assertions) != len(predicates):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid {phase}condition")
        result = self._verify(
            {"mode": "all", "assertions": assertions, "freshness": "live"},
            consume_budget=False,
        )
        if result.verdict != "pass":
            raise ProtocolError(
                ErrorCode.PRECONDITION_FAILED if phase == "pre" else ErrorCode.POSTCONDITION_FAILED,
                f"{phase}conditions were not proven",
            )
        return [dict(claim) for claim in result.claims]

    def _select_target(
        self, target: Mapping[str, Any]
    ) -> tuple[Any, SemanticAdapter, str]:
        if set(target) == {"ref"} and isinstance(target.get("ref"), str):
            ref = self.state.resolve_ref(str(target["ref"]))
            return ref, self.registry.get(ref.adapter_id), ref.resource
        if set(target) == {"resource", "scope", "where"}:
            page, adapter, _ = self._query(
                {
                    "resource": target["resource"], "scope": target["scope"],
                    "where": target["where"], "fields": [], "order_by": [],
                    "parameters": {}, "limit": 2, "freshness": "live",
                },
                consume_budget=False,
                request_id="action-selector",
            )
            if not page.records:
                raise ProtocolError(ErrorCode.NOT_FOUND, "semantic selector matched no target")
            if len(page.records) != 1 or page.truncated:
                raise ProtocolError(
                    ErrorCode.AMBIGUOUS,
                    "semantic selector did not identify exactly one target",
                    candidates=page.records[:2],
                )
            ref = self.state.resolve_ref(str(page.records[0]["ref"]))
            return ref, adapter, ref.resource
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST,
            "target must contain exactly ref or resource/scope/where",
        )

    def _browser_target_snapshot(
        self,
        *,
        previous_surface_ids: frozenset[str] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], SemanticAdapter | None]:
        """Capture the current browser surface set without issuing model refs.

        This read-only internal observation is intentionally tolerant of an
        absent or temporarily unavailable browser.  Cross-application action
        provenance is additive evidence; inability to observe Chrome must not
        turn an otherwise valid desktop action into a harness failure.
        """

        try:
            adapter = self.registry.resolve("browser.targets")
            private_snapshot = getattr(adapter, "target_snapshot", None)
            if callable(private_snapshot):
                items = private_snapshot(
                    previous_surface_ids=previous_surface_ids,
                    timeout_ms=2_500 if previous_surface_ids is not None else 0,
                )
            else:
                observation = adapter.query(
                    AdapterContext(
                        self.episode_id,
                        "browser.targets",
                        "action-target-provenance",
                        self.state.current_revision(adapter.adapter_id, "browser.targets"),
                    ),
                    {
                        "resource": "browser.targets", "scope": {}, "where": {},
                        "fields": [], "order_by": [], "parameters": {}, "limit": 100,
                        "freshness": "live",
                    },
                )
                items = observation.items
        except Exception:
            # Provenance must never turn a successfully routable UI action
            # into a harness failure. Absence of evidence is reported by
            # omitting the target delta rather than inventing one.
            return {}, None
        snapshot: dict[str, dict[str, Any]] = {}
        invalid_identity = False
        for record in items:
            surface_id = record.get("surface_id")
            if not isinstance(surface_id, str) or not surface_id:
                invalid_identity = True
                continue
            snapshot[surface_id] = {
                "surface_id": surface_id,
                "url": record.get("url") if isinstance(record.get("url"), str) else None,
            }
        if invalid_identity:
            return {}, None
        return snapshot, adapter

    def _browser_target_delta(
        self,
        before: Mapping[str, Mapping[str, Any]],
        *,
        adapter: SemanticAdapter | None,
        request_id: str,
    ) -> dict[str, Any]:
        """Prove browser target creation and materialize one ref if unique."""

        after, current_adapter = self._browser_target_snapshot(
            previous_surface_ids=frozenset(before)
        )
        if current_adapter is None:
            return {"browser_target_effect": {
                "before_surface_ids": sorted(before),
                "created_surface_count": None,
                "creation_verdict": "unknown",
            }}
        browser_adapter = current_adapter or adapter
        before_ids = sorted(before)
        after_ids = sorted(after)
        created_ids = sorted(set(after) - set(before))
        evidence: dict[str, Any] = {
            "before_surface_ids": before_ids,
            "after_surface_ids": after_ids,
            "created_surface_count": len(created_ids),
            "creation_verdict": (
                "one_created" if len(created_ids) == 1
                else "none_created" if not created_ids
                else "ambiguous_multiple_created"
            ),
        }
        if len(created_ids) != 1 or browser_adapter is None:
            return {"browser_target_effect": evidence}

        created_id = created_ids[0]
        evidence["created_surface_id"] = created_id
        target_url = after[created_id].get("url")
        if isinstance(target_url, str) and target_url and target_url != "about:blank":
            evidence["target_uri"] = target_url
        try:
            records, _revision, _observation = self._observe(
                browser_adapter,
                "browser.targets",
                {
                    "resource": "browser.targets", "scope": {}, "where": {},
                    "fields": [], "order_by": [], "parameters": {}, "limit": 100,
                    "freshness": "live",
                },
                request_id,
            )
        except Exception:
            return {"browser_target_effect": evidence}
        matches = [
            record for record in records
            if record.get("surface_id") == created_id
        ]
        if len(matches) != 1:
            # The set delta remains truthful, but a current unique entity ref
            # could not be established. Never guess among candidates.
            return {"browser_target_effect": evidence}

        record = matches[0]
        evidence["created_surface_ref"] = record["ref"]
        return {"browser_target_effect": evidence}

    def _act(
        self,
        payload: Mapping[str, Any],
        *,
        consume_budget: bool,
        request_id: str = "internal-act",
    ) -> tuple[dict[str, Any], SemanticAdapter, Sequence[Mapping[str, Any]]]:
        if consume_budget:
            self.state.consume_operation()
        if not isinstance(payload, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "act payload must be an object")
        target = payload.get("target")
        action = payload.get("action")
        arguments = payload.get("arguments")
        if not isinstance(target, Mapping) or not isinstance(action, str) or not isinstance(arguments, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "act requires target/action/arguments")
        request_fingerprint = canonical_fingerprint(payload)
        # Idempotency is a receipt capability, not a fresh entity action.  A
        # successful first mutation commonly invalidates the target's old ref;
        # resolve the stored receipt before touching that stale capability so
        # replay can never execute (or even re-observe) the action again.
        replay = self.state.replay_action(payload.get("idempotency_key"), request_fingerprint)
        if replay is not None:
            adapter = self.registry.get(replay.adapter_id)
            stored = dict(replay.result)
            result = {
                "status": "applied" if replay.changed else "no_effect",
                "execution_path": stored.get("execution_path", "native_api"),
                "receipt_id": replay.receipt_id,
                "before_revision": replay.before_revision,
                "after_revision": replay.after_revision,
                "delta": stored.get("delta", {}),
                "side_effects": stored.get("side_effects", []),
                "postconditions": stored.get("postconditions", []),
                "error": None,
            }
            return result, adapter, ()
        ref, adapter, resource = self._select_target(target)
        action_metadata = adapter.action_metadata(action)
        adapter.validate_arguments(action, arguments)
        if action_metadata.get("risk") in {"persistent", "external"} and payload.get("confirm") is not True:
            raise ProtocolError(
                ErrorCode.PERMISSION_DENIED,
                "persistent or external action requires confirm=true",
            )
        current = self.state.current_revision(adapter.adapter_id, resource)
        expected = payload.get("expected_revision")
        if expected is not None:
            self.state.assert_revision(adapter.adapter_id, resource, str(expected))
        preconditions = self._check_predicates(payload.get("preconditions", ()), phase="pre")

        capture_browser_targets = (
            action in _TARGET_PROVENANCE_ACTIONS
            and action_metadata.get("idempotent") is not True
        )
        browser_targets_before: dict[str, dict[str, Any]] = {}
        browser_target_adapter: SemanticAdapter | None = None
        if capture_browser_targets:
            browser_targets_before, browser_target_adapter = self._browser_target_snapshot()

        # Re-observe immediately before mutation and require the native locator
        # and action-relevant fingerprint to still resolve uniquely.
        issued_ref = ref
        originating_scope = dict(ref.locator.get("_query_scope") or {})
        originating_scope["adapter"] = adapter.adapter_id
        originating_parameters = dict(ref.locator.get("_query_parameters") or {})
        originating_where = dict(ref.locator.get("_query_where") or {})
        fresh_records, fresh_revision, _ = self._observe(adapter, resource, {
            "resource": resource, "scope": originating_scope,
            "where": originating_where, "fields": [], "order_by": [],
            "parameters": originating_parameters,
            "limit": 100, "freshness": "live",
        }, request_id)
        if expected is not None and fresh_revision != expected:
            raise ProtocolError(
                ErrorCode.REVISION_CONFLICT, "target revision changed before action", retryable=True
            )
        try:
            ref = self.state.resolve_ref(ref.ref, adapter_id=adapter.adapter_id, resource=resource)
        except ProtocolError as error:
            if error.code not in {ErrorCode.STALE_REF, ErrorCode.NOT_FOUND}:
                raise ProtocolError(
                    ErrorCode.STALE_REF, "target no longer resolves", retryable=True
                ) from error

            # A relevant surface revision invalidates the issued capability,
            # but stable native identities (and, secondarily, unique semantic
            # identity fields) may safely rebind it.  Never retarget by ordinal
            # position and never choose among multiple semantic matches.
            fresh_refs = []
            for record in fresh_records:
                public_ref = record.get("ref")
                if not isinstance(public_ref, str):
                    continue
                try:
                    fresh_refs.append((
                        record,
                        self.state.resolve_ref(
                            public_ref, adapter_id=adapter.adapter_id, resource=resource
                        ),
                    ))
                except ProtocolError:
                    continue
            old_native = issued_ref.locator.get("native_ref")
            native_matches = [
                candidate for _record, candidate in fresh_refs
                if old_native is not None
                and candidate.locator.get("native_ref") == old_native
            ]
            if len(native_matches) == 1:
                ref = native_matches[0]
            else:
                stable_fields = (
                    "kind", "role", "name", "path", "advertised_actions",
                    "child_count",
                )
                expected_identity = {
                    key: issued_ref.fingerprint[key]
                    for key in stable_fields if key in issued_ref.fingerprint
                }
                semantic_matches = [
                    candidate for record, candidate in fresh_refs
                    if expected_identity
                    and all(record.get(key) == value for key, value in expected_identity.items())
                ]
                if len(semantic_matches) != 1:
                    raise ProtocolError(
                        ErrorCode.STALE_REF,
                        "target no longer resolves uniquely; query the resource again",
                        retryable=True,
                        candidates=[
                            {"ref": candidate.ref}
                            for candidate in semantic_matches[:10]
                        ],
                    ) from error
                ref = semantic_matches[0]
        native_ref = ref.locator.get("native_ref")
        guest_target = {"ref": native_ref} if native_ref is not None else {}
        adapter_payload = {
            **dict(payload),
            "target": guest_target,
        }
        started = _now()
        try:
            applied = adapter.act(
                AdapterContext(self.episode_id, resource, request_id, fresh_revision),
                adapter_payload,
            )
            applied_status = Status(applied.status)
            if applied_status in {Status.UNCERTAIN, Status.PARTIAL}:
                raise ProtocolError(
                    ErrorCode.UNCERTAIN,
                    "adapter could not establish the final action state",
                    side_effect_state=SideEffectState.UNKNOWN,
                )
            if applied_status in {Status.REJECTED, Status.FAILED}:
                if applied.changed is True:
                    raise ProtocolError(
                        ErrorCode.POSTCONDITION_FAILED,
                        "adapter reported failure after applying a mutation",
                        side_effect_state=SideEffectState.APPLIED,
                    )
                if applied.changed is None:
                    raise ProtocolError(
                        ErrorCode.UNCERTAIN,
                        "adapter reported failure with unknown mutation state",
                        side_effect_state=SideEffectState.UNKNOWN,
                    )
                raise ProtocolError(
                    ErrorCode.NO_EFFECT,
                    "adapter rejected the action without changing state",
                    side_effect_state=SideEffectState.NONE,
                )
            post_observation = adapter.query(
                AdapterContext(self.episode_id, resource, request_id, fresh_revision),
                {
                    "resource": resource, "scope": originating_scope,
                    "where": originating_where, "fields": [], "order_by": [],
                    "parameters": originating_parameters,
                    "limit": 100, "freshness": "live",
                },
            )
            effect = SideEffectState.APPLIED if applied.changed else SideEffectState.NONE
            execution_path = str(applied.result.get("execution_path") or "native_api")
            delta = {
                key: value for key, value in applied.result.items()
                if key != "execution_path"
            }
            if (
                capture_browser_targets
                and execution_path in _TARGET_PROVENANCE_EXECUTION_PATHS
                and browser_target_adapter is not None
            ):
                delta.update(self._browser_target_delta(
                    browser_targets_before,
                    adapter=browser_target_adapter,
                    request_id=request_id,
                ))
            receipt = self.state.record_action(
                adapter_id=adapter.adapter_id,
                resource=resource,
                action=action,
                target_ref=ref.ref,
                expected_revision=fresh_revision,
                changed=applied.changed,
                side_effect_state=effect,
                result={
                    "execution_path": execution_path,
                    "delta": delta,
                    "side_effects": [],
                    "postconditions": [],
                },
                idempotency_key=payload.get("idempotency_key"),
                request_fingerprint=request_fingerprint,
                native_revision=post_observation.native_revision,
            )
            postconditions = self._check_predicates(
                payload.get("postconditions", ()), phase="post"
            )
            result = {
                "status": "applied" if applied.changed else "no_effect",
                "execution_path": execution_path,
                "receipt_id": receipt.receipt_id,
                "before_revision": receipt.before_revision,
                "after_revision": receipt.after_revision,
                "delta": delta,
                "side_effects": [],
                "postconditions": postconditions,
                "error": None,
            }
            self._trace("act", request_id, adapter.adapter_id, resource, started, "ok", receipt.receipt_id)
            return result, adapter, applied.provenance
        except (TimeoutError, ConnectionError) as transport_error:
            error = ProtocolError(
                ErrorCode.UNCERTAIN,
                "adapter transport failed after mutation began",
                retryable=False,
                side_effect_state=SideEffectState.UNKNOWN,
            )
            receipt = self.state.record_action(
                adapter_id=adapter.adapter_id,
                resource=resource,
                action=action,
                target_ref=ref.ref,
                expected_revision=fresh_revision,
                changed=None,
                side_effect_state=SideEffectState.UNKNOWN,
                result={"error": error.to_dict()},
                idempotency_key=payload.get("idempotency_key"),
                request_fingerprint=request_fingerprint,
            )
            error.candidates = ({"receipt_id": receipt.receipt_id},)
            raise error from transport_error
        except ProtocolError as error:
            if error.code is ErrorCode.POSTCONDITION_FAILED and "receipt" in locals():
                error.side_effect_state = SideEffectState.APPLIED
                error.candidates = tuple(error.candidates) + ({
                    "receipt_id": receipt.receipt_id,
                },)
            if error.side_effect_state is SideEffectState.UNKNOWN or error.code is ErrorCode.UNCERTAIN:
                receipt = self.state.record_action(
                    adapter_id=adapter.adapter_id,
                    resource=resource,
                    action=action,
                    target_ref=ref.ref,
                    expected_revision=fresh_revision,
                    changed=None,
                    side_effect_state=SideEffectState.UNKNOWN,
                    result={"error": error.to_dict()},
                    idempotency_key=payload.get("idempotency_key"),
                    request_fingerprint=request_fingerprint,
                )
                error.candidates = ({"receipt_id": receipt.receipt_id},)
            elif error.side_effect_state is SideEffectState.APPLIED and "receipt" not in locals():
                receipt = self.state.record_action(
                    adapter_id=adapter.adapter_id,
                    resource=resource,
                    action=action,
                    target_ref=ref.ref,
                    expected_revision=fresh_revision,
                    changed=True,
                    side_effect_state=SideEffectState.APPLIED,
                    result={"error": error.to_dict()},
                    idempotency_key=payload.get("idempotency_key"),
                    request_fingerprint=request_fingerprint,
                )
                error.candidates = tuple(error.candidates) + ({
                    "receipt_id": receipt.receipt_id,
                },)
            raise

    def _verify(
        self, payload: Mapping[str, Any], *, consume_budget: bool
    ):
        return self.verify_engine.verify(
            state=self.state,
            adapter_id=KERNEL_ADAPTER_ID,
            payload=payload,
            query=lambda query_payload: self._query(
                query_payload, consume_budget=False, request_id="verification-query"
            )[0],
            consume_budget=consume_budget,
        )

    def _trace(
        self, operation: str, request_id: str, adapter_id: str, resource: str | None,
        started: float, status: str, receipt_id: str | None = None,
    ) -> None:
        self.trace.append({
            "operation": operation,
            "request_id": request_id,
            "adapter_id": adapter_id,
            "resource": resource,
            "duration_ms": round((_now() - started) * 1_000, 3),
            "status": status,
            "receipt_id": receipt_id,
            "semantic_operations": self.state.semantic_operations,
        })

    def dispatch(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        started = _now()
        request_id = str(raw.get("request_id") or "invalid-request") if isinstance(raw, Mapping) else "invalid-request"
        adapter_id = KERNEL_ADAPTER_ID
        resource: str | None = None
        try:
            request = RequestEnvelope.parse(validate_request(raw))
            if request.episode_id != self.episode_id:
                raise ProtocolError(ErrorCode.PERMISSION_DENIED, "episode_id does not match route")
            if request.operation == "query":
                resource = str(request.payload.get("resource") or "")
                page, adapter, observation = self._query(
                    request.payload, consume_budget=True, request_id=request.request_id
                )
                adapter_id = adapter.adapter_id
                response = ok_response(
                    request_id=request.request_id,
                    adapter_id=adapter.adapter_id,
                    result=page.to_dict(),
                    before_revision=page.revision,
                    after_revision=page.revision,
                    provenance=observation.provenance,
                )
            elif request.operation == "act":
                result, adapter, provenance = self._act(
                    request.payload, consume_budget=True, request_id=request.request_id
                )
                adapter_id = adapter.adapter_id
                response = ok_response(
                    request_id=request.request_id,
                    adapter_id=adapter.adapter_id,
                    result=result,
                    before_revision=result["before_revision"],
                    after_revision=result["after_revision"],
                    provenance=provenance,
                )
            elif request.operation == "verify":
                verification = self._verify(request.payload, consume_budget=True)
                response = ok_response(
                    request_id=request.request_id,
                    adapter_id=KERNEL_ADAPTER_ID,
                    result=verification.to_dict(),
                    before_revision=None,
                    after_revision=None,
                    status=(Status.OK if verification.verdict == "pass" else Status.PARTIAL),
                )
            else:
                run = self.interpreter.execute(
                    str(request.payload.get("code") or ""),
                    computer=_RunComputer(self),
                    episode_state=self.state,
                )
                response = ok_response(
                    request_id=request.request_id,
                    adapter_id=KERNEL_ADAPTER_ID,
                    result=run.to_dict(),
                    before_revision=None,
                    after_revision=None,
                    status=(Status.PARTIAL if run.failed_operation else Status.OK),
                )
            self._trace(request.operation, request.request_id, adapter_id, resource, started, response.status.value)
            return validate_response(response.to_dict())
        except ProtocolError as error:
            evidence_id = self.state.issue_evidence_id()
            self.evidence_ids.add(evidence_id)
            error.candidates = tuple(error.candidates) + ({"evidence_id": evidence_id},)
            response = error_response(
                request_id=request_id,
                adapter_id=adapter_id,
                error=error,
            )
            self._trace("error", request_id, adapter_id, resource, started, response.status.value)
            return validate_response(response.to_dict())
        except Exception:
            error = ProtocolError(ErrorCode.INTERNAL_ERROR, "semantic kernel failed")
            response = error_response(
                request_id=request_id, adapter_id=adapter_id, error=error
            )
            self._trace("error", request_id, adapter_id, resource, started, "failed")
            return validate_response(response.to_dict())

    def complete(self, payload: Mapping[str, Any]) -> CompletionResult:
        if not isinstance(payload, Mapping):
            return CompletionResult(False, False, False, error={
                "code": "invalid_request", "message": "completion must be an object"
            })
        infeasible = payload.get("infeasible", False)
        claims = payload.get("claims")
        evidence_ids = payload.get("evidence_ids")
        if not isinstance(infeasible, bool) or not isinstance(claims, list) or not isinstance(evidence_ids, list):
            return CompletionResult(False, False, bool(infeasible), error={
                "code": "invalid_request", "message": "completion fields are invalid"
            })
        if self.state.uncertain_actions():
            return CompletionResult(False, False, infeasible, error={
                "code": "uncertain", "message": "an action has unresolved side effects"
            })
        if infeasible:
            if not evidence_ids or not all(value in self.evidence_ids for value in evidence_ids):
                return CompletionResult(False, False, True, error={
                    "code": "precondition_failed",
                    "message": "infeasible completion requires current typed evidence",
                })
            return CompletionResult(True, True, True)
        if not claims:
            return CompletionResult(False, False, False, error={
                "code": "precondition_failed",
                "message": "completion requires at least one passing verification",
            })
        for claim in claims:
            verification_id = claim.get("verification_id") if isinstance(claim, Mapping) else None
            try:
                is_current = (
                    isinstance(verification_id, str)
                    and self.state.verification_is_current(verification_id)
                )
            except ProtocolError:
                is_current = False
            if not is_current:
                return CompletionResult(False, False, False, error={
                    "code": "precondition_failed",
                    "message": "completion contains a stale, failed, or unknown verification",
                })
        warnings: list[str] = []
        try:
            pending_page, _, _ = self._query({
                "resource": "system.pending_state", "scope": {}, "where": {},
                "fields": [], "order_by": [], "parameters": {}, "limit": 30,
                "freshness": "live",
            }, consume_budget=False, request_id="completion-pending-state")
            for record in pending_page.records:
                for field in (
                    "modified_documents", "running_exports", "pending_downloads",
                    "live_disk_divergence",
                ):
                    if record.get(field):
                        warnings.append(field)
        except ProtocolError:
            warnings.append("pending_state_unavailable")
        return CompletionResult(True, True, False, tuple(sorted(set(warnings))))

    def state_summary(self, *, screenshots_captured: int) -> dict[str, Any]:
        return {
            "runtime": self.runtime_name,
            "protocol_version": "1.0",
            "screenshots_captured": screenshots_captured,
            "semantic_operations": self.state.semantic_operations,
            "semantic_budget": self.state.semantic_budget,
            "receipt_counts": self.state.receipt_counts(),
            "adapters": self.registry.describe(),
            "representation_gaps": list(self.representation_gaps),
            "trace_events": len(self.trace),
            "image_parts_created": 0,
            "image_parts_in_session": 0,
            "image_parts_sent": 0,
            "pixels_sent_to_policy_model": 0,
            "visual_sidecar_calls": 0,
        }

    def close(self) -> None:
        self.registry.close()
