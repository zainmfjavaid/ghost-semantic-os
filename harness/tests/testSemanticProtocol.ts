import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

import { Check } from "typebox/value";

import {
  ActPayloadSchema,
  CapabilitySummaryRecordSchema,
  QueryPayloadSchema,
  ResourceCapabilityDetailRecordSchema,
  SEMANTIC_PROTOCOL_VERSION,
  SemanticProtocolMessageSchema,
  SemanticValidationError,
  TaskCompletePayloadSchema,
  VerifyPayloadSchema,
  assertZeroImageContent,
  validateSemanticRequest,
  validateSemanticResponse,
} from "../src/semantic/protocol.js";
import { QueryToolPayloadSchema } from "../src/semantic/tools.js";

const query = {
  resource: "browser.controls",
  scope: { adapter: "chrome", surface: "active_tab" },
  where: {
    op: "all",
    filters: [
      { op: "eq", field: "role", value: "button" },
      { op: "is_true", field: "enabled" },
    ],
  },
  fields: ["ref", "role", "name", "actions"],
  order_by: [{ field: "name", direction: "asc" }],
  parameters: {},
  limit: 20,
  freshness: "live",
} as const;
assert.equal(Check(QueryPayloadSchema, query), true);
assert.equal(Check(QueryToolPayloadSchema, {
  resource: "system.capabilities",
}), true, "model query tool accepts canonical default omissions");

const capabilityBase = {
  ref: "opaque-capability-ref",
  kind: "system.capability",
  name: "browser.elements",
  adapter_id: "browser.cdp@1",
  resources: ["browser.elements"],
  description: "Queryable semantic resource; inspect this ref for its schemas",
  states: {},
  advertised_actions: [],
  revision: "capability-revision",
  source: "semantic.kernel@1",
  freshness: "live",
} as const;
assert.equal(Check(CapabilitySummaryRecordSchema, {
  ...capabilityBase,
  capability_type: "resource",
  resource: "browser.elements",
  actions: ["invoke", "set_text"],
}), true, "resource summaries are compact typed discovery records");
const { description: _summaryDescription, ...capabilityDetailBase } = capabilityBase;
assert.equal(Check(ResourceCapabilityDetailRecordSchema, {
  ...capabilityDetailBase,
  capability_type: "resource_descriptor",
  application: "Chrome",
  resource: "browser.elements",
  field_schema: { type: "object", additionalProperties: true },
  parameter_schema: { type: "object", properties: {}, additionalProperties: true },
  actions: ["invoke"],
  action_schemas: { invoke: { arguments_schema: { type: "object" } } },
  verification_schema: {
    resource: "browser.elements", freshness: ["live"], operators: ["exists"],
  },
}), true, "resource detail contains only that resource's typed schemas");
assert.equal(Check(ResourceCapabilityDetailRecordSchema, {
  ...capabilityDetailBase,
  capability_type: "resource_descriptor",
  application: "Chrome",
  resource: "browser.elements",
  field_schema: {},
  parameter_schema: {},
  actions: [],
  action_schemas: {},
  verification_schema: {},
  resource_schemas: { "browser.tabs": {} },
}), false, "resource detail rejects whole-adapter schema leakage");

const request = {
  protocol_version: SEMANTIC_PROTOCOL_VERSION,
  request_id: "req-1",
  episode_id: "episode-1",
  operation: "query",
  payload: query,
};
assert.deepEqual(validateSemanticRequest(request), request);

const response = {
  protocol_version: SEMANTIC_PROTOCOL_VERSION,
  request_id: "req-1",
  status: "ok",
  adapter_id: "chrome-cdp",
  observed_at: "2026-08-02T12:00:00Z",
  before_revision: "rev-7",
  after_revision: "rev-7",
  result: {
    records: [{ ref: "opaque-control-ref", role: "button", name: "Save", enabled: true }],
    next_cursor: null,
    truncated: false,
    total: 1,
    overflow_handle: "overflow-1",
    data_handle: "overflow-1",
  },
  provenance: [{
    source: "browser.controls", freshness: "live", execution_path: "app_bridge",
  }],
  error: null,
};
assert.deepEqual(validateSemanticResponse(response), response);

const semanticTargetAct = {
  target: {
    resource: "browser.controls",
    scope: { adapter: "chrome", surface: "active_tab" },
    where: { op: "eq", field: "name", value: "Save" },
  },
  action: "press",
  arguments: {},
  expected_revision: "rev-7",
  preconditions: [{
    resource: "browser.controls",
    scope: { adapter: "chrome" },
    where: { op: "eq", field: "name", value: "Save" },
    assert: { op: "exists" },
  }],
  postconditions: [{
    all: [
      {
        resource: "artifacts",
        scope: { path: "/home/oai/share/report.pdf" },
        assert: { op: "exists" },
      },
      { not: {
        resource: "system.pending_state",
        scope: {},
        assert: { op: "contains", field: "pending", value: "save" },
      } },
    ],
  }],
  timeout_ms: 10_000,
  idempotency_key: "save-report-once",
  confirm: false,
};
assert.equal(Check(ActPayloadSchema, semanticTargetAct), true);
assert.equal(Check(ActPayloadSchema, {
  ...semanticTargetAct,
  target: { ref: "one", resource: "browser.controls", scope: {}, where: { op: "is_true", field: "x" } },
}), false, "act target must use exactly one target form");

const verification = {
  mode: "all",
  assertions: [{
    claim_id: "artifact-saved",
    query: {
      resource: "artifacts",
      scope: { path: "/home/oai/share/report.pdf" },
      fields: ["path", "exists"],
      order_by: [],
      parameters: {},
      limit: 1,
      freshness: "live",
    },
    assert: { op: "exists" },
  }],
  freshness: "live",
};
assert.equal(Check(VerifyPayloadSchema, verification), true);
assert.equal(Check(VerifyPayloadSchema, {
  ...verification,
  reconcile_action: { receipt_id: "act-uncertain", outcome: "none" },
}), true, "verify may reconcile one exact uncertain receipt from live evidence");
assert.equal(Check(VerifyPayloadSchema, {
  ...verification,
  reconcile_action: { receipt_id: "act-uncertain", outcome: "unknown" },
}), false, "reconciliation cannot preserve or invent an unknown outcome");
assert.equal(Check(QueryPayloadSchema, {
  resource: "browser.controls", scope: {}, where: {}, order_by: [], parameters: {},
  freshness: "cache_ok",
}), true, "empty where is match-all and optional fields use canonical defaults");
assert.equal(Check(QueryPayloadSchema, { ...query, limit: 101 }), false);
assert.equal(Check(QueryPayloadSchema, {
  ...query,
  order_by: [
    { field: "a", direction: "asc" },
    { field: "b", direction: "asc" },
    { field: "c", direction: "asc" },
  ],
}), false);

const actResponse = {
  ...response,
  request_id: "req-act",
  result: {
    status: "applied",
    execution_path: "native_api",
    receipt_id: "receipt-1",
    before_revision: "rev-7",
    after_revision: "rev-8",
    delta: { changed_field: "value" },
    side_effects: [],
    postconditions: [{ verdict: "pass" }],
    error: null,
  },
  before_revision: "rev-7",
  after_revision: "rev-8",
};
assert.doesNotThrow(() => validateSemanticResponse(actResponse));

const verifyResponse = {
  ...response,
  request_id: "req-verify",
  result: {
    verification_id: "verify-1",
    verdict: "pass",
    claims: [{ claim_id: "artifact-saved", verdict: "pass", observed: true, evidence_ids: ["e-1"] }],
    dependencies: [
      { surface: "surface-1", revision: "rev-8" },
      { artifact: "artifact-1", hash: "sha256:opaque" },
    ],
    evidence: [{ evidence_id: "e-1", value: true }],
    observed_at: "2026-08-02T12:00:00Z",
  },
};
assert.doesNotThrow(() => validateSemanticResponse(verifyResponse));
assert.doesNotThrow(() => validateSemanticResponse({
  ...verifyResponse,
  result: {
    ...verifyResponse.result,
    reconciliation: {
      reconciliation_id: "recon-1",
      action_receipt_id: "act-uncertain",
      verification_id: "verify-1",
      verification_fingerprint: "sha256-opaque",
      outcome: "none",
      observed_at: "2026-08-02T12:00:00Z",
    },
  },
}));

const runResponse = {
  ...response,
  request_id: "req-run",
  status: "partial",
  result: {
    value: null,
    output: [],
    operation_count: 3,
    applied_operations: 2,
    failed_operation: {
      index: 2,
      error: {
        code: "postcondition_failed",
        message: "Third operation did not satisfy its postcondition.",
        retryable: false,
        side_effect_state: "none",
        missing_capability: null,
        candidates: [],
        recovery: { allowed_operations: ["query", "verify"] },
      },
    },
  },
};
assert.doesNotThrow(() => validateSemanticResponse(runResponse));

const completion = {
  summary: "The requested artifact is saved.",
  infeasible: false,
  claims: [{ claim: "The PDF exists at the requested path.", verification_id: "verify-1" }],
  evidence_ids: ["evidence-1"],
};
assert.equal(Check(TaskCompletePayloadSchema, completion), true);
assert.equal(Check(SemanticProtocolMessageSchema, completion), false,
  "task_complete is lifecycle state, not a computer wire operation");

assert.throws(
  () => validateSemanticRequest({ ...request, operation: "computer.query" }),
  SemanticValidationError,
);
assert.throws(
  () => validateSemanticRequest({ ...request, undocumented: true }),
  SemanticValidationError,
);
assert.throws(
  () => assertZeroImageContent({ content: [{ type: "image_url", image_url: "https://invalid.test/a.png" }] }),
  /Image content/,
);
assert.throws(
  () => assertZeroImageContent({ payload: "data:image/png;base64,AAAA" }),
  /Image content/,
);
assert.throws(
  () => validateSemanticResponse({ ...response, result: { ...response.result, screenshot: "pixels" } }),
  SemanticValidationError,
);

const generated = JSON.parse(await readFile(
  resolve(import.meta.dirname, "../../protocol/semantic-v1.schema.json"),
  "utf8",
)) as { $schema?: string; $id?: string; [key: string]: unknown };
assert.equal(generated.$schema, "https://json-schema.org/draft/2020-12/schema");
assert.equal(generated.$id, "https://ghost.ai/protocol/semantic-v1.schema.json");
const generatedText = JSON.stringify(generated);
assert.doesNotMatch(generatedText, /"\$ref":"(?:Filter|JsonValue|Assertion|VerifyExpression)"/);
assert.match(generatedText, /"\$anchor":"(?:filter|jsonvalue|assertion|verifyexpression)_[0-9]+"/);

const sharedFixtures = JSON.parse(await readFile(
  resolve(import.meta.dirname, "../../protocol/fixtures/semantic-v1-conformance.json"),
  "utf8",
)) as { valid: unknown[]; invalid: unknown[] };
for (const value of sharedFixtures.valid) {
  assert.equal(Check(SemanticProtocolMessageSchema, value), true,
    `shared valid fixture rejected: ${JSON.stringify(value)}`);
}
for (const value of sharedFixtures.invalid) {
  assert.equal(Check(SemanticProtocolMessageSchema, value), false,
    `shared invalid fixture accepted: ${JSON.stringify(value)}`);
}

console.log("PASS canonical 1.0 envelopes and unprefixed wire operations validate");
console.log("PASS canonical query bounds, match-all filters, assertions, and exact-one act targets validate");
console.log("PASS act, verify, and partial-run receipts expose the canonical result fields");
console.log("PASS task_complete remains outside the computer protocol envelope");
console.log("PASS image content and undocumented fields are rejected");
console.log("PASS TypeScript and Python share canonical conformance fixtures");
