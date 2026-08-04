import { Type, type Static, type TSchema } from "typebox";
import { Check, Errors } from "typebox/value";

export const SEMANTIC_PROTOCOL_VERSION = "1.0" as const;

const strictObject = <const P extends Parameters<typeof Type.Object>[0]>(
  properties: P,
  options: Parameters<typeof Type.Object>[1] = {},
) => Type.Object(properties, { additionalProperties: false, ...options });

const opaqueId = Type.String({ minLength: 1, maxLength: 512 });
const fieldName = Type.String({ minLength: 1, maxLength: 256 });
const safeString = (maxLength = 16_384) => Type.String({
  maxLength,
  pattern: "^(?!\\s*data:image/)[\\s\\S]*$",
});
const safeKey = Type.String({
  minLength: 1,
  maxLength: 256,
  pattern: "^(?!(?:image|images|image_url|imageUrl|screenshot|screenshots)$).+$",
});
const timestamp = Type.String({
  pattern: "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\\.[0-9]+)?Z$",
});

/** Bounded JSON for adapter-specific values; never provider content. */
export const JsonValueSchema = Type.Cyclic({
  JsonValue: Type.Union([
    Type.Null(),
    Type.Boolean(),
    Type.Number(),
    safeString(),
    Type.Array(Type.Ref("JsonValue"), { maxItems: 2_048 }),
    Type.Record(safeKey, Type.Ref("JsonValue"), {
      maxProperties: 512,
      additionalProperties: false,
    }),
  ]),
}, "JsonValue");
export const JsonObjectSchema = Type.Record(safeKey, JsonValueSchema, {
  maxProperties: 512,
  additionalProperties: false,
});

export const ScopeSchema = strictObject({
  adapter: Type.Optional(opaqueId),
  surface: Type.Optional(opaqueId),
  ref: Type.Optional(opaqueId),
  path: Type.Optional(safeString(4_096)),
  document: Type.Optional(opaqueId),
});

const comparisonFilter = (op: string) => strictObject({
  op: Type.Literal(op),
  field: fieldName,
  value: JsonValueSchema,
});
const unaryFieldFilter = (op: string) => strictObject({
  op: Type.Literal(op),
  field: fieldName,
});

export const QueryFilterSchema = Type.Cyclic({
  Filter: Type.Union([
    strictObject({
      op: Type.Literal("all"),
      filters: Type.Array(Type.Ref("Filter"), { minItems: 1, maxItems: 128 }),
    }),
    strictObject({
      op: Type.Literal("any"),
      filters: Type.Array(Type.Ref("Filter"), { minItems: 1, maxItems: 128 }),
    }),
    strictObject({ op: Type.Literal("not"), filter: Type.Ref("Filter") }),
    comparisonFilter("eq"),
    comparisonFilter("ne"),
    comparisonFilter("contains"),
    comparisonFilter("starts_with"),
    comparisonFilter("ends_with"),
    comparisonFilter("matches"),
    comparisonFilter("gt"),
    comparisonFilter("gte"),
    comparisonFilter("lt"),
    comparisonFilter("lte"),
    comparisonFilter("in"),
    comparisonFilter("has"),
    unaryFieldFilter("is_true"),
    unaryFieldFilter("is_false"),
  ]),
}, "Filter");
export const QueryWhereSchema = Type.Union([strictObject({}), QueryFilterSchema]);

export const QueryPayloadSchema = strictObject({
  resource: Type.String({ minLength: 1, maxLength: 256 }),
  scope: ScopeSchema,
  where: Type.Optional(QueryWhereSchema),
  fields: Type.Optional(Type.Array(fieldName, { maxItems: 128, uniqueItems: true })),
  order_by: Type.Array(strictObject({
    field: fieldName,
    direction: Type.Enum(["asc", "desc"] as const),
  }), { maxItems: 2 }),
  parameters: JsonObjectSchema,
  limit: Type.Optional(Type.Integer({ minimum: 1, maximum: 100, default: 30 })),
  cursor: Type.Optional(opaqueId),
  freshness: Type.Enum(["live", "cache_ok"] as const),
});

export const PredicateSchema = strictObject({
  resource: Type.String({ minLength: 1, maxLength: 256 }),
  scope: ScopeSchema,
  where: Type.Optional(QueryWhereSchema),
  assert: strictObject({
    op: Type.Enum([
      "exists", "absent", "eq", "ne", "contains", "matches", "count", "approx", "parseable",
    ] as const),
    field: Type.Optional(fieldName),
    value: Type.Optional(JsonValueSchema),
    tolerance: Type.Optional(Type.Number({ minimum: 0 })),
  }),
});

export const AssertionExpressionSchema = Type.Cyclic({
  Assertion: Type.Union([
    PredicateSchema,
    strictObject({
      all: Type.Array(Type.Ref("Assertion"), { minItems: 1, maxItems: 128 }),
    }),
    strictObject({
      any: Type.Array(Type.Ref("Assertion"), { minItems: 1, maxItems: 128 }),
    }),
    strictObject({ not: Type.Ref("Assertion") }),
  ]),
}, "Assertion");

export const ActTargetSchema = Type.Union([
  strictObject({ ref: opaqueId }),
  strictObject({
    resource: Type.String({ minLength: 1, maxLength: 256 }),
    scope: ScopeSchema,
    where: QueryWhereSchema,
  }),
]);

export const ActPayloadSchema = strictObject({
  target: ActTargetSchema,
  action: Type.String({ minLength: 1, maxLength: 256 }),
  arguments: JsonObjectSchema,
  expected_revision: Type.Optional(opaqueId),
  preconditions: Type.Array(AssertionExpressionSchema, { maxItems: 128 }),
  postconditions: Type.Array(AssertionExpressionSchema, { maxItems: 128 }),
  timeout_ms: Type.Optional(Type.Integer({ minimum: 1, maximum: 300_000, default: 10_000 })),
  idempotency_key: Type.Optional(opaqueId),
  confirm: Type.Optional(Type.Boolean({ default: false })),
});

export const VerifyAssertionSchema = strictObject({
  claim_id: opaqueId,
  query: QueryPayloadSchema,
  assert: strictObject({
    op: Type.Enum([
      "exists", "absent", "eq", "ne", "contains", "matches", "count", "approx", "parseable",
    ] as const),
    field: Type.Optional(fieldName),
    value: Type.Optional(JsonValueSchema),
    tolerance: Type.Optional(Type.Number({ minimum: 0 })),
  }),
});

export const VerifyExpressionSchema = Type.Cyclic({
  VerifyExpression: Type.Union([
    VerifyAssertionSchema,
    strictObject({
      all: Type.Array(Type.Ref("VerifyExpression"), { minItems: 1, maxItems: 128 }),
    }),
    strictObject({
      any: Type.Array(Type.Ref("VerifyExpression"), { minItems: 1, maxItems: 128 }),
    }),
    strictObject({ not: Type.Ref("VerifyExpression") }),
  ]),
}, "VerifyExpression");

export const VerifyPayloadSchema = strictObject({
  mode: Type.Enum(["all", "any"] as const),
  assertions: Type.Array(VerifyExpressionSchema, { minItems: 1, maxItems: 128 }),
  freshness: Type.Literal("live"),
  reconcile_action: Type.Optional(strictObject({
    receipt_id: opaqueId,
    outcome: Type.Enum(["none", "applied"] as const),
  })),
});

export const RunPayloadSchema = strictObject({
  code: Type.String({ minLength: 1, maxLength: 12_000 }),
});

export const TaskCompletePayloadSchema = strictObject({
  summary: safeString(8_192),
  infeasible: Type.Optional(Type.Boolean({ default: false })),
  claims: Type.Array(strictObject({
    claim: safeString(8_192),
    verification_id: opaqueId,
  }), { maxItems: 128 }),
  evidence_ids: Type.Array(opaqueId, { maxItems: 256, uniqueItems: true }),
});

export const ErrorCodeSchema = Type.Enum([
  "invalid_request",
  "unknown_resource",
  "unsupported",
  "representation_gap",
  "adapter_unavailable",
  "not_found",
  "ambiguous",
  "stale_ref",
  "revision_conflict",
  "precondition_failed",
  "postcondition_failed",
  "no_effect",
  "uncertain",
  "timeout",
  "permission_denied",
  "artifact_conflict",
  "policy_violation",
  "budget_exhausted",
  "internal_error",
] as const);

export const SemanticErrorSchema = strictObject({
  code: ErrorCodeSchema,
  message: safeString(8_192),
  retryable: Type.Boolean(),
  side_effect_state: Type.Enum(["none", "applied", "unknown"] as const),
  missing_capability: Type.Union([safeString(1_024), Type.Null()]),
  candidates: Type.Array(JsonValueSchema, { maxItems: 128 }),
  recovery: strictObject({
    allowed_operations: Type.Array(Type.String({ minLength: 1, maxLength: 256 }), {
      maxItems: 64,
      uniqueItems: true,
    }),
    suggested_resource: Type.Optional(Type.String({ minLength: 1, maxLength: 256 })),
  }),
});

export const ProvenanceEntrySchema = strictObject({
  source: Type.String({ minLength: 1, maxLength: 256 }),
  freshness: Type.Enum(["live", "cached", "persistent", "derived"] as const),
  execution_path: Type.Optional(Type.Enum([
    "native_api", "app_bridge", "accessibility", "semantic_input",
  ] as const)),
});

const capabilityPublicFields = {
  ref: opaqueId,
  kind: Type.Literal("system.capability"),
  name: Type.String({ minLength: 1, maxLength: 256 }),
  adapter_id: opaqueId,
  resources: Type.Array(Type.String({ minLength: 1, maxLength: 256 }), {
    minItems: 1,
    maxItems: 256,
    uniqueItems: true,
  }),
  description: Type.String({ minLength: 1, maxLength: 1_024 }),
  states: JsonObjectSchema,
  advertised_actions: Type.Array(Type.String({ minLength: 1, maxLength: 256 }), {
    maxItems: 256,
    uniqueItems: true,
  }),
  revision: opaqueId,
  source: opaqueId,
  freshness: Type.Literal("live"),
} as const;

/** Compact records returned by system.capabilities. */
export const CapabilitySummaryRecordSchema = Type.Union([
  strictObject({
    ...capabilityPublicFields,
    capability_type: Type.Literal("adapter"),
    application: Type.String({ maxLength: 1_024 }),
    semantic_version: Type.String({ minLength: 1, maxLength: 128 }),
  }),
  strictObject({
    ...capabilityPublicFields,
    capability_type: Type.Literal("resource"),
    resource: Type.String({ minLength: 1, maxLength: 256 }),
    actions: Type.Array(Type.String({ minLength: 1, maxLength: 256 }), {
      maxItems: 256,
      uniqueItems: true,
    }),
  }),
]);

/** One resource-scoped schema returned by system.capability. */
export const ResourceCapabilityDetailRecordSchema = strictObject({
  ref: opaqueId,
  kind: Type.Literal("system.capability"),
  capability_type: Type.Literal("resource_descriptor"),
  name: Type.String({ minLength: 1, maxLength: 256 }),
  adapter_id: opaqueId,
  application: Type.String({ maxLength: 1_024 }),
  resource: Type.String({ minLength: 1, maxLength: 256 }),
  resources: Type.Tuple([Type.String({ minLength: 1, maxLength: 256 })]),
  field_schema: JsonObjectSchema,
  parameter_schema: JsonObjectSchema,
  actions: Type.Array(Type.String({ minLength: 1, maxLength: 256 }), {
    maxItems: 256,
    uniqueItems: true,
  }),
  action_schemas: JsonObjectSchema,
  verification_schema: JsonObjectSchema,
  states: JsonObjectSchema,
  advertised_actions: Type.Array(Type.String({ minLength: 1, maxLength: 256 }), {
    maxItems: 256,
    uniqueItems: true,
  }),
  revision: opaqueId,
  source: opaqueId,
  freshness: Type.Literal("live"),
});

const requestEnvelope = <const O extends "query" | "act" | "verify" | "run", const P extends TSchema>(
  operation: O,
  payload: P,
) => strictObject({
  protocol_version: Type.Literal(SEMANTIC_PROTOCOL_VERSION),
  request_id: opaqueId,
  episode_id: opaqueId,
  operation: Type.Literal(operation),
  payload,
});

export const QueryRequestSchema = requestEnvelope("query", QueryPayloadSchema);
export const ActRequestSchema = requestEnvelope("act", ActPayloadSchema);
export const VerifyRequestSchema = requestEnvelope("verify", VerifyPayloadSchema);
export const RunRequestSchema = requestEnvelope("run", RunPayloadSchema);
export const SemanticRequestSchema = Type.Union([
  QueryRequestSchema,
  ActRequestSchema,
  VerifyRequestSchema,
  RunRequestSchema,
]);

export const QueryResultSchema = strictObject({
  records: Type.Array(JsonObjectSchema, { maxItems: 100 }),
  next_cursor: Type.Union([opaqueId, Type.Null()]),
  truncated: Type.Boolean(),
  total: Type.Union([Type.Integer({ minimum: 0 }), Type.Null()]),
  overflow_handle: Type.Optional(Type.Union([opaqueId, Type.Null()])),
  // Deprecated compatibility alias for overflow_handle. Adapter-owned
  // collections use an explicit collection_handle inside semantic records.
  data_handle: Type.Optional(Type.Union([opaqueId, Type.Null()])),
});

export const ActResultSchema = strictObject({
  status: Type.Enum(["applied", "no_effect", "rejected", "failed", "uncertain"] as const),
  execution_path: Type.Enum(["native_api", "app_bridge", "accessibility", "semantic_input"] as const),
  receipt_id: opaqueId,
  before_revision: Type.Union([opaqueId, Type.Null()]),
  after_revision: Type.Union([opaqueId, Type.Null()]),
  delta: JsonObjectSchema,
  side_effects: Type.Array(JsonObjectSchema, { maxItems: 256 }),
  postconditions: Type.Array(JsonObjectSchema, { maxItems: 128 }),
  error: Type.Union([SemanticErrorSchema, Type.Null()]),
});

export const VerifyResultSchema = strictObject({
  verification_id: opaqueId,
  verdict: Type.Enum(["pass", "fail", "unknown"] as const),
  claims: Type.Array(strictObject({
    claim_id: opaqueId,
    verdict: Type.Enum(["pass", "fail", "unknown"] as const),
    observed: Type.Union([JsonValueSchema, Type.Null()]),
    evidence_ids: Type.Array(opaqueId, { maxItems: 128 }),
  }), { maxItems: 128 }),
  dependencies: Type.Array(Type.Union([
    strictObject({ surface: opaqueId, revision: opaqueId }),
    strictObject({ artifact: opaqueId, hash: opaqueId }),
  ]), { maxItems: 256 }),
  evidence: Type.Array(JsonObjectSchema, { maxItems: 512 }),
  observed_at: timestamp,
  reconciliation: Type.Optional(strictObject({
    reconciliation_id: opaqueId,
    action_receipt_id: opaqueId,
    verification_id: opaqueId,
    verification_fingerprint: opaqueId,
    outcome: Type.Enum(["none", "applied"] as const),
    observed_at: timestamp,
  })),
});

export const RunResultSchema = strictObject({
  value: JsonValueSchema,
  output: Type.Array(JsonValueSchema, { maxItems: 2_048 }),
  operation_count: Type.Integer({ minimum: 0 }),
  applied_operations: Type.Integer({ minimum: 0 }),
  failed_operation: Type.Union([
    strictObject({
      index: Type.Integer({ minimum: 0 }),
      error: SemanticErrorSchema,
    }),
    Type.Null(),
  ]),
});

const responseEnvelope = <const R extends TSchema>(result: R) => strictObject({
  protocol_version: Type.Literal(SEMANTIC_PROTOCOL_VERSION),
  request_id: opaqueId,
  status: Type.Enum(["ok", "partial", "rejected", "failed", "uncertain"] as const),
  adapter_id: opaqueId,
  observed_at: timestamp,
  before_revision: Type.Union([opaqueId, Type.Null()]),
  after_revision: Type.Union([opaqueId, Type.Null()]),
  result: Type.Union([result, Type.Null()]),
  provenance: Type.Array(ProvenanceEntrySchema, { maxItems: 256 }),
  error: Type.Union([SemanticErrorSchema, Type.Null()]),
});

export const QueryResponseSchema = responseEnvelope(QueryResultSchema);
export const ActResponseSchema = responseEnvelope(ActResultSchema);
export const VerifyResponseSchema = responseEnvelope(VerifyResultSchema);
export const RunResponseSchema = responseEnvelope(RunResultSchema);
export const SemanticResponseSchema = Type.Union([
  QueryResponseSchema,
  ActResponseSchema,
  VerifyResponseSchema,
  RunResponseSchema,
]);

export const SemanticProtocolMessageSchema = Type.Union([
  SemanticRequestSchema,
  SemanticResponseSchema,
], {
  $schema: "https://json-schema.org/draft/2020-12/schema",
  $id: "https://ghost.ai/protocol/semantic-v1.schema.json",
  title: "Ghost Semantic Computer Protocol v1",
});

export type QueryPayload = Static<typeof QueryPayloadSchema>;
export type ActPayload = Static<typeof ActPayloadSchema>;
export type VerifyPayload = Static<typeof VerifyPayloadSchema>;
export type RunPayload = Static<typeof RunPayloadSchema>;
export type TaskCompletePayload = Static<typeof TaskCompletePayloadSchema>;
export type QueryResult = Static<typeof QueryResultSchema>;
export type ActResult = Static<typeof ActResultSchema>;
export type VerifyResult = Static<typeof VerifyResultSchema>;
export type RunResult = Static<typeof RunResultSchema>;
export type SemanticRequest = Static<typeof SemanticRequestSchema>;
export type SemanticResponse = Static<typeof SemanticResponseSchema>;
export type SemanticError = Static<typeof SemanticErrorSchema>;
export type CapabilitySummaryRecord = Static<typeof CapabilitySummaryRecordSchema>;
export type ResourceCapabilityDetailRecord = Static<typeof ResourceCapabilityDetailRecordSchema>;

export class SemanticValidationError extends Error {
  readonly errors: string[];
  constructor(message: string, errors: string[] = []) {
    super(message);
    this.name = "SemanticValidationError";
    this.errors = errors;
  }
}

function validationErrors(schema: TSchema, value: unknown): string[] {
  return Errors(schema, value).slice(0, 20).map((error) =>
    `${error.instancePath || "/"}: ${error.message}`,
  );
}

function checked<T>(schema: TSchema, value: unknown, label: string): T {
  assertZeroImageContent(value);
  if (!Check(schema, value)) {
    throw new SemanticValidationError(label, validationErrors(schema, value));
  }
  return value as T;
}

export function validateSemanticRequest(value: unknown): SemanticRequest {
  return checked(SemanticRequestSchema, value, "Invalid semantic protocol request");
}

export function validateSemanticResponse(value: unknown): SemanticResponse {
  return checked(SemanticResponseSchema, value, "Invalid semantic protocol response");
}

const forbiddenImageKeys = new Set([
  "image", "images", "image_url", "imageurl", "screenshot", "screenshots",
]);

/** Reject image blocks, image fields and encoded pixels at the transport boundary. */
export function assertZeroImageContent(
  value: unknown,
  path = "$",
  seen = new Set<object>(),
  depth = 0,
): void {
  if (depth > 32) throw new SemanticValidationError(`Content nesting exceeds limit at ${path}`);
  if (typeof value === "string") {
    if (/^\s*data:image\//i.test(value)) {
      throw new SemanticValidationError(`Image content is forbidden at ${path}`);
    }
    return;
  }
  if (value === null || typeof value !== "object") return;
  if (seen.has(value)) throw new SemanticValidationError(`Cyclic content is forbidden at ${path}`);
  seen.add(value);
  if (Array.isArray(value)) {
    value.forEach((child, index) => assertZeroImageContent(child, `${path}[${index}]`, seen, depth + 1));
    seen.delete(value);
    return;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.type === "string" && /^(?:input_)?image(?:_url)?$/i.test(record.type)) {
    throw new SemanticValidationError(`Image content block is forbidden at ${path}`);
  }
  for (const [key, child] of Object.entries(record)) {
    const normalized = key.toLowerCase();
    if (forbiddenImageKeys.has(normalized)) {
      throw new SemanticValidationError(`Image field ${key} is forbidden at ${path}`);
    }
    if (["mimetype", "media_type", "content_type"].includes(normalized)
      && typeof child === "string" && /^image\//i.test(child)) {
      throw new SemanticValidationError(`Image media type is forbidden at ${path}.${key}`);
    }
    assertZeroImageContent(child, `${path}.${key}`, seen, depth + 1);
  }
  seen.delete(value);
}
