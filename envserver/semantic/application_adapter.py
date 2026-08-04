"""Shared contract for versioned application semantic bridges.

The harness process cannot truthfully inspect an application by reading host
state: the application lives in the nested OSWorld guest.  Remaining
application adapters therefore speak a small, typed transport contract to a
versioned guest integration (extension, D-Bus bridge, parser, or native app
plugin).  When that integration is absent, the adapter fails explicitly with
``representation_gap`` instead of silently falling back to pixels, keys, or
coordinates.

This module contains no task instructions, evaluator knowledge, selectors, or
GUI automation escape hatch.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .adapters import (
    AdapterActionResult,
    AdapterContext,
    AdapterObservation,
    SemanticAdapter,
)
from .protocol import ErrorCode, ProtocolError, Recovery, SideEffectState, Status


Transport = Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]]


def semantic_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), default=str
        ).encode("utf-8")
    ).hexdigest()


def _transport_error(
    response: Mapping[str, Any], *, mutation_started: bool
) -> ProtocolError:
    raw = response.get("error")
    error = raw if isinstance(raw, Mapping) else {}
    raw_code = str(error.get("code") or "adapter_unavailable")
    # A route-level 404 from the base guest daemon (which contains no entity
    # message) means this versioned application integration is not installed.
    # Entity-level misses returned by an installed bridge include a message
    # and remain ordinary ``not_found`` errors.
    if raw_code == "not_found" and not error.get("message"):
        raw_code = "representation_gap"
    try:
        code = ErrorCode(raw_code)
    except ValueError:
        code = ErrorCode.INTERNAL_ERROR
    side_effect = str(error.get("side_effect_state") or "none")
    if mutation_started and side_effect == "unknown":
        code = ErrorCode.UNCERTAIN
    try:
        side_effect_state = SideEffectState(side_effect)
    except ValueError:
        side_effect_state = (
            SideEffectState.UNKNOWN if mutation_started else SideEffectState.NONE
        )
    recovery = error.get("recovery")
    recovery_record = recovery if isinstance(recovery, Mapping) else {}
    return ProtocolError(
        code,
        str(error.get("message") or "application semantic bridge failed")[:2_000],
        retryable=bool(error.get("retryable", False)),
        side_effect_state=side_effect_state,
        missing_capability=(
            str(error["missing_capability"])
            if error.get("missing_capability") is not None
            else None
        ),
        candidates=(
            tuple(item for item in error.get("candidates", ()) if isinstance(item, Mapping))
            if isinstance(error.get("candidates"), (list, tuple))
            else ()
        ),
        recovery=Recovery(
            allowed_operations=tuple(
                str(value)
                for value in recovery_record.get("allowed_operations", ())
                if isinstance(value, str)
            ),
            suggested_resource=(
                str(recovery_record["suggested_resource"])
                if recovery_record.get("suggested_resource") is not None
                else None
            ),
        ),
    )


@dataclass(frozen=True)
class ApplicationAdapterDescriptor:
    adapter_id: str
    application: str
    supported_versions: str
    resources: tuple[str, ...]
    actions: tuple[str, ...]
    execution_paths: tuple[str, ...]
    resource_schemas: Mapping[str, Any]
    action_schemas: Mapping[str, Any]
    known_representation_gaps: tuple[Mapping[str, Any], ...] = ()
    patch_hash: str | None = None
    native_routes: tuple[str, ...] = ()

    @staticmethod
    def _risk(action: str) -> str:
        if action in {"send"}:
            return "external"
        persistent_tokens = (
            "save", "export", "install", "disable", "enable", "edit_",
            "set_", "add_attachment", "remove_attachment", "create_filter",
            "update_filter", "move_message", "copy_message", "archive_message",
            "tag_message", "apply_", "rename_", "add_files", "remove_tag",
        )
        return (
            "persistent"
            if action.startswith(persistent_tokens) or action in {
                "compose", "reply", "forward", "close_image", "add_layer",
                "delete_layer", "duplicate_layer", "create_image", "open_image",
                "create_selection", "create_path", "add_guide", "remove_guide",
                "insert_text_layer", "convert", "resize", "crop", "cluster",
                "lookup", "scan", "move_track", "fill_form_field",
                "add_annotation", "save_copy", "print",
            }
            else "reversible"
        )

    def to_dict(self) -> dict[str, Any]:
        semantic_version = (
            self.adapter_id.rsplit("@", 1)[1]
            if "@" in self.adapter_id
            else "unversioned"
        )
        return {
            "adapter_id": self.adapter_id,
            "semantic_version": semantic_version,
            "application": self.application,
            "supported_versions": [self.supported_versions],
            "resources": list(self.resources),
            "actions": list(self.actions),
            "execution_paths": list(self.execution_paths),
            "resource_schemas": dict(self.resource_schemas),
            "action_schemas": {
                action: {
                    "arguments_schema": dict(schema),
                    "risk": self._risk(action),
                    "idempotent": False,
                    "execution_paths": list(self.execution_paths),
                }
                for action, schema in self.action_schemas.items()
            },
            "known_representation_gaps": [
                dict(gap) for gap in self.known_representation_gaps
            ],
            "accepts_entity_target": True,
            "patch_hash": self.patch_hash,
            "native_routes": list(self.native_routes),
        }


class RemoteApplicationAdapter(SemanticAdapter):
    """Base class for a truthful versioned integration in the nested guest."""

    descriptor_spec: ApplicationAdapterDescriptor

    def __init__(self, transport: Transport | None = None) -> None:
        descriptor = self.descriptor_spec
        self.adapter_id = descriptor.adapter_id
        self.resources = frozenset(descriptor.resources)
        self.capabilities = frozenset(descriptor.actions)
        self.accepts_entity_target = True
        self._transport = transport
        self._closed = False

    def descriptor(self) -> dict[str, Any]:
        return self.descriptor_spec.to_dict()

    @property
    def descriptor_record(self) -> dict[str, Any]:
        return self.descriptor()

    def probe(self) -> dict[str, Any]:
        if self._closed:
            return {
                "ok": False,
                "adapter_id": self.adapter_id,
                "code": "adapter_unavailable",
                "message": "adapter is closed",
            }
        if self._transport is None:
            return {
                "ok": False,
                "adapter_id": self.adapter_id,
                "code": "representation_gap",
                "message": "versioned guest integration is not installed",
            }
        try:
            response = self._transport(
                "GET", f"/v1/adapters/{self.adapter_id}/health", None
            )
        except Exception as error:
            return {
                "ok": False,
                "adapter_id": self.adapter_id,
                "code": "adapter_unavailable",
                "message": f"guest integration health failed: {type(error).__name__}",
            }
        if not isinstance(response, Mapping):
            return {
                "ok": False,
                "adapter_id": self.adapter_id,
                "code": "adapter_unavailable",
                "message": "guest integration returned an invalid health response",
            }
        result = response.get("result")
        public = dict(result) if isinstance(result, Mapping) else {}
        public.setdefault("ok", bool(response.get("ok")))
        public.setdefault("adapter_id", self.adapter_id)
        if public["ok"] is not True:
            error = response.get("error")
            error = error if isinstance(error, Mapping) else {}
            code = str(error.get("code") or "adapter_unavailable")
            if code == "not_found" and not error.get("message"):
                code = "representation_gap"
            public.setdefault("code", code)
            public.setdefault(
                "message",
                str(error.get("message") or "versioned guest integration is not installed"),
            )
        return public

    def health(self) -> dict[str, Any]:
        probe = self.probe()
        return {
            "adapter_id": self.adapter_id,
            "status": "healthy" if probe.get("ok") is True else "unavailable",
            "probe": probe,
        }

    def revision(self, surface: str | None = None) -> str | None:
        if self._transport is None or self._closed:
            return None
        payload = {"surface": surface} if surface is not None else {}
        response = self._call_transport(
            "POST",
            f"/v1/adapters/{self.adapter_id}/revision",
            payload,
            mutation_started=False,
        )
        if not response.get("ok"):
            raise _transport_error(response, mutation_started=False)
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ProtocolError(
                ErrorCode.INTERNAL_ERROR,
                "application bridge revision result is invalid",
            )
        revision = result.get("revision")
        return str(revision) if revision is not None else None

    def _require_transport(self, capability: str) -> Transport:
        if self._closed:
            raise ProtocolError(
                ErrorCode.ADAPTER_UNAVAILABLE,
                f"{self.adapter_id} is closed",
                retryable=False,
            )
        if self._transport is None:
            raise ProtocolError(
                ErrorCode.REPRESENTATION_GAP,
                f"{self.adapter_id} requires its versioned guest integration",
                retryable=False,
                missing_capability=capability,
                recovery=Recovery(
                    allowed_operations=("computer.query",),
                    suggested_resource="system.health",
                ),
            )
        return self._transport

    def _call_transport(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None,
        *,
        mutation_started: bool,
    ) -> Mapping[str, Any]:
        transport = self._require_transport(path)
        try:
            response = transport(method, path, payload)
        except ProtocolError:
            raise
        except Exception as error:
            raise ProtocolError(
                ErrorCode.UNCERTAIN if mutation_started else ErrorCode.ADAPTER_UNAVAILABLE,
                f"application semantic transport failed: {type(error).__name__}",
                retryable=not mutation_started,
                side_effect_state=(
                    SideEffectState.UNKNOWN if mutation_started else SideEffectState.NONE
                ),
            ) from error
        if not isinstance(response, Mapping):
            raise ProtocolError(
                ErrorCode.UNCERTAIN if mutation_started else ErrorCode.INTERNAL_ERROR,
                "application semantic transport returned a non-object response",
                side_effect_state=(
                    SideEffectState.UNKNOWN if mutation_started else SideEffectState.NONE
                ),
            )
        return response

    def _validate_query(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> None:
        if context.resource not in self.resources:
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, context.resource)
        scope = payload.get("scope")
        parameters = payload.get("parameters") or {}
        if not isinstance(scope, Mapping) or not isinstance(parameters, Mapping):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "application query scope and parameters must be objects",
            )
        query_arguments = {**dict(scope), **dict(parameters)}
        # Adapter selection fields belong to the common protocol rather than
        # the resource-specific parameter schema.
        for field in ("adapter", "surface", "ref", "document"):
            query_arguments.pop(field, None)
        schema = self.descriptor_spec.resource_schemas.get(context.resource)
        if isinstance(schema, Mapping):
            validate_schema(query_arguments, schema, field="parameters")

    def _validate_action(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> None:
        action = payload.get("action")
        if not isinstance(action, str) or action not in self.capabilities:
            raise ProtocolError(
                ErrorCode.UNSUPPORTED,
                f"{self.adapter_id} does not advertise action {action!r}",
                missing_capability=(str(action) if action is not None else None),
            )
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "application action arguments must be an object"
            )
        schema = self.descriptor_spec.action_schemas.get(action)
        if isinstance(schema, Mapping):
            validate_schema(arguments, schema, field=f"arguments.{action}")

    def observe(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterObservation:
        self._validate_query(context, payload)
        self._require_transport(context.resource)
        request = dict(payload)
        request["resource"] = context.resource
        request.update({
            "where": {}, "fields": [], "order_by": [], "cursor": None, "limit": 100,
        })
        records: list[dict[str, Any]] = []
        native_revision: str | None = None
        internal_offset = 0
        seen_offsets: set[int] = set()
        result: Mapping[str, Any] = {}
        while True:
            if internal_offset in seen_offsets:
                raise ProtocolError(
                    ErrorCode.INTERNAL_ERROR,
                    "application bridge repeated a private query offset",
                )
            seen_offsets.add(internal_offset)
            response = self._call_transport(
                "POST",
                f"/v1/adapters/{self.adapter_id}/query",
                {**request, "internal_offset": internal_offset},
                mutation_started=False,
            )
            if not response.get("ok"):
                raise _transport_error(response, mutation_started=False)
            raw_result = response.get("result")
            if not isinstance(raw_result, Mapping):
                raise ProtocolError(
                    ErrorCode.INTERNAL_ERROR,
                    "application bridge query returned no result",
                )
            result = raw_result
            page_records = result.get("records", ())
            if not isinstance(page_records, (list, tuple)) or not all(
                isinstance(record, Mapping) for record in page_records
            ):
                raise ProtocolError(
                    ErrorCode.INTERNAL_ERROR,
                    "application bridge query records are invalid",
                )
            page_revision = (
                str(result["revision"]) if result.get("revision") is not None else None
            )
            if native_revision is None:
                native_revision = page_revision
            elif page_revision is not None and page_revision != native_revision:
                raise ProtocolError(
                    ErrorCode.REVISION_CONFLICT,
                    "application state changed while its query was paged",
                    retryable=True,
                )
            records.extend(dict(record) for record in page_records)
            if len(records) > 5_000:
                raise ProtocolError(
                    ErrorCode.BUDGET_EXHAUSTED,
                    "application semantic collection exceeds 5000 records",
                )
            next_offset = result.get("next_internal_offset")
            if next_offset is None:
                if result.get("truncated") is True:
                    raise ProtocolError(
                        ErrorCode.ADAPTER_UNAVAILABLE,
                        "application bridge truncated without a private continuation",
                        retryable=True,
                    )
                break
            if (
                isinstance(next_offset, bool) or not isinstance(next_offset, int)
                or next_offset <= internal_offset
            ):
                raise ProtocolError(
                    ErrorCode.INTERNAL_ERROR,
                    "application bridge returned an invalid private continuation",
                )
            internal_offset = next_offset
        if native_revision is None:
            native_revision = f"{self.adapter_id}_{semantic_digest(records)[:20]}"
        return AdapterObservation(
            items=tuple(records),
            provenance=({
                "source": self.adapter_id,
                "freshness": "live",
                "execution_path": result.get(
                    "execution_path", self.descriptor_spec.execution_paths[0]
                ),
            },),
            summary={
                "record_count": len(records),
                "transport_pages": len(seen_offsets),
                "truncated": False,
                "total": result.get("total", len(records)),
            },
            native_revision=native_revision,
        )

    def act(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterActionResult:
        self._validate_action(context, payload)
        action = str(payload["action"])
        self._require_transport(action)
        response = self._call_transport(
            "POST",
            f"/v1/adapters/{self.adapter_id}/act",
            dict(payload),
            mutation_started=True,
        )
        if not response.get("ok"):
            raise _transport_error(
                response,
                mutation_started=True,
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ProtocolError(
                ErrorCode.UNCERTAIN,
                "application bridge action returned no result",
                side_effect_state=SideEffectState.UNKNOWN,
            )
        raw_status = str(result.get("status") or "applied")
        changed = result.get("changed")
        if not isinstance(changed, bool):
            changed = raw_status == "applied"
        try:
            status: Status | str = Status(str(response.get("status") or "ok"))
        except ValueError:
            status = Status.OK
        return AdapterActionResult(
            changed=changed,
            result=dict(result),
            provenance=({
                "source": self.adapter_id,
                "freshness": "live",
                "execution_path": result.get(
                    "execution_path", self.descriptor_spec.execution_paths[0]
                ),
            },),
            status=status,
            native_revision=(
                str(result["revision"]) if result.get("revision") is not None else None
            ),
        )

    def resolve_ref(self, ref: str) -> Mapping[str, Any]:
        self._require_transport("resolve_ref")
        response = self._call_transport(
            "POST",
            f"/v1/adapters/{self.adapter_id}/resolve-ref",
            {"ref": ref},
            mutation_started=False,
        )
        if not response.get("ok"):
            raise _transport_error(response, mutation_started=False)
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ProtocolError(ErrorCode.STALE_REF, "native ref no longer resolves")
        return dict(result)

    def close(self) -> None:
        if self._closed:
            return
        if self._transport is not None:
            try:
                self._transport(
                    "POST", f"/v1/adapters/{self.adapter_id}/close", {}
                )
            except Exception:
                # Episode teardown is the containment boundary.  Close remains
                # idempotent and must not conceal the original episode result.
                pass
        self._closed = True


def object_schema(
    properties: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    required: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required),
        "additionalProperties": False,
    }


PATH_SCHEMA = {"type": "string", "pattern": "^/"}
STRING_SCHEMA = {"type": "string", "maxLength": 65536}
BOOLEAN_SCHEMA = {"type": "boolean"}
NUMBER_SCHEMA = {"type": "number"}


def validate_schema(value: Any, schema: Mapping[str, Any], *, field: str) -> None:
    """Validate the bounded JSON-Schema subset used by adapter descriptors."""

    if not schema:
        return
    if "enum" in schema and value not in schema["enum"]:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} is not an advertised value")
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} must be an object")
        properties = schema.get("properties") or {}
        required = schema.get("required") or ()
        for name in required:
            if name not in value:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} requires {name}")
        if schema.get("additionalProperties") is False:
            unknown = set(map(str, value)) - set(map(str, properties))
            if unknown:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    f"{field} contains unknown field {sorted(unknown)[0]}",
                )
        for name, item in value.items():
            if name in properties and isinstance(properties[name], Mapping):
                validate_schema(item, properties[name], field=f"{field}.{name}")
        return
    if expected == "array":
        if not isinstance(value, (list, tuple)):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} must be an array")
        if len(value) < int(schema.get("minItems", 0)):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} has too few items")
        if len(value) > int(schema.get("maxItems", 5_000)):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} has too many items")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                validate_schema(item, item_schema, field=f"{field}[{index}]")
        return
    if expected == "string":
        if not isinstance(value, str):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} must be a string")
        if len(value) > int(schema.get("maxLength", 64 * 1024)):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} is too long")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} does not match its schema")
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} must be an integer")
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} must be numeric")
    elif expected == "boolean" and not isinstance(value, bool):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} must be boolean")
    if expected in {"integer", "number"}:
        if "minimum" in schema and value < schema["minimum"]:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{field} exceeds maximum")
