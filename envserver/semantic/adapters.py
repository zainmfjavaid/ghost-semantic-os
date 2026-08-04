"""Semantic adapter interface and deterministic registry."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Mapping, Sequence

from .protocol import ErrorCode, ProtocolError, Status


# Adapter IDs may carry an explicit protocol/implementation generation, e.g.
# ``libreoffice.uno@1``.  The registry treats that suffix as identity, never as
# a version range.
IDENTIFIER = re.compile(
    r"^[A-Za-z][A-Za-z0-9._-]{0,126}(?:@[A-Za-z0-9][A-Za-z0-9._-]{0,63})?$"
)

RISK_CLASSES = frozenset({"reversible", "persistent", "external"})
EXECUTION_PATHS = frozenset({
    "native_api", "app_bridge", "accessibility", "semantic_input",
})

# These are conservative defaults for legacy adapters which have not yet
# supplied per-action metadata.  An adapter descriptor can override the risk
# for any action, but may never advertise an unknown risk class.
DEFAULT_EXTERNAL_ACTIONS = frozenset({"send", "submit", "publish", "purchase"})
DEFAULT_PERSISTENT_ACTIONS = frozenset({
    "create_directory", "copy", "move", "rename", "write_text", "patch_text",
    "save", "save_as", "export", "download", "set_setting", "write_clipboard",
    "create", "update", "delete", "install", "enable", "disable", "uninstall",
    "load_unpacked", "save_pdf", "create_shortcut", "clear_history",
    "create_bookmark", "update_bookmark", "move_bookmark", "delete_bookmark",
    "set_pref", "delete_history", "enable_extension", "disable_extension",
    "uninstall_extension", "create_desktop_entry", "write_base64_atomic",
})


class CapabilitySet(frozenset[str]):
    """Backwards-compatible action set which also fulfils ``capabilities()``.

    The first semantic adapters used a public ``capabilities`` frozenset.  The
    v1 adapter contract calls for a lifecycle method of the same name.  Making
    the immutable set callable preserves membership checks used by existing
    adapters while giving probes and conformance tests a uniform callable API.
    """

    def __new__(cls, values: Sequence[str] | frozenset[str] = ()):
        return super().__new__(cls, values)

    def __call__(self) -> tuple[str, ...]:
        return tuple(sorted(self))


def _object_schema(*, allow_additional: bool) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {},
        "additionalProperties": allow_additional,
    }


def _semantic_record_schema() -> dict[str, Any]:
    """Schema for fields the kernel adds to every observed record.

    Adapters may refine this with resource-specific properties.  Keeping
    ``additionalProperties`` true is intentional: older guest bridges did not
    publish result-field schemas, and capability discovery must describe that
    uncertainty rather than pretend their native fields do not exist.
    """

    return {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "Opaque episode-scoped entity ref"},
            "kind": {"type": "string"},
            "states": {"type": "object", "additionalProperties": True},
            "advertised_actions": {"type": "array", "items": {"type": "string"}},
            "revision": {"type": "string"},
            "source": {"type": "string"},
            "freshness": {"type": "string", "enum": ["live"]},
        },
        "required": [
            "ref", "kind", "states", "advertised_actions", "revision",
            "source", "freshness",
        ],
        "additionalProperties": True,
    }


def _field_schema(description: Any) -> dict[str, Any]:
    if isinstance(description, Mapping):
        return dict(description)
    text = str(description)
    lowered = text.casefold()
    schema: dict[str, Any] = {"description": text[:1_024]}
    if "array" in lowered or "list" in lowered:
        schema["type"] = "array"
    elif "number" in lowered or "integer" in lowered:
        schema["type"] = "number"
    elif "boolean" in lowered or lowered.startswith("bool"):
        schema["type"] = "boolean"
    else:
        schema["type"] = "string"
    return schema


def _normalize_schema(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize legacy field-description maps into real JSON Schemas."""

    value = dict(raw or {})
    schema_keywords = {
        "$ref", "$defs", "type", "properties", "items", "oneOf", "anyOf",
        "allOf", "enum", "const",
    }
    if schema_keywords & set(value):
        return value
    if set(value) == {"parameters"} and isinstance(value["parameters"], Mapping):
        value = dict(value["parameters"])
    if not value:
        return _object_schema(allow_additional=True)
    required = [
        key for key, description in value.items()
        if isinstance(description, str) and description.casefold().startswith("required ")
    ]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            str(key): _field_schema(description)
            for key, description in value.items()
        },
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def _validate_value(value: Any, schema: Mapping[str, Any], *, label: str) -> None:
    """Validate the bounded JSON-Schema subset used by adapter descriptors."""

    if "enum" in schema and value not in schema["enum"]:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{label} is not an allowed value")
    expected = schema.get("type")
    matches = {
        "object": isinstance(value, Mapping),
        "array": isinstance(value, (list, tuple)),
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }
    if isinstance(expected, str) and expected in matches and not matches[expected]:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{label} must be {expected}")
    if expected == "object" and isinstance(value, Mapping):
        properties = schema.get("properties") or {}
        required = schema.get("required") or ()
        if isinstance(required, (list, tuple)):
            missing = [field for field in required if field not in value]
            if missing:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"{label} is missing fields: {missing!r}"
                )
        if isinstance(properties, Mapping):
            unknown = set(value) - set(properties)
            if unknown and schema.get("additionalProperties") is False:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"{label} has unknown fields: {sorted(unknown)!r}"
                )
            for field, child in value.items():
                child_schema = properties.get(field)
                if isinstance(child_schema, Mapping):
                    _validate_value(child, child_schema, label=f"{label}.{field}")
    if expected == "array" and isinstance(value, (list, tuple)):
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, child in enumerate(value):
                _validate_value(child, items, label=f"{label}[{index}]")


def _default_risk(action: str) -> str:
    if action in DEFAULT_EXTERNAL_ACTIONS:
        return "external"
    if action in DEFAULT_PERSISTENT_ACTIONS:
        return "persistent"
    return "reversible"


def _normalize_action_descriptor(
    action: str,
    raw: Mapping[str, Any] | None,
    execution_paths: Sequence[str],
) -> dict[str, Any]:
    value = dict(raw or {})
    if any(key in value for key in ("arguments_schema", "risk", "idempotent", "execution_paths")):
        arguments = value.get("arguments_schema", _object_schema(allow_additional=True))
        risk = value.get("risk", _default_risk(action))
        idempotent = value.get("idempotent", False)
        paths = value.get("execution_paths", execution_paths)
    else:
        # Guest v0 descriptors used the action_schemas value directly as the
        # arguments JSON Schema.  Retain that wire compatibility.
        arguments = value or _object_schema(allow_additional=True)
        risk = _default_risk(action)
        idempotent = False
        paths = execution_paths
    if not isinstance(arguments, Mapping):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid arguments schema for {action}")
    if risk not in RISK_CLASSES:
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid risk class for {action}")
    if not isinstance(idempotent, bool):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid idempotency metadata for {action}")
    if (
        not isinstance(paths, (list, tuple))
        or not paths
        or not all(path in EXECUTION_PATHS for path in paths)
    ):
        raise ProtocolError(ErrorCode.INVALID_REQUEST, f"invalid execution paths for {action}")
    normalized_arguments = _normalize_schema(arguments)
    if normalized_arguments.get("type") != "object":
        raise ProtocolError(
            ErrorCode.INVALID_REQUEST,
            f"action arguments schema must be an object for {action}",
        )
    return {
        "arguments_schema": normalized_arguments,
        "risk": risk,
        "idempotent": idempotent,
        "execution_paths": list(dict.fromkeys(paths)),
    }


@dataclass(frozen=True)
class AdapterContext:
    episode_id: str
    resource: str
    request_id: str
    before_revision: str | None


@dataclass(frozen=True)
class AdapterObservation:
    items: Sequence[Mapping[str, Any]]
    provenance: Sequence[Mapping[str, Any]] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)
    native_revision: str | None = None


@dataclass(frozen=True)
class AdapterActionResult:
    changed: bool | None
    result: Mapping[str, Any] = field(default_factory=dict)
    provenance: Sequence[Mapping[str, Any]] = ()
    status: Status | str = Status.OK
    native_revision: str | None = None


class SemanticAdapter(ABC):
    """Task-agnostic lifecycle contract implemented by every surface adapter.

    ``observe`` remains the compatibility hook used by early adapters.  New
    code calls ``query``; the default delegates to ``observe``.  Lifecycle
    defaults are deliberately side-effect-free and descriptors always publish
    resource/action schemas plus action risk and idempotency metadata.
    """

    adapter_id: str
    resources: frozenset[str]
    capabilities: frozenset[str]
    application: str = "generic"
    supported_versions: Sequence[str] = ("*",)
    execution_paths: Sequence[str] = ("native_api",)
    resource_schemas: Mapping[str, Mapping[str, Any]] = {}
    resource_field_schemas: Mapping[str, Mapping[str, Any]] = {}
    resource_actions: Mapping[str, Sequence[str]] = {}
    action_schemas: Mapping[str, Mapping[str, Any]] = {}
    known_representation_gaps: Sequence[Mapping[str, Any]] = ()
    accepts_entity_target: bool = False
    patch_hash: str | None = None

    def descriptor(self) -> dict[str, Any]:
        paths = tuple(self.execution_paths)
        if not paths or not all(path in EXECUTION_PATHS for path in paths):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid adapter execution paths")
        actions = CapabilitySet(self.capabilities)
        resource_schemas: dict[str, Any] = {}
        resource_field_schemas: dict[str, Any] = {}
        for resource in sorted(self.resources):
            schema = self.resource_schemas.get(resource, _object_schema(allow_additional=True))
            if not isinstance(schema, Mapping):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"invalid resource schema for {resource}"
                )
            normalized = _normalize_schema(schema)
            if normalized.get("type") != "object":
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    f"resource parameters schema must be an object for {resource}",
                )
            resource_schemas[resource] = normalized
            raw_fields = self.resource_field_schemas.get(resource)
            if raw_fields is None:
                field_schema = _semantic_record_schema()
            elif not isinstance(raw_fields, Mapping):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    f"invalid result field schema for {resource}",
                )
            else:
                field_schema = _normalize_schema(raw_fields)
                if field_schema.get("type") != "object":
                    raise ProtocolError(
                        ErrorCode.INVALID_REQUEST,
                        f"result field schema must be an object for {resource}",
                    )
            resource_field_schemas[resource] = field_schema
        action_schemas = {
            action: _normalize_action_descriptor(
                action, self.action_schemas.get(action), paths
            )
            for action in sorted(actions)
        }
        resource_actions: dict[str, list[str]] = {}
        for resource in sorted(self.resources):
            declared = self.resource_actions.get(resource)
            # Compatibility for adapters which predate resource-level action
            # declarations.  It remains truthful (the adapter owns every
            # advertised action) but less precise than an explicit mapping.
            values = tuple(actions) if declared is None else tuple(declared)
            if not all(isinstance(action, str) and action in actions for action in values):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    f"resource action map for {resource} contains an unknown action",
                )
            resource_actions[resource] = sorted(set(values))
        unknown_action_resources = set(self.resource_actions) - set(self.resources)
        if unknown_action_resources:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                f"resource action map contains unknown resources: {sorted(unknown_action_resources)!r}",
            )
        version = self.adapter_id.rsplit("@", 1)[1] if "@" in self.adapter_id else "unversioned"
        gaps: list[dict[str, Any]] = []
        for value in self.known_representation_gaps:
            if not isinstance(value, Mapping):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST,
                    "representation gaps must be typed objects",
                )
            gaps.append(dict(value))
        descriptor = {
            "adapter_id": self.adapter_id,
            "semantic_version": version,
            "application": self.application,
            "supported_versions": list(self.supported_versions),
            "resources": sorted(self.resources),
            "actions": list(actions()),
            "execution_paths": list(dict.fromkeys(paths)),
            "resource_schemas": resource_schemas,
            "resource_field_schemas": resource_field_schemas,
            "resource_actions": resource_actions,
            "action_schemas": action_schemas,
            "known_representation_gaps": gaps,
            "accepts_entity_target": bool(self.accepts_entity_target),
            "patch_hash": self.patch_hash,
        }
        return descriptor

    def probe(self) -> Mapping[str, Any]:
        return {
            "ok": True,
            "adapter_id": self.adapter_id,
            "resources": sorted(self.resources),
        }

    def health(self) -> Mapping[str, Any]:
        probe = self.probe()
        return {
            "adapter_id": self.adapter_id,
            "status": "healthy" if probe.get("ok") is True else "unavailable",
            "probe": dict(probe),
        }

    def revision(self, surface: str | None = None) -> str | None:
        del surface
        return None

    def query(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterObservation:
        return self.observe(context, payload)

    def resolve_ref(self, ref: str) -> Any:
        """Resolve an adapter-private ref when the adapter has a native route.

        The kernel's live re-observation is the authoritative generic fallback;
        returning ``None`` explicitly says that no additional native resolver
        is available.  It must never trigger focus or another mutation.
        """

        del ref
        return None

    def resolve_data_handle(self, handle: str) -> AdapterObservation | None:
        """Resolve one opaque collection owned by this adapter, if any.

        Returning ``None`` means this adapter does not implement data handles.
        Implementations which do own handles return a read-only observation or
        raise ``not_found`` for a missing/expired capability.
        """

        del handle
        return None

    def action_metadata(self, action: str) -> Mapping[str, Any]:
        if action not in self.capabilities:
            raise ProtocolError(
                ErrorCode.UNSUPPORTED,
                f"adapter does not advertise action: {action}",
                missing_capability=action,
            )
        return dict(self.descriptor()["action_schemas"][action])

    def validate_parameters(self, resource: str, parameters: Mapping[str, Any]) -> None:
        schema = self.descriptor()["resource_schemas"].get(resource)
        if not isinstance(schema, Mapping):
            raise ProtocolError(ErrorCode.UNKNOWN_RESOURCE, resource)
        _validate_value(parameters, schema, label=f"{resource}.parameters")

    def validate_arguments(self, action: str, arguments: Mapping[str, Any]) -> None:
        metadata = self.action_metadata(action)
        schema = metadata.get("arguments_schema")
        if not isinstance(schema, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{action} has no arguments schema")
        _validate_value(arguments, schema, label=f"{action}.arguments")

    def close(self) -> None:
        """Release adapter resources.  Must be safe to call more than once."""

    @abstractmethod
    def observe(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterObservation:
        """Return a fresh semantic observation for ``context.resource``."""

    @abstractmethod
    def act(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> AdapterActionResult:
        """Perform one adapter mutation after the kernel checks preconditions."""


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, SemanticAdapter] = {}
        self._resources: dict[str, list[str]] = {}
        self._lock = RLock()

    def register(self, adapter: SemanticAdapter) -> SemanticAdapter:
        adapter_id = getattr(adapter, "adapter_id", None)
        resources = getattr(adapter, "resources", None)
        capabilities = getattr(adapter, "capabilities", None)
        if not isinstance(adapter_id, str) or not IDENTIFIER.fullmatch(adapter_id):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "invalid adapter_id")
        if not isinstance(resources, frozenset) or not resources:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "adapter resources must be a non-empty frozenset"
            )
        if not isinstance(capabilities, frozenset):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "adapter capabilities must be a frozenset"
            )
        # Normalize the old immutable attribute into a callable immutable set,
        # satisfying both the v0 and v1 contract forms.
        if not isinstance(capabilities, CapabilitySet):
            capabilities = CapabilitySet(capabilities)
            setattr(adapter, "capabilities", capabilities)
        for value in (*resources, *capabilities):
            if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"invalid adapter identifier: {value!r}"
                )
        descriptor = adapter.descriptor()
        if descriptor.get("adapter_id") != adapter_id:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "descriptor adapter_id mismatch")
        if set(descriptor.get("resources", ())) != set(resources):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "descriptor resources mismatch")
        if set(descriptor.get("actions", ())) != set(capabilities):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "descriptor actions mismatch")
        with self._lock:
            if adapter_id in self._adapters:
                raise ProtocolError(
                    ErrorCode.INVALID_REQUEST, f"adapter already registered: {adapter_id}"
                )
            self._adapters[adapter_id] = adapter
            for resource in sorted(resources):
                self._resources.setdefault(resource, []).append(adapter_id)
                self._resources[resource].sort()
        return adapter

    def unregister(self, adapter_id: str) -> None:
        with self._lock:
            adapter = self._adapters.pop(adapter_id, None)
            if adapter is None:
                raise ProtocolError(
                    ErrorCode.ADAPTER_UNAVAILABLE,
                    f"adapter is not registered: {adapter_id}",
                )
            for resource in adapter.resources:
                members = self._resources.get(resource, [])
                if adapter_id in members:
                    members.remove(adapter_id)
                if not members:
                    self._resources.pop(resource, None)

    def get(self, adapter_id: str) -> SemanticAdapter:
        with self._lock:
            adapter = self._adapters.get(adapter_id)
        if adapter is None:
            raise ProtocolError(
                ErrorCode.ADAPTER_UNAVAILABLE,
                f"adapter is not registered: {adapter_id}",
                retryable=True,
            )
        return adapter

    def resolve(
        self,
        resource: str,
        *,
        adapter_id: str | None = None,
        required_capability: str | None = None,
    ) -> SemanticAdapter:
        if adapter_id is not None:
            adapter = self.get(adapter_id)
            if resource not in adapter.resources:
                raise ProtocolError(
                    ErrorCode.UNKNOWN_RESOURCE,
                    f"adapter {adapter_id!r} does not expose {resource!r}",
                )
            candidates = [adapter]
        else:
            with self._lock:
                ids = tuple(self._resources.get(resource, ()))
                candidates = [self._adapters[value] for value in ids]
            if not candidates:
                raise ProtocolError(
                    ErrorCode.UNKNOWN_RESOURCE,
                    f"no adapter exposes resource: {resource}",
                )
        if required_capability is not None:
            candidates = [
                adapter
                for adapter in candidates
                if required_capability in adapter.capabilities
            ]
            if not candidates:
                raise ProtocolError(
                    ErrorCode.UNSUPPORTED,
                    f"resource {resource!r} lacks capability {required_capability!r}",
                    missing_capability=required_capability,
                )
        if len(candidates) > 1:
            raise ProtocolError(
                ErrorCode.AMBIGUOUS,
                f"multiple adapters expose resource: {resource}",
                candidates=[{"adapter_id": item.adapter_id} for item in candidates],
            )
        return candidates[0]

    def describe(self) -> list[dict[str, Any]]:
        with self._lock:
            adapters = [adapter for _, adapter in sorted(self._adapters.items())]
        return [adapter.descriptor() for adapter in adapters]

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            adapters = [adapter for _, adapter in sorted(self._adapters.items())]
        records: list[dict[str, Any]] = []
        for adapter in adapters:
            try:
                value = adapter.health()
                records.append(dict(value))
            except Exception as error:
                records.append({
                    "adapter_id": adapter.adapter_id,
                    "status": "unavailable",
                    "error": str(error)[:2_000],
                })
        return records

    def resolve_data_handle(self, handle: str) -> AdapterObservation:
        """Resolve exactly one adapter-owned data handle."""

        with self._lock:
            adapters = [adapter for _, adapter in sorted(self._adapters.items())]
        matches: list[AdapterObservation] = []
        for adapter in adapters:
            try:
                observation = adapter.resolve_data_handle(handle)
            except ProtocolError as error:
                if error.code is ErrorCode.NOT_FOUND:
                    continue
                raise
            if observation is not None:
                matches.append(observation)
        if not matches:
            raise ProtocolError(
                ErrorCode.NOT_FOUND,
                "data handle is missing or expired",
                retryable=False,
            )
        if len(matches) > 1:
            raise ProtocolError(
                ErrorCode.AMBIGUOUS,
                "multiple adapters resolved the same opaque data handle",
            )
        return matches[0]

    def close(self) -> None:
        with self._lock:
            adapters = [adapter for _, adapter in sorted(self._adapters.items())]
        for adapter in adapters:
            try:
                adapter.close()
            except Exception:
                # Teardown is best effort and must continue closing independent
                # adapters.  Runtime traces still contain the earlier health.
                continue
