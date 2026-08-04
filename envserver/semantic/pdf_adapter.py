"""PDF structure and Evince live-state semantic adapter."""

from __future__ import annotations

from typing import Any, Mapping

from .adapters import AdapterContext
from .application_adapter import (
    ApplicationAdapterDescriptor,
    PATH_SCHEMA,
    RemoteApplicationAdapter,
    STRING_SCHEMA,
    Transport,
    object_schema,
)
from .protocol import ErrorCode, ProtocolError


_RESOURCES = (
    "pdf.documents", "pdf.pages", "pdf.text", "pdf.sections", "pdf.links",
    "pdf.annotations", "pdf.forms", "pdf.metadata", "pdf.selection",
    "pdf.save_state",
)
_ACTIONS = (
    "open", "go_to_page", "follow_link", "fill_form_field", "add_annotation",
    "save_copy", "print", "export",
)


class PDFEvinceSemanticAdapter(RemoteApplicationAdapter):
    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="pdf-evince@1",
        application="Evince and PDF artifact parser",
        supported_versions="PDF 1.x/2.0; Evince >=42,<50",
        resources=_RESOURCES,
        actions=_ACTIONS,
        execution_paths=("native_api", "accessibility"),
        native_routes=("poppler_parser", "evince_dbus"),
        resource_schemas={
            resource: object_schema({
                "path": PATH_SCHEMA,
                "page": {"type": "integer", "minimum": 1},
            }) for resource in _RESOURCES
        },
        action_schemas={
            "open": object_schema({"path": PATH_SCHEMA}, required=("path",)),
            "go_to_page": object_schema({"page": {"type": "integer", "minimum": 1}}, required=("page",)),
            "follow_link": object_schema(),
            "fill_form_field": object_schema({"value": STRING_SCHEMA}, required=("value",)),
            "add_annotation": object_schema({"text": STRING_SCHEMA, "page": {"type": "integer", "minimum": 1}}, required=("text",)),
            "save_copy": object_schema({"path": PATH_SCHEMA}, required=("path",)),
            "print": object_schema({"destination": {"enum": ["printer", "pdf"]}, "path": PATH_SCHEMA}, required=("destination",)),
            "export": object_schema({"path": PATH_SCHEMA, "format": STRING_SCHEMA}, required=("path", "format")),
        },
        known_representation_gaps=({
            "capability": "visual_page_similarity",
            "reason": "visual page composition belongs to semantic-visual-v1",
        }, {
            "capability": "freeform_ink_annotation",
            "reason": "freehand geometry cannot be expressed without a visual reference",
        }),
    )

    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__(transport)

    def _validate_action(self, context: AdapterContext, payload: Mapping[str, Any]) -> None:
        super()._validate_action(context, payload)
        action = str(payload["action"])
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "PDF action arguments must be an object")
        for field in self.descriptor_spec.action_schemas[action].get("required", ()):
            if field not in arguments:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{action} requires {field}")
        if action == "print" and arguments.get("destination") == "pdf" and not arguments.get("path"):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "PDF print destination requires path")
