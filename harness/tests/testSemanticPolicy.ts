import assert from 'node:assert/strict';
import {
  assertZeroImageTelemetry,
  compactSemanticContext,
  createSemanticPolicyTelemetry,
  findImagePayloadPaths,
} from '../src/semanticPolicy.js';

assert.deepEqual(findImagePayloadPaths({ messages: [{ type: 'text', text: 'ok' }] }), []);
assert.deepEqual(findImagePayloadPaths({ type: 'image', data: 'secret' }), ['$']);
assert.deepEqual(
  findImagePayloadPaths({ content: [{ image_url: { url: 'https://example.test/x.png' } }] }),
  ['$.content[0].image_url'],
);
assert.deepEqual(
  findImagePayloadPaths({ source: { type: 'base64', media_type: 'image/png', data: 'secret' } }),
  ['$.source'],
);
assert.deepEqual(findImagePayloadPaths({ data: 'data:image/jpeg;base64,secret' }), ['$.data']);

const telemetry = createSemanticPolicyTelemetry();
const messages: unknown[] = [
  { role: 'user', content: [{ type: 'text', text: 'task' }] },
  ...Array.from({ length: 10 }, (_, index) => ({
    role: 'toolResult',
    toolName: 'computer.query',
    content: [{ type: 'text', text: `result-${index}` }],
    details: {
      semantic: {
        resource: index < 2 ? 'system.surfaces' : `resource.${index}`,
        surface: 'surface-a',
        observation_id: `observation-${index}`,
        revision: `revision-${index}`,
        ...(index === 2 ? { overflow_handle: 'overflow-a' } : {}),
        ...(index === 0 ? { collection_handle: 'collection-a' } : {}),
      },
    },
  })),
  ...Array.from({ length: 14 }, (_, index) => ({
    role: 'toolResult',
    toolName: 'computer.act',
    content: [{ type: 'text', text: `action-${index}` }],
    details: { semantic: { receipt_id: `receipt-${index}` } },
  })),
];
const projected = compactSemanticContext(messages, telemetry) as Array<{
  role?: string;
  toolName?: string;
  content?: Array<{ text?: string }>;
}>;
assert.equal(projected.length, messages.length);
assert.ok(telemetry.compactionEvents > 0);
assert.ok(telemetry.supersededObservationCount >= 3);
assert.equal(telemetry.activeDataHandles, 2);
assert.ok(projected.some((message) =>
  message.content?.some((item) => item.text?.includes('semantic result superseded'))));
assert.ok(projected.some((message) =>
  message.content?.some((item) => item.text?.includes('collection_handle=collection-a'))),
  'compacted research results must retain their unique collection capability');
// The authoritative input remains untouched.
assert.equal(
  (messages[1] as { content: Array<{ text: string }> }).content[0]?.text,
  'result-0',
);

const aliasTelemetry = createSemanticPolicyTelemetry();
const aliasProjection = compactSemanticContext(Array.from({ length: 10 }, (_, index) => ({
  role: 'toolResult',
  toolName: 'computer_query',
  content: [{ type: 'text', text: `alias-result-${index}` }],
  details: { semantic: { resource: `alias.resource.${index}`, surface: 'surface-a' } },
})), aliasTelemetry) as Array<{ content?: Array<{ text?: string }> }>;
assert.equal(aliasTelemetry.expandedObservationCount, 8);
assert.equal(aliasTelemetry.supersededObservationCount, 2);
assert.ok(aliasProjection.some((message) =>
  message.content?.some((item) => item.text?.includes('semantic result superseded'))));

const scopedTelemetry = createSemanticPolicyTelemetry();
compactSemanticContext([0, 2000].map((offset) => ({
  role: 'toolResult',
  toolName: 'computer.query',
  content: [{ type: 'text', text: `file-page-${offset}` }],
  details: { semantic: {
    resource: 'filesystem.file', scope: { path: '/home/user/result.yaml' },
    parameters: { offset, length: 2000 }, cursor: null,
  } },
})), scopedTelemetry);
assert.equal(
  scopedTelemetry.expandedObservationCount, 2,
  'different query scopes/pages must not supersede each other',
);

const webTelemetry = createSemanticPolicyTelemetry();
const webMessages = [
  {
    role: 'toolResult', toolName: 'web_elements',
    content: [{ type: 'text', text: 'old element indices' }], details: { steps: 1 },
  },
  {
    role: 'toolResult', toolName: 'web_find',
    content: [{ type: 'text', text: 'new element indices' }], details: { steps: 2 },
  },
  {
    role: 'toolResult', toolName: 'web_tabs',
    content: [{ type: 'text', text: 'old tab indices' }], details: { steps: 3 },
  },
  {
    role: 'toolResult', toolName: 'web_tabs',
    content: [{ type: 'text', text: 'new tab indices' }], details: { steps: 4 },
  },
  {
    role: 'toolResult', toolName: 'web_read_pages',
    content: [{ type: 'text', text: 'unique research evidence' }], details: { steps: 5 },
  },
  {
    role: 'toolResult', toolName: 'computer_python',
    content: [{ type: 'text', text: 'unique guest-code output' }], details: { steps: 6 },
  },
];
const webProjection = compactSemanticContext(webMessages, webTelemetry) as Array<{
  content?: Array<{ text?: string }>;
}>;
assert.match(webProjection[0]?.content?.[0]?.text ?? '', /web observation superseded/);
assert.equal(webProjection[1]?.content?.[0]?.text, 'new element indices');
assert.match(webProjection[2]?.content?.[0]?.text ?? '', /web observation superseded/);
assert.equal(webProjection[3]?.content?.[0]?.text, 'new tab indices');
assert.equal(webProjection[4]?.content?.[0]?.text, 'unique research evidence');
assert.equal(webProjection[5]?.content?.[0]?.text, 'unique guest-code output');

const simpleTelemetry = createSemanticPolicyTelemetry();
const simpleProjection = compactSemanticContext([
  {
    role: 'toolResult', toolName: 'read_computer',
    content: [{ type: 'text', text: 'old full computer tree '.repeat(500) }],
    details: { simpleComputer: { kind: 'read', activeSurface: 'A' } },
  },
  {
    role: 'toolResult', toolName: 'computer_click',
    content: [{ type: 'text', text: 'new full computer tree' }],
    details: { simpleComputer: { kind: 'click', activeSurface: 'B' } },
  },
] as unknown[], simpleTelemetry) as Array<{ content?: Array<{ text?: string }> }>;
assert.equal(simpleProjection[0]?.content?.[0]?.text, 'old full computer tree '.repeat(500));
assert.equal(simpleProjection[1]?.content?.[0]?.text, 'new full computer tree');
assert.equal(simpleTelemetry.expandedObservationCount, 2);
assert.equal(simpleTelemetry.supersededObservationCount, 0);
assert.equal(simpleTelemetry.compactionEvents, 0);
assert.ok(simpleTelemetry.lastToolResultCharacters > 10_000);

const hundredCallTelemetry = createSemanticPolicyTelemetry();
const hundredCallMessages = Array.from({ length: 100 }, (_, index) => {
  const failed = index === 98;
  const kind = index % 3 === 0 ? 'read' : index % 3 === 1 ? 'click' : 'type';
  const toolName = kind === 'read'
    ? 'read_computer'
    : kind === 'click' ? 'computer_click' : 'computer_type';
  return {
    role: 'toolResult',
    toolName,
    content: [{
      type: 'text',
      text: failed
        ? 'Computer type failed: stale_ref: element is stale.'
        : `full-render-${index}:` + ' semantic element'.repeat(700),
    }],
    details: {
      simpleComputer: {
        kind,
        ok: !failed,
        activeSurface: 'A',
        ...(failed ? {
          errorCode: 'stale_ref',
          errorSideEffectState: 'none',
        } : kind === 'read' ? {} : {
          actionOutcome: {
            status: index === 97 ? 'uncertain' : 'applied',
            executionPath: 'native_api',
            ...(index === 97 ? {
              errorCode: 'timeout', sideEffectState: 'unknown',
            } : {}),
          },
        }),
      },
    },
  };
});
const authoritativeSnapshot = structuredClone(hundredCallMessages);
const hundredCallProjection = compactSemanticContext(
  hundredCallMessages, hundredCallTelemetry,
) as Array<{ content?: Array<{ text?: string }> }>;
const projectedTexts = hundredCallProjection.map(
  (message) => message.content?.[0]?.text ?? '',
);
assert.equal(
  projectedTexts.filter((text) => text.startsWith('full-render-')).length,
  99,
  'the full-history experiment must retain every successful semantic-simple render',
);
assert.match(projectedTexts[99] ?? '', /^full-render-99:/);
assert.match(
  projectedTexts[98] ?? '',
  /Computer type failed: stale_ref: element is stale/,
);
assert.match(
  projectedTexts[97] ?? '',
  /^full-render-97:/,
);
assert.ok(
  hundredCallTelemetry.lastToolResultCharacters > 1_000_000,
  'the deliberately generous causal arm must expose its real context cost',
);
assert.equal(hundredCallTelemetry.expandedObservationCount, 100);
assert.equal(hundredCallTelemetry.supersededObservationCount, 0);
assert.equal(hundredCallTelemetry.compactionEvents, 0);
assert.deepEqual(
  hundredCallMessages,
  authoritativeSnapshot,
  'provider projection must not mutate the authoritative session/trace inputs',
);

const failedLatestProjection = compactSemanticContext([
  {
    role: 'toolResult', toolName: 'read_computer',
    content: [{ type: 'text', text: 'last usable full render' }],
    details: { simpleComputer: { kind: 'read', ok: true } },
  },
  {
    role: 'toolResult', toolName: 'computer_click',
    content: [{ type: 'text', text: 'Computer click failed: stale_ref' }],
    details: { simpleComputer: {
      kind: 'click', ok: false, errorCode: 'stale_ref', errorSideEffectState: 'none',
    } },
  },
], createSemanticPolicyTelemetry()) as Array<{ content?: Array<{ text?: string }> }>;
assert.equal(
  failedLatestProjection[0]?.content?.[0]?.text,
  'last usable full render',
  'a failed latest action must not evict the last usable computer state',
);
assert.match(
  failedLatestProjection[1]?.content?.[0]?.text ?? '',
  /Computer click failed: stale_ref/,
);

assert.throws(
  () => compactSemanticContext([{
    role: 'toolResult',
    toolName: 'computer.query',
    content: [{ type: 'image', data: 'secret' }],
  }], createSemanticPolicyTelemetry()),
  /policy_violation/,
);
assert.doesNotThrow(() => assertZeroImageTelemetry(createSemanticPolicyTelemetry()));
const violated = createSemanticPolicyTelemetry();
violated.imagePartsSent = 1;
assert.throws(() => assertZeroImageTelemetry(violated), /image counters are nonzero/);

console.log('PASS semantic payload audit rejects provider-specific image forms');
console.log('PASS semantic context governor compacts only the provider projection');
console.log('PASS strict zero-image counters fail closed');
