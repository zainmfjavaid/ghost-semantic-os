"""Terminal semantics over the guest's argv-only sandbox worker.

This is the application-family facade for OSWorld terminal tasks.  It never
types into a terminal window.  The corresponding guest worker owns PTYs and
executes an argv array without a shell or desktop/session credentials.
"""

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


class SandboxedTerminalAdapter(RemoteApplicationAdapter):
    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="sandboxed-process@1",
        application="Sandboxed terminal/process worker",
        supported_versions="protocol 1",
        resources=(
            "terminal.sessions", "terminal.process", "terminal.output",
            "terminal.command_status",
        ),
        actions=("create_session", "close_session", "exec", "send_stdin", "wait"),
        execution_paths=("native_api",),
        native_routes=("sandbox_worker",),
        resource_schemas={
            resource: object_schema()
            for resource in (
                "terminal.sessions", "terminal.process", "terminal.output",
                "terminal.command_status",
            )
        },
        action_schemas={
            "create_session": object_schema({"cwd": PATH_SCHEMA}, required=("cwd",)),
            "close_session": object_schema(),
            "exec": object_schema({
                "argv": {"type": "array", "items": STRING_SCHEMA, "minItems": 1, "maxItems": 256},
                "cwd": PATH_SCHEMA,
                "stdin": {"type": "string", "maxLength": 1_048_576},
                "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 300},
            }, required=("argv", "cwd")),
            "send_stdin": object_schema({"data": {"type": "string", "maxLength": 1_048_576}}, required=("data",)),
            "wait": object_schema({"timeout_seconds": {"type": "number", "minimum": 0, "maximum": 300}}),
        },
        known_representation_gaps=({
            "capability": "interactive_full_screen_terminal_ui",
            "reason": "the worker exposes owned processes and PTYs, not pixel terminal control",
        },),
    )

    def __init__(self, transport: Transport | None = None) -> None:
        super().__init__(transport)

    def _validate_action(self, context: AdapterContext, payload: Mapping[str, Any]) -> None:
        super()._validate_action(context, payload)
        if payload.get("action") != "exec":
            return
        arguments = payload.get("arguments")
        if not isinstance(arguments, Mapping):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "process action arguments must be an object")
        argv = arguments.get("argv")
        if not isinstance(argv, (list, tuple)) or not argv or not all(
            isinstance(value, str) and value and "\x00" not in value for value in argv
        ):
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "exec requires a non-empty argv array")
        # A shell string is not part of the published action schema.  Shell
        # metacharacters are harmless literal argv values because the worker
        # never inserts a shell executable.
        if "command" in arguments or "shell" in arguments or "env" in arguments:
            raise ProtocolError(
                ErrorCode.POLICY_VIOLATION,
                "shell strings and environment injection are unavailable",
            )
