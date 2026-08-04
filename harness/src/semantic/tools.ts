import { Type, type TSchema } from "typebox";

import {
  ActPayloadSchema,
  QueryPayloadSchema,
  RunPayloadSchema,
  TaskCompletePayloadSchema,
  VerifyPayloadSchema,
} from "./protocol.js";

// The wire protocol is normalized and intentionally explicit, while the
// model-facing tool accepts omission of fields with canonical defaults. This
// keeps harmless omissions from consuming a model tool call before the
// request reaches the semantic kernel.
const queryProperties = QueryPayloadSchema.properties;
export const QueryToolPayloadSchema = Type.Object({
  resource: queryProperties.resource,
  scope: Type.Optional(queryProperties.scope),
  where: Type.Optional(queryProperties.where),
  fields: Type.Optional(queryProperties.fields),
  order_by: Type.Optional(queryProperties.order_by),
  parameters: Type.Optional(queryProperties.parameters),
  limit: Type.Optional(queryProperties.limit),
  cursor: Type.Optional(queryProperties.cursor),
  freshness: Type.Optional(queryProperties.freshness),
}, { additionalProperties: false });

const actProperties = ActPayloadSchema.properties;
export const ActToolPayloadSchema = Type.Object({
  target: actProperties.target,
  action: actProperties.action,
  arguments: Type.Optional(actProperties.arguments),
  expected_revision: Type.Optional(actProperties.expected_revision),
  preconditions: Type.Optional(actProperties.preconditions),
  postconditions: Type.Optional(actProperties.postconditions),
  timeout_ms: Type.Optional(actProperties.timeout_ms),
  idempotency_key: Type.Optional(actProperties.idempotency_key),
  confirm: Type.Optional(actProperties.confirm),
}, { additionalProperties: false });

export interface SemanticToolDefinition {
  readonly name: "computer.query" | "computer.act" | "computer.verify" | "computer.run" | "task_complete";
  readonly description: string;
  readonly inputSchema: TSchema;
}

/** Tool names are model-facing; the client maps computer.* to unprefixed wire operations. */
export const SEMANTIC_TOOL_DEFINITIONS: readonly SemanticToolDefinition[] = [
  {
    name: "computer.query",
    description: "Query an advertised semantic resource. In system.capabilities results, use exact names from resources; adapter_id/name/kind/ref are not resource names. Inspect one capability via system.capability with scope.ref. Read a kernel overflow_handle via system.data_handle with scope.ref. Pass an adapter-owned collection_handle only to the owning resource's advertised parameter.",
    inputSchema: QueryToolPayloadSchema,
  },
  {
    name: "computer.act",
    description: "Apply one semantic action to exactly one target with revision and condition controls.",
    inputSchema: ActToolPayloadSchema,
  },
  {
    name: "computer.verify",
    description: "Evaluate current, read-only semantic assertions and return reusable evidence. A passing live verification may reconcile one exact uncertain action receipt without replaying it.",
    inputSchema: VerifyPayloadSchema,
  },
  {
    name: "computer.run",
    description: "Run bounded Python-like semantic source using computer.query/act/verify and safe primitive operations. Each computer method accepts either one request dictionary or ordinary keyword fields and returns its result object directly (for example rows['records']), not the outer wire envelope. Imports, open, eval/exec, shell, f-strings, functions, arbitrary attributes, UI injection, and browser JS are unavailable.",
    inputSchema: RunPayloadSchema,
  },
  {
    name: "task_complete",
    description: "Submit lifecycle completion claims backed by current verification and evidence IDs.",
    inputSchema: TaskCompletePayloadSchema,
  },
] as const;
