import assert from 'node:assert/strict';
import {
  canonicalSemanticToolName,
  createSemanticRuntimeTools,
  semanticToolTransport,
  transportSemanticToolName,
} from '../src/semanticRuntimeTools.js';

async function main(): Promise<void> {
  assert.equal(semanticToolTransport('openrouter', 'anthropic/claude-opus-5'), 'anthropic');
  assert.equal(semanticToolTransport('anthropic', 'claude-opus-5'), 'anthropic');
  assert.equal(semanticToolTransport('openrouter', 'qwen/qwen3.6-27b'), 'canonical');

  assert.equal(transportSemanticToolName('computer.query', 'anthropic'), 'computer_query');
  assert.equal(transportSemanticToolName('task_complete', 'anthropic'), 'task_complete');
  assert.equal(canonicalSemanticToolName('computer_query'), 'computer.query');
  assert.equal(canonicalSemanticToolName('task_complete'), 'task_complete');
  assert.equal(canonicalSemanticToolName('unrelated_tool'), 'unrelated_tool');

  const client = { baseUrl: 'http://semantic.invalid', episodeId: 'episode-test' };
  const canonical = createSemanticRuntimeTools(client, () => {}, 'canonical');
  assert.deepEqual(
    canonical.map((tool) => tool.name),
    ['computer.query', 'computer.act', 'computer.verify', 'computer.run', 'task_complete'],
  );

  const anthropic = createSemanticRuntimeTools(client, () => {}, 'anthropic');
  assert.deepEqual(
    anthropic.map((tool) => tool.name),
    ['computer_query', 'computer_act', 'computer_verify', 'computer_run', 'task_complete'],
  );
  assert.deepEqual(
    anthropic.map((tool) => canonicalSemanticToolName(tool.name)),
    ['computer.query', 'computer.act', 'computer.verify', 'computer.run', 'task_complete'],
  );

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
      result: {
        records: [
          { kind: 'research.result', collection_handle: 'collection-test' },
          { kind: 'research.result', collection_handle: 'collection-test' },
        ],
        truncated: false,
        next_cursor: null,
        total: 2,
        overflow_handle: 'overflow-test',
        data_handle: 'overflow-test',
      },
      provenance: [],
      error: null,
    }), { status: 200, headers: { 'content-type': 'application/json' } });
  };
  try {
    const result = await anthropic[0].execute('call-1', { resource: 'system.health' });
    assert.equal(dispatched?.operation, 'query');
    assert.equal(
      (result.details as { semantic?: { kind?: string } } | undefined)?.semantic?.kind,
      'query',
    );
    assert.equal(
      (result.details as { semantic?: { collection_handle?: string } } | undefined)
        ?.semantic?.collection_handle,
      'collection-test',
    );
    assert.equal(
      (result.details as { semantic?: { overflow_handle?: string } } | undefined)
        ?.semantic?.overflow_handle,
      'overflow-test',
    );
  } finally {
    globalThis.fetch = originalFetch;
  }

  console.log('semantic provider tool-name tests passed');
}

await main();
