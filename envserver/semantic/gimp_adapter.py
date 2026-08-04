"""GIMP semantic adapter over a versioned PDB/plugin bridge."""

from __future__ import annotations

from typing import Any, Mapping

from .adapters import AdapterContext
from .application_adapter import (
    ApplicationAdapterDescriptor,
    BOOLEAN_SCHEMA,
    NUMBER_SCHEMA,
    PATH_SCHEMA,
    RemoteApplicationAdapter,
    STRING_SCHEMA,
    Transport,
    object_schema,
)
from .protocol import ErrorCode, ProtocolError


_RESOURCES = (
    "gimp.images", "gimp.canvas", "gimp.layers", "gimp.channels", "gimp.paths",
    "gimp.selections", "gimp.guides", "gimp.text_layers", "gimp.undo_history",
    "gimp.exports", "gimp.filters",
)
_ACTIONS = (
    "create_image", "open_image", "close_image", "add_layer", "delete_layer",
    "duplicate_layer", "reorder_layer", "set_layer_visibility", "set_layer_opacity",
    "set_layer_mode", "transform_layer", "create_selection", "create_path",
    "add_guide", "remove_guide", "apply_filter", "insert_text_layer",
    "edit_text_layer", "undo", "redo", "export",
)


class GimpSemanticAdapter(RemoteApplicationAdapter):
    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="gimp-pdb@1",
        application="GNU Image Manipulation Program",
        supported_versions=">=3,<4",
        resources=_RESOURCES,
        actions=_ACTIONS,
        execution_paths=("app_bridge",),
        native_routes=("gimp_pdb_plugin",),
        resource_schemas={resource: object_schema() for resource in _RESOURCES},
        action_schemas={
            "create_image": object_schema({"width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}}, required=("width", "height")),
            "open_image": object_schema({"path": PATH_SCHEMA}, required=("path",)),
            "close_image": object_schema({"discard_changes": BOOLEAN_SCHEMA}),
            "add_layer": object_schema({"name": STRING_SCHEMA, "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}}, required=("name",)),
            "delete_layer": object_schema(), "duplicate_layer": object_schema(),
            "reorder_layer": object_schema({"position": {"type": "integer", "minimum": 0}}, required=("position",)),
            "set_layer_visibility": object_schema({"visible": BOOLEAN_SCHEMA}, required=("visible",)),
            "set_layer_opacity": object_schema({"opacity": {"type": "number", "minimum": 0, "maximum": 100}}, required=("opacity",)),
            "set_layer_mode": object_schema({"mode": STRING_SCHEMA}, required=("mode",)),
            "transform_layer": object_schema({"transform": {"type": "object"}}, required=("transform",)),
            "create_selection": object_schema({"shape": {"enum": ["rectangle", "ellipse", "path"]}, "geometry": {"type": "object"}}, required=("shape", "geometry")),
            "create_path": object_schema({"points": {"type": "array", "maxItems": 10000}}, required=("points",)),
            "add_guide": object_schema({"orientation": {"enum": ["horizontal", "vertical"]}, "position": NUMBER_SCHEMA}, required=("orientation", "position")),
            "remove_guide": object_schema(),
            "apply_filter": object_schema({"procedure": STRING_SCHEMA, "parameters": {"type": "object"}}, required=("procedure",)),
            "insert_text_layer": object_schema({"text": STRING_SCHEMA, "properties": {"type": "object"}}, required=("text",)),
            "edit_text_layer": object_schema({"text": STRING_SCHEMA, "properties": {"type": "object"}}),
            "undo": object_schema(), "redo": object_schema(),
            "export": object_schema({"path": PATH_SCHEMA, "format": STRING_SCHEMA, "options": {"type": "object"}}, required=("path", "format")),
        },
        known_representation_gaps=({
            "capability": "visual_composition",
            "reason": "judging aesthetic composition or visual similarity requires semantic-visual-v1",
        }, {
            "capability": "freehand_painting",
            "reason": "unstructured brush trajectories require a visual reference and are not a bounded semantic action",
        }),
    )

    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__(transport)

    def _validate_action(self, context: AdapterContext, payload: Mapping[str, Any]) -> None:
        super()._validate_action(context, payload)
        action = str(payload["action"])
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "GIMP action arguments must be an object")
        for field in self.descriptor_spec.action_schemas[action].get("required", ()):
            if field not in arguments:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{action} requires {field}")
        if action == "apply_filter":
            procedure = arguments.get("procedure")
            if not isinstance(procedure, str) or not procedure or procedure.startswith("_"):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "apply_filter requires a public registered PDB procedure")
