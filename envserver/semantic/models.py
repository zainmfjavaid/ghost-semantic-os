"""Pydantic boundary models paired with the generated TypeBox wire schema."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .protocol import ErrorCode, ProtocolError


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "protocol" / "semantic-v1.schema.json"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Scope(StrictModel):
    adapter: str | None = None
    surface: str | None = None
    ref: str | None = None
    path: str | None = None
    document: str | None = None


class Order(StrictModel):
    field: str
    direction: Literal["asc", "desc"]


def _json_value(value: Any, *, path: str = "$", depth: int = 0) -> None:
    if depth > 32:
        raise ValueError(f"JSON nesting exceeds 32 at {path}")
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if value.lstrip().casefold().startswith("data:image/"):
            raise ValueError(f"image content is forbidden at {path}")
        if len(value) > 16_384:
            raise ValueError(f"string exceeds wire bound at {path}")
        return
    if isinstance(value, list):
        if len(value) > 2_048:
            raise ValueError(f"array exceeds wire bound at {path}")
        for index, child in enumerate(value):
            _json_value(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 512:
            raise ValueError(f"object exceeds wire bound at {path}")
        forbidden = {"image", "images", "image_url", "imageurl", "screenshot", "screenshots"}
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise ValueError(f"invalid JSON key at {path}")
            if key.casefold() in forbidden:
                raise ValueError(f"image field is forbidden at {path}.{key}")
            _json_value(child, path=f"{path}.{key}", depth=depth + 1)
        return
    raise ValueError(f"non-JSON value at {path}")


FILTER_LEAF = {
    "eq", "ne", "contains", "starts_with", "ends_with", "matches", "gt",
    "gte", "lt", "lte", "in", "has", "is_true", "is_false",
}


def _filter(value: Any, *, depth: int = 0) -> None:
    if value == {}:
        return
    if not isinstance(value, dict) or depth > 32:
        raise ValueError("where expression is invalid")
    operation = value.get("op")
    if operation in {"all", "any"}:
        if set(value) != {"op", "filters"}:
            raise ValueError("boolean filter contains unknown fields")
        children = value["filters"]
        if not isinstance(children, list) or not 1 <= len(children) <= 128:
            raise ValueError("boolean filter requires 1..128 children")
        for child in children:
            _filter(child, depth=depth + 1)
        return
    if operation == "not":
        if set(value) != {"op", "filter"}:
            raise ValueError("not filter contains unknown fields")
        _filter(value["filter"], depth=depth + 1)
        return
    if operation not in FILTER_LEAF or not isinstance(value.get("field"), str):
        raise ValueError("filter leaf is invalid")
    expected = {"op", "field"} if operation in {"is_true", "is_false"} else {"op", "field", "value"}
    if set(value) != expected:
        raise ValueError("filter leaf contains unknown or missing fields")
    if "value" in value:
        _json_value(value["value"])


ASSERT_OPS = {"exists", "absent", "eq", "ne", "contains", "matches", "count", "approx", "parseable"}


def _assertion(value: Any) -> None:
    if not isinstance(value, dict) or not set(value) <= {"op", "field", "value", "tolerance"}:
        raise ValueError("assertion is invalid")
    if value.get("op") not in ASSERT_OPS:
        raise ValueError("assertion operation is invalid")
    if "field" in value and not isinstance(value["field"], str):
        raise ValueError("assertion field must be a string")
    if "value" in value:
        _json_value(value["value"])
    if "tolerance" in value and (
        not isinstance(value["tolerance"], (int, float))
        or isinstance(value["tolerance"], bool)
        or value["tolerance"] < 0
    ):
        raise ValueError("assertion tolerance is invalid")


class QueryPayload(StrictModel):
    resource: str
    scope: Scope
    where: dict[str, Any] | None = None
    fields: list[str] | None = Field(default=None, max_length=128)
    order_by: list[Order] = Field(max_length=2)
    parameters: dict[str, Any]
    limit: int | None = Field(default=None, ge=1, le=100)
    cursor: str | None = None
    freshness: Literal["live", "cache_ok"]

    @model_validator(mode="after")
    def validate_semantics(self):
        _filter(self.where or {})
        _json_value(self.parameters)
        if self.fields is not None and len(set(self.fields)) != len(self.fields):
            raise ValueError("fields must be unique")
        return self


def _predicate(value: Any) -> None:
    if (
        not isinstance(value, dict)
        or not {"resource", "scope", "assert"} <= set(value)
        or not set(value) <= {"resource", "scope", "where", "assert"}
    ):
        raise ValueError("predicate must contain resource/scope/where/assert")
    if not isinstance(value["resource"], str):
        raise ValueError("predicate resource is invalid")
    Scope.model_validate(value["scope"])
    _filter(value.get("where", {}))
    _assertion(value["assert"])


def _predicate_expression(value: Any, *, depth: int = 0) -> None:
    if not isinstance(value, dict) or depth > 32:
        raise ValueError("predicate expression is invalid")
    if set(value) in ({"all"}, {"any"}):
        children = next(iter(value.values()))
        if not isinstance(children, list) or not 1 <= len(children) <= 128:
            raise ValueError("predicate boolean requires 1..128 children")
        for child in children:
            _predicate_expression(child, depth=depth + 1)
        return
    if set(value) == {"not"}:
        _predicate_expression(value["not"], depth=depth + 1)
        return
    _predicate(value)


class ActPayload(StrictModel):
    target: dict[str, Any]
    action: str
    arguments: dict[str, Any]
    expected_revision: str | None = None
    preconditions: list[dict[str, Any]] = Field(max_length=128)
    postconditions: list[dict[str, Any]] = Field(max_length=128)
    timeout_ms: int | None = Field(default=None, ge=1, le=300_000)
    idempotency_key: str | None = None
    confirm: bool | None = None

    @model_validator(mode="after")
    def validate_semantics(self):
        if set(self.target) == {"ref"}:
            if not isinstance(self.target["ref"], str):
                raise ValueError("target ref is invalid")
        elif set(self.target) == {"resource", "scope", "where"}:
            if not isinstance(self.target["resource"], str):
                raise ValueError("target resource is invalid")
            Scope.model_validate(self.target["scope"])
            _filter(self.target["where"])
        else:
            raise ValueError("target must be exact ref or semantic selector")
        _json_value(self.arguments)
        for value in self.preconditions + self.postconditions:
            _predicate_expression(value)
        return self


def _verify_expression(value: Any, *, depth: int = 0) -> None:
    if not isinstance(value, dict) or depth > 32:
        raise ValueError("verification expression is invalid")
    if set(value) in ({"all"}, {"any"}):
        children = next(iter(value.values()))
        if not isinstance(children, list) or not 1 <= len(children) <= 128:
            raise ValueError("verification boolean requires 1..128 children")
        for child in children:
            _verify_expression(child, depth=depth + 1)
        return
    if set(value) == {"not"}:
        _verify_expression(value["not"], depth=depth + 1)
        return
    if set(value) != {"claim_id", "query", "assert"} or not isinstance(value.get("claim_id"), str):
        raise ValueError("verification leaf is invalid")
    QueryPayload.model_validate(value["query"])
    _assertion(value["assert"])


class VerifyPayload(StrictModel):
    mode: Literal["all", "any"]
    assertions: list[dict[str, Any]] = Field(min_length=1, max_length=128)
    freshness: Literal["live"]
    reconcile_action: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_semantics(self):
        for assertion in self.assertions:
            _verify_expression(assertion)
        if "reconcile_action" in self.model_fields_set and self.reconcile_action is None:
            raise ValueError("reconcile_action cannot be null")
        if self.reconcile_action is not None:
            if set(self.reconcile_action) != {"receipt_id", "outcome"}:
                raise ValueError(
                    "reconcile_action requires exact receipt_id and outcome fields"
                )
            if not isinstance(self.reconcile_action.get("receipt_id"), str):
                raise ValueError("reconcile_action receipt_id is invalid")
            if self.reconcile_action.get("outcome") not in {"none", "applied"}:
                raise ValueError("reconcile_action outcome is invalid")
        return self


class RunPayload(StrictModel):
    code: str = Field(min_length=1, max_length=12_000)


class SemanticRequest(StrictModel):
    protocol_version: Literal["1.0"]
    request_id: str
    episode_id: str
    operation: Literal["query", "act", "verify", "run"]
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self):
        model = {
            "query": QueryPayload,
            "act": ActPayload,
            "verify": VerifyPayload,
            "run": RunPayload,
        }[self.operation]
        model.model_validate(self.payload)
        return self


class SemanticResponse(StrictModel):
    protocol_version: Literal["1.0"]
    request_id: str
    status: Literal["ok", "partial", "rejected", "failed", "uncertain"]
    adapter_id: str
    observed_at: str
    before_revision: str | None
    after_revision: str | None
    result: dict[str, Any] | None
    provenance: list[dict[str, Any]]
    error: dict[str, Any] | None

    @model_validator(mode="after")
    def validate_json(self):
        _json_value(self.result)
        _json_value(self.provenance)
        _json_value(self.error)
        return self


def _protocol_error(label: str, error: ValidationError | ValueError) -> ProtocolError:
    return ProtocolError(
        ErrorCode.INVALID_REQUEST,
        f"{label}: {str(error)[:2_000]}",
    )


def validate_request(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        SemanticRequest.model_validate(value)
        return dict(value)
    except (ValidationError, ValueError) as error:
        raise _protocol_error("invalid semantic request", error) from error


def validate_payload(operation: str, value: Mapping[str, Any]) -> dict[str, Any]:
    models = {
        "query": QueryPayload,
        "act": ActPayload,
        "verify": VerifyPayload,
        "run": RunPayload,
    }
    model = models.get(operation)
    if model is None:
        raise ProtocolError(ErrorCode.UNSUPPORTED, f"unknown operation: {operation}")
    try:
        model.model_validate(value)
        return dict(value)
    except (ValidationError, ValueError) as error:
        raise _protocol_error(f"invalid {operation} payload", error) from error


def validate_response(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        SemanticResponse.model_validate(value)
        return dict(value)
    except (ValidationError, ValueError) as error:
        raise _protocol_error("invalid semantic response", error) from error


def load_canonical_schema() -> dict[str, Any]:
    """Load the checked TypeBox artifact used by cross-language CI."""

    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise RuntimeError("semantic protocol schema is not JSON Schema 2020-12")
    return schema
