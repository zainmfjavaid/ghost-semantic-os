"""Thunderbird semantic adapter over a versioned MailExtension bridge."""

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
    "mail.accounts", "mail.folders", "mail.messages", "mail.threads",
    "mail.search_results", "mail.drafts", "mail.composer", "mail.attachments",
    "mail.tags", "mail.filters", "mail.settings",
)
_ACTIONS = (
    "open_message", "search_messages", "compose", "reply", "forward",
    "set_recipients", "set_subject", "set_body", "add_attachment",
    "remove_attachment", "save_draft", "move_message", "copy_message",
    "archive_message", "tag_message", "create_filter", "update_filter", "send",
)


class ThunderbirdSemanticAdapter(RemoteApplicationAdapter):
    descriptor_spec = ApplicationAdapterDescriptor(
        adapter_id="thunderbird-extension@1",
        application="Mozilla Thunderbird",
        supported_versions=">=115,<200",
        resources=_RESOURCES,
        actions=_ACTIONS,
        execution_paths=("app_bridge", "native_api", "accessibility"),
        native_routes=("mail_extension", "native_messaging", "profile_parser"),
        resource_schemas={resource: object_schema() for resource in _RESOURCES},
        action_schemas={
            "open_message": object_schema(),
            "search_messages": object_schema({"query": STRING_SCHEMA}, required=("query",)),
            "compose": object_schema(),
            "reply": object_schema(),
            "forward": object_schema(),
            "set_recipients": object_schema({
                "to": {"type": "array", "items": STRING_SCHEMA, "maxItems": 100},
                "cc": {"type": "array", "items": STRING_SCHEMA, "maxItems": 100},
                "bcc": {"type": "array", "items": STRING_SCHEMA, "maxItems": 100},
            }),
            "set_subject": object_schema({"subject": STRING_SCHEMA}, required=("subject",)),
            "set_body": object_schema({
                "body": STRING_SCHEMA,
                "format": {"enum": ["plain", "html"]},
            }, required=("body",)),
            "add_attachment": object_schema({"path": PATH_SCHEMA}, required=("path",)),
            "remove_attachment": object_schema(),
            "save_draft": object_schema(),
            "move_message": object_schema({"folder_ref": STRING_SCHEMA}, required=("folder_ref",)),
            "copy_message": object_schema({"folder_ref": STRING_SCHEMA}, required=("folder_ref",)),
            "archive_message": object_schema(),
            "tag_message": object_schema({"tag": STRING_SCHEMA, "enabled": BOOLEAN_SCHEMA}, required=("tag",)),
            "create_filter": object_schema({"name": STRING_SCHEMA, "conditions": {"type": "array"}, "actions": {"type": "array"}}, required=("name", "conditions", "actions")),
            "update_filter": object_schema({"changes": {"type": "object"}}, required=("changes",)),
            "send": object_schema(),
        },
        known_representation_gaps=({
            "capability": "server_side_mail_search_when_account_is_offline",
            "reason": "the bridge reports only state available to the configured account and local store",
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
            raise ProtocolError(ErrorCode.INVALID_REQUEST, "mail action arguments must be an object")
        schema = self.descriptor_spec.action_schemas[action]
        for field in schema.get("required", ()):
            if field not in arguments:
                raise ProtocolError(ErrorCode.INVALID_REQUEST, f"{action} requires {field}")
        if action == "send" and payload.get("idempotency_key") is None:
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "send requires an idempotency_key because it affects an external party",
            )
