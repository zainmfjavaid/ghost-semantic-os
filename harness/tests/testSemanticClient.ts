import assert from "node:assert/strict";

import {
  SemanticComputerClient,
  SemanticRemoteError,
  type SemanticTransport,
} from "../src/semantic/client.js";
import {
  SEMANTIC_PROTOCOL_VERSION,
  SemanticValidationError,
  type SemanticRequest,
} from "../src/semantic/protocol.js";

class MockTransport implements SemanticTransport {
  lastRequest?: SemanticRequest;
  constructor(readonly respond: (request: SemanticRequest) => unknown) {}
  async send(request: SemanticRequest): Promise<unknown> {
    this.lastRequest = request;
    return this.respond(request);
  }
}

const success = (request: SemanticRequest) => ({
  protocol_version: SEMANTIC_PROTOCOL_VERSION,
  request_id: request.request_id,
  status: "ok",
  adapter_id: "test-adapter",
  observed_at: "2026-08-02T12:00:00Z",
  before_revision: "r1",
  after_revision: "r1",
  result: { records: [], next_cursor: null, truncated: false, total: 0 },
  provenance: [{ source: "browser.controls", freshness: "live" }],
  error: null,
});

const transport = new MockTransport(success);
const client = new SemanticComputerClient(transport, {
  episodeId: "episode-fixed",
  requestId: () => "request-fixed",
});
const result = await client.query({
  resource: "browser.controls",
  scope: {},
  fields: ["name"],
  order_by: [],
  parameters: {},
  limit: 10,
  freshness: "live",
});
assert.equal(transport.lastRequest?.protocol_version, "1.0");
assert.equal(transport.lastRequest?.episode_id, "episode-fixed");
assert.equal(transport.lastRequest?.operation, "query");
assert.deepEqual(result.records, []);

const failure = new SemanticComputerClient(new MockTransport((request) => ({
  protocol_version: SEMANTIC_PROTOCOL_VERSION,
  request_id: request.request_id,
  status: "failed",
  adapter_id: "test-adapter",
  observed_at: "2026-08-02T12:00:00Z",
  before_revision: "r1",
  after_revision: null,
  result: null,
  provenance: [],
  error: {
    code: "stale_ref",
    message: "The opaque ref is stale.",
    retryable: true,
    side_effect_state: "none",
    missing_capability: null,
    candidates: [],
    recovery: { allowed_operations: ["query"] },
  },
})), { episodeId: "episode-fixed", requestId: () => "failure-fixed" });
await assert.rejects(
  failure.query({
    resource: "browser.controls", scope: {}, fields: [], order_by: [], parameters: {},
    limit: 10, freshness: "live",
  }),
  SemanticRemoteError,
);

const mismatch = new SemanticComputerClient(new MockTransport((request) => ({
  ...success(request), request_id: "wrong-request",
})), { episodeId: "episode-fixed", requestId: () => "expected-request" });
await assert.rejects(
  mismatch.query({
    resource: "browser.controls", scope: {}, fields: [], order_by: [], parameters: {},
    limit: 10, freshness: "live",
  }),
  /request_id mismatch/,
);

const imageLeak = new SemanticComputerClient(new MockTransport((request) => ({
  ...success(request),
  result: { records: [{ screenshot: "data:image/png;base64,AAAA" }], next_cursor: null, truncated: false, total: 1 },
})), { episodeId: "episode-fixed", requestId: () => "image-request" });
await assert.rejects(
  imageLeak.query({
    resource: "browser.controls", scope: {}, fields: [], order_by: [], parameters: {},
    limit: 10, freshness: "live",
  }),
  SemanticValidationError,
);

console.log("PASS client attaches episode identity and maps computer.query to wire query");
console.log("PASS typed remote failures and response-correlation failures remain distinct");
console.log("PASS image-bearing adapter responses are rejected at the transport boundary");
