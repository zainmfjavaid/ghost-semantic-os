import assert from 'node:assert/strict';
import { Check } from 'typebox/value';

import {
  QueryToolPayloadSchema,
} from '../src/semantic/tools.js';
import { VerifyPayloadSchema } from '../src/semantic/protocol.js';
import { createSemanticRuntimeTools } from '../src/semanticRuntimeTools.js';
import { normalizeModelArguments } from '../src/semantic/argumentNormalization.js';

const query = normalizeModelArguments({
  resource: 'ui.elements',
  scope: '{"surface":"surface-1"}',
  where: '{"op":"contains","field":"name","value":"Save"}',
  fields: '["name","role"]',
  order_by: '[{"field":"name","direction":"asc"}]',
  parameters: '{"include_hidden":false}',
}, QueryToolPayloadSchema);

assert.deepEqual(query.evidence?.decoded_json_paths, [
  '$.scope', '$.where', '$.fields', '$.order_by', '$.parameters',
]);
assert.deepEqual(query.value, {
  resource: 'ui.elements',
  scope: { surface: 'surface-1' },
  where: { op: 'contains', field: 'name', value: 'Save' },
  fields: ['name', 'role'],
  order_by: [{ field: 'name', direction: 'asc' }],
  parameters: { include_hidden: false },
});
assert.equal(Check(QueryToolPayloadSchema, query.value), true);
assert.equal(Check(QueryToolPayloadSchema, {
  resource: 'ui.elements', where: '{"op":"eq","field":"role","value":"button"}',
}), false, 'the advertised TypeBox schema must remain strict');

const verify = normalizeModelArguments({
  mode: 'all',
  freshness: 'live',
  assertions: JSON.stringify([{
    claim_id: 'save-exists',
    query: {
      resource: 'ui.elements',
      scope: '{"surface":"surface-1"}',
      where: '{"op":"eq","field":"name","value":"Save"}',
      order_by: '[]',
      parameters: '{}',
      freshness: 'live',
    },
    assert: { op: 'exists' },
  }]),
}, VerifyPayloadSchema);
assert.deepEqual(verify.evidence?.decoded_json_paths, [
  '$.assertions',
  '$.assertions[0].query.scope',
  '$.assertions[0].query.where',
  '$.assertions[0].query.order_by',
  '$.assertions[0].query.parameters',
]);
assert.equal(Check(VerifyPayloadSchema, verify.value), true);

const tools = createSemanticRuntimeTools(
  { baseUrl: 'http://semantic.invalid', episodeId: 'episode-test' },
);

for (const [name, invalid] of [
  ['computer.query', { resource: 'system.capabilities', source: '{}' }],
  ['computer.act', { target: { resource: 'browser.page', scope: {} }, action: 'navigate' }],
  ['computer.verify', {
    mode: 'all', freshness: 'live', assertions: [{
      claim_id: 'bad',
      query: {
        resource: 'browser.tabs', scope: {}, order_by: [], parameters: {}, freshness: 'live',
      },
      assert: { op: 'exists' },
      verify_on: 'browser.tabs',
    }],
  }],
  ['computer.run', { source: 'emit(1)' }],
] as const) {
  const tool = tools.find((candidate) => candidate.name === name);
  assert.ok(tool?.prepareArguments);
  const carrier = tool.prepareArguments(invalid);
  assert.equal(
    Check(tool.parameters, carrier), true,
    `${name} invalid arguments must be converted into a schema-valid private carrier`,
  );
  const rejected = await tool.execute(`invalid-${name}`, carrier as never);
  assert.equal(rejected.content[0]?.type, 'text');
  const response = JSON.parse((rejected.content[0] as { text: string }).text) as {
    protocol_version: string;
    status: string;
    adapter_id: string;
    result: unknown;
    error: { code: string; side_effect_state: string };
  };
  assert.equal(response.protocol_version, '1.0');
  assert.equal(response.status, 'rejected');
  assert.equal(response.adapter_id, 'semantic.tool-validation@1');
  assert.equal(response.result, null);
  assert.equal(response.error.code, 'invalid_request');
  assert.equal(response.error.side_effect_state, 'none');
  assert.equal(rejected.details.semantic.semantic_operations, 0);
}

const actTool = tools.find((tool) => tool.name === 'computer.act');
assert.ok(actTool?.prepareArguments);
const preparedAct = actTool.prepareArguments({
  target: '{"ref":"ref-1"}',
  action: 'set_text',
  arguments: '{"text":"{\\"literal\\":true}","command":"rm -rf /"}',
  preconditions: '[]',
  postconditions: '[]',
});
assert.deepEqual(preparedAct, {
  target: { ref: 'ref-1' },
  action: 'set_text',
  arguments: { text: '{"literal":true}', command: 'rm -rf /' },
  preconditions: [],
  postconditions: [],
});
assert.equal(
  (preparedAct as { arguments: { text: unknown } }).arguments.text,
  '{"literal":true}',
  'string-valued action data must never be recursively interpreted',
);

const runTool = tools.find((tool) => tool.name === 'computer.run');
assert.ok(runTool?.prepareArguments);
const runCode = '{"command":"rm -rf /","task":"benchmark-specific"}';
assert.deepEqual(runTool.prepareArguments({ code: runCode }), { code: runCode });

const malformed = normalizeModelArguments({
  resource: 'ui.elements',
  where: '{"op":"eq"',
}, QueryToolPayloadSchema);
assert.equal((malformed.value as { where: unknown }).where, '{"op":"eq"');
assert.equal(malformed.evidence, null);
assert.equal(Check(QueryToolPayloadSchema, malformed.value), false);

const wrongShape = normalizeModelArguments({
  resource: 'ui.elements',
  where: '[]',
}, QueryToolPayloadSchema);
assert.equal((wrongShape.value as { where: unknown }).where, '[]');
assert.equal(wrongShape.evidence, null);
assert.equal(Check(QueryToolPayloadSchema, wrongShape.value), false);

const originalFetch = globalThis.fetch;
let dispatched: Record<string, unknown> | undefined;
globalThis.fetch = async (_input, init) => {
  dispatched = JSON.parse(String(init?.body)) as Record<string, unknown>;
  return new Response(JSON.stringify({
    protocol_version: '1.0',
    request_id: dispatched.request_id,
    status: 'ok',
    adapter_id: 'system@1',
    observed_at: '2026-08-02T12:00:00Z',
    before_revision: null,
    after_revision: null,
    result: { records: [], truncated: false, next_cursor: null, total: 0 },
    provenance: [],
    error: null,
  }), { status: 200, headers: { 'content-type': 'application/json' } });
};
try {
  const queryTool = tools.find((tool) => tool.name === 'computer.query');
  assert.ok(queryTool?.prepareArguments);
  const preparedFirst = queryTool.prepareArguments({
    resource: 'ui.elements',
    where: '{"op":"eq","field":"role","value":"button"}',
  });
  const preparedSecond = queryTool.prepareArguments({
    resource: 'browser.tabs',
    where: '{"op":"contains","field":"url","value":"example.com"}',
  });
  assert.deepEqual((preparedFirst as { where: unknown }).where, {
    op: 'eq', field: 'role', value: 'button',
  });
  assert.deepEqual((preparedSecond as { where: unknown }).where, {
    op: 'contains', field: 'url', value: 'example.com',
  });
  const result = await queryTool.execute('call-1', preparedFirst);
  const payload = dispatched?.payload as Record<string, unknown>;
  assert.deepEqual(payload.where, { op: 'eq', field: 'role', value: 'button' });
  assert.deepEqual(payload.scope, {});
  assert.deepEqual(payload.order_by, []);
  assert.deepEqual(payload.parameters, {});
  assert.equal(payload.freshness, 'live');
  assert.equal(result.content[0]?.type, 'text');
  assert.equal(
    (result.content[0] as { text?: string }).text?.includes('normalized_args'),
    false,
    'normalization evidence belongs only in the authoritative trace',
  );
} finally {
  globalThis.fetch = originalFetch;
}

console.log('PASS schema-directed normalization repairs only valid structured JSON strings');
console.log('PASS malformed/wrong-shape JSON and string-valued code/action data remain untouched');
console.log('PASS canonical dispatch validation and raw/normalized trace evidence remain intact');
