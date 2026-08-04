"""VS Code semantic adapter over a versioned Ghost extension."""

from __future__ import annotations

from typing import Any, Mapping

from .adapters import AdapterContext
from .application_adapter import (
    ApplicationAdapterDescriptor,
    BOOLEAN_SCHEMA,
    PATH_SCHEMA,
    RemoteApplicationAdapter,
    STRING_SCHEMA,
    Transport,
    object_schema,
)
from .protocol import ErrorCode, ProtocolError


_RESOURCES = (
    "vscode.workspaces", "vscode.files", "vscode.editors", "vscode.buffers",
    "vscode.selections", "vscode.symbols", "vscode.diagnostics",
    "vscode.search_results", "vscode.settings", "vscode.extensions",
    "vscode.terminals", "vscode.tasks", "vscode.save_state",
)
_ACTIONS = (
    "open_file", "apply_text_edit", "apply_workspace_edit", "rename_symbol",
    "save", "set_setting", "run_command", "run_task", "run_terminal_process",
    "install_extension", "enable_extension", "disable_extension",
)


class VSCodeSemanticAdapter(RemoteApplicationAdapter):
    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="vscode-ghost-extension@1",
        application="Visual Studio Code",
        supported_versions=">=1.80,<2",
        resources=_RESOURCES,
        actions=_ACTIONS,
        execution_paths=("app_bridge",),
        native_routes=("vscode_extension",),
        resource_schemas={resource: object_schema() for resource in _RESOURCES},
        action_schemas={
            "open_file": object_schema({"uri": STRING_SCHEMA}),
            "apply_text_edit": object_schema({
                "uri": STRING_SCHEMA,
                "expected_buffer_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "range": {"type": "object"},
                "text": STRING_SCHEMA,
            }, required=("uri", "expected_buffer_hash", "range", "text")),
            "apply_workspace_edit": object_schema({
                "edits": {"type": "array", "minItems": 1, "maxItems": 1000},
            }, required=("edits",)),
            "rename_symbol": object_schema({
                "uri": STRING_SCHEMA, "position": {"type": "object"}, "new_name": STRING_SCHEMA,
            }, required=("uri", "position", "new_name")),
            "save": object_schema(),
            "set_setting": object_schema({"key": STRING_SCHEMA, "value": {}}, required=("key", "value")),
            "run_command": object_schema({"command": STRING_SCHEMA, "arguments": {"type": "array", "maxItems": 100}}, required=("command",)),
            "run_task": object_schema({"task": STRING_SCHEMA}, required=("task",)),
            "run_terminal_process": object_schema({
                "argv": {"type": "array", "items": STRING_SCHEMA, "minItems": 1, "maxItems": 256},
                "cwd": PATH_SCHEMA,
            }, required=("argv", "cwd")),
            "install_extension": object_schema({"identifier": STRING_SCHEMA}, required=("identifier",)),
            "enable_extension": object_schema(),
            "disable_extension": object_schema(),
        },
        known_representation_gaps=({
            "capability": "rendered_extension_webview_visual_content",
            "reason": "extension webview pixels require the separately scored visual sidecar",
        },),
    )

    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__(transport)

    def _validate_action(
        self, context: AdapterContext, payload: Mapping[str, Any]
    ) -> None:
        super()._validate_action(context, payload)
        action = str(payload["action"])
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "VS Code action arguments must be an object")
        schema = self.descriptor_spec.action_schemas[action]
        for field in schema.get("required", ()):
            if field not in arguments:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{action} requires {field}")
        if action in {"apply_text_edit", "apply_workspace_edit"}:
            edits = [arguments] if action == "apply_text_edit" else arguments.get("edits", ())
            if not isinstance(edits, (list, tuple)):
                raise ProtocolError(ErrorCode.INVALID_REQUEST, "workspace edits must be an array")
            for edit in edits:
                if not isinstance(edit, Mapping) or not edit.get("expected_buffer_hash"):
                    raise ProtocolError(
                        ErrorCode.INVALID_REQUEST,
                        "every VS Code text edit requires expected_buffer_hash",
                    )
