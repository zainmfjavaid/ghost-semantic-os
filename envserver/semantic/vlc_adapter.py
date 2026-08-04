"""VLC and generic media-player semantic adapters over MPRIS/D-Bus."""

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


_VLC_RESOURCES = (
    "vlc.playback", "vlc.media", "vlc.playlist", "vlc.audio_tracks",
    "vlc.subtitle_tracks", "vlc.preferences", "vlc.equalizer",
)
_VLC_ACTIONS = (
    "play", "pause", "stop", "seek", "set_volume", "add_playlist_entry",
    "remove_playlist_entry", "reorder_playlist", "select_audio_track",
    "select_subtitle_track", "set_loop", "set_shuffle", "set_equalizer",
    "save_preferences",
)


class VLCSemanticAdapter(RemoteApplicationAdapter):
    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="vlc-mpris-http@1",
        application="VLC media player",
        supported_versions=">=3,<4",
        resources=_VLC_RESOURCES,
        actions=_VLC_ACTIONS,
        execution_paths=("native_api",),
        native_routes=("mpris_dbus", "authenticated_http"),
        resource_schemas={resource: object_schema() for resource in _VLC_RESOURCES},
        action_schemas={
            "play": object_schema(), "pause": object_schema(), "stop": object_schema(),
            "seek": object_schema({"position_seconds": NUMBER_SCHEMA}, required=("position_seconds",)),
            "set_volume": object_schema({"volume": {"type": "number", "minimum": 0, "maximum": 2}}, required=("volume",)),
            "add_playlist_entry": object_schema({"path": PATH_SCHEMA}, required=("path",)),
            "remove_playlist_entry": object_schema(),
            "reorder_playlist": object_schema({"position": {"type": "integer", "minimum": 0}}, required=("position",)),
            "select_audio_track": object_schema({"track_id": STRING_SCHEMA}, required=("track_id",)),
            "select_subtitle_track": object_schema({"track_id": STRING_SCHEMA}, required=("track_id",)),
            "set_loop": object_schema({"mode": {"enum": ["none", "track", "playlist"]}}, required=("mode",)),
            "set_shuffle": object_schema({"enabled": BOOLEAN_SCHEMA}, required=("enabled",)),
            "set_equalizer": object_schema({"preset": STRING_SCHEMA, "bands": {"type": "array", "items": NUMBER_SCHEMA, "maxItems": 64}}),
            "save_preferences": object_schema(),
        },
        known_representation_gaps=(),
    )

    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__(transport)

    def _validate_action(self, context: AdapterContext, payload: Mapping[str, Any]) -> None:
        super()._validate_action(context, payload)
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "VLC action arguments must be an object")
        action = str(payload["action"])
        for field in self.descriptor_spec.action_schemas[action].get("required", ()):
            if field not in arguments:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{action} requires {field}")


class MPRISMediaAdapter(RemoteApplicationAdapter):
    """Application-neutral MPRIS controls used by the inventory's media family."""

    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="mpris-media@1",
        application="MPRIS-compatible media player",
        supported_versions="MPRIS2",
        resources=("media.playback", "media.current", "media.players"),
        actions=("play", "pause", "stop", "seek", "set_volume"),
        execution_paths=("native_api",),
        native_routes=("mpris_dbus",),
        resource_schemas={
            resource: object_schema()
            for resource in ("media.playback", "media.current", "media.players")
        },
        action_schemas={
            "play": object_schema(), "pause": object_schema(), "stop": object_schema(),
            "seek": object_schema({"position_seconds": NUMBER_SCHEMA}, required=("position_seconds",)),
            "set_volume": object_schema({"volume": {"type": "number", "minimum": 0, "maximum": 2}}, required=("volume",)),
        },
    )

    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__(transport)
