"""Deterministic media metadata and Picard semantic adapters."""

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


class MediaMetadataAdapter(RemoteApplicationAdapter):
    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="media-metadata@1",
        application="Deterministic image/media parser",
        supported_versions="protocol 1",
        resources=(
            "media.files", "media.dimensions", "media.streams", "media.metadata",
            "media.exif", "media.ocr", "media.palette", "media.histogram",
        ),
        actions=("edit_metadata", "convert", "resize", "crop", "save", "export"),
        execution_paths=("native_api",),
        native_routes=("deterministic_artifact_parser",),
        resource_schemas={
            resource: object_schema({"path": PATH_SCHEMA})
            for resource in (
                "media.files", "media.dimensions", "media.streams", "media.metadata",
                "media.exif", "media.ocr", "media.palette", "media.histogram",
            )
        },
        action_schemas={
            "edit_metadata": object_schema({"fields": {"type": "object"}}, required=("fields",)),
            "convert": object_schema({"path": PATH_SCHEMA, "format": STRING_SCHEMA}, required=("path", "format")),
            "resize": object_schema({"width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}, "preserve_aspect": {"type": "boolean"}}),
            "crop": object_schema({"x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0}, "width": {"type": "integer", "minimum": 1}, "height": {"type": "integer", "minimum": 1}}, required=("x", "y", "width", "height")),
            "save": object_schema(),
            "export": object_schema({"path": PATH_SCHEMA, "format": STRING_SCHEMA}, required=("path", "format")),
        },
        known_representation_gaps=({
            "capability": "visual_content",
            "reason": "semantic-v1 exposes deterministic OCR/palette/histogram data but does not judge images",
        },),
    )

    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__(transport)

    def _validate_action(self, context: AdapterContext, payload: Mapping[str, Any]) -> None:
        super()._validate_action(context, payload)
        action = str(payload["action"])
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "media action arguments must be an object")
        for field in self.descriptor_spec.action_schemas[action].get("required", ()):
            if field not in arguments:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{action} requires {field}")
        if action == "resize" and "width" not in arguments and "height" not in arguments:
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "resize requires width or height")


class PicardMediaAdapter(RemoteApplicationAdapter):
    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="picard-media@1",
        application="MusicBrainz Picard",
        supported_versions=">=2,<3",
        resources=(
            "picard.files", "picard.clusters", "picard.albums", "picard.tracks",
            "picard.tags", "picard.save_state",
        ),
        actions=(
            "add_files", "cluster", "lookup", "scan", "set_tag", "remove_tag",
            "move_track", "save",
        ),
        execution_paths=("app_bridge", "native_api"),
        native_routes=("picard_plugin", "deterministic_artifact_parser"),
        resource_schemas={
            resource: object_schema()
            for resource in (
                "picard.files", "picard.clusters", "picard.albums", "picard.tracks",
                "picard.tags", "picard.save_state",
            )
        },
        action_schemas={
            "add_files": object_schema({"paths": {"type": "array", "items": PATH_SCHEMA, "minItems": 1, "maxItems": 1000}}, required=("paths",)),
            "cluster": object_schema(), "lookup": object_schema(), "scan": object_schema(),
            "set_tag": object_schema({"name": STRING_SCHEMA, "value": STRING_SCHEMA}, required=("name", "value")),
            "remove_tag": object_schema({"name": STRING_SCHEMA}, required=("name",)),
            "move_track": object_schema({"album_ref": STRING_SCHEMA, "position": {"type": "integer", "minimum": 0}}, required=("album_ref",)),
            "save": object_schema(),
        },
        known_representation_gaps=(),
    )

    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__(transport)
