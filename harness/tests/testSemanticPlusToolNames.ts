import assert from 'node:assert/strict';
import {
  createSemanticPlusRuntimeTools,
  SEMANTIC_PLUS_FORBIDDEN_TOOL_NAMES,
  semanticPlusExpectedToolNames,
} from '../src/semanticPlusRuntimeTools.js';

const client = { baseUrl: 'http://semantic-plus.invalid', episodeId: 'episode-plus' };

const canonical = createSemanticPlusRuntimeTools(client, () => {}, 'canonical');
assert.deepEqual(canonical.map((tool) => tool.name), [
  'computer.query',
  'computer.act',
  'computer.verify',
  'computer.run',
  'task_complete',
  'computer_exec',
  'computer_python',
  'web_elements',
  'web_find',
  'web_click',
  'web_type',
  'web_navigate',
  'web_read',
  'web_search',
  'web_read_pages',
  'web_scroll',
  'web_frames',
  'web_js',
  'web_actions',
  'web_tabs',
  'web_switch_tab',
  'web_close_tab',
]);
assert.deepEqual(canonical.map((tool) => tool.name), semanticPlusExpectedToolNames('canonical'));
assert.equal(new Set(canonical.map((tool) => tool.name)).size, canonical.length);
for (const forbidden of SEMANTIC_PLUS_FORBIDDEN_TOOL_NAMES) {
  assert.equal(canonical.some((tool) => tool.name === forbidden), false, forbidden);
}
for (const name of ['computer_exec', 'computer_python']) {
  const description = canonical.find((tool) => tool.name === name)?.description ?? '';
  assert.doesNotMatch(description, /desktop_\*/);
  assert.match(description, /computer\.query\/computer\.act/);
}

const anthropic = createSemanticPlusRuntimeTools(client, () => {}, 'anthropic');
assert.deepEqual(anthropic.map((tool) => tool.name).slice(0, 5), [
  'computer_query',
  'computer_act',
  'computer_verify',
  'computer_run',
  'task_complete',
]);
assert.deepEqual(anthropic.map((tool) => tool.name), semanticPlusExpectedToolNames('anthropic'));

const originalFetch = globalThis.fetch;
const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
globalThis.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input.url);
  const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
  calls.push({ path: url.pathname, body });
  return new Response(JSON.stringify({
    result: 'bounded text result',
    web_elements: '[0] button "Continue"',
    page_text: 'Example page',
    steps: 1,
  }), { status: 200, headers: { 'content-type': 'application/json' } });
};
try {
  const tools = new Map(canonical.map((tool) => [tool.name, tool]));
  const elements = await tools.get('web_elements')!.execute('web-call', {});
  const bash = await tools.get('computer_exec')!.execute('bash-call', {
    script: 'printf ok',
  });
  const python = await tools.get('computer_python')!.execute('python-call', {
    code: 'print("ok")',
  });

  assert.equal(calls[0]?.path, '/episodes/episode-plus/web');
  assert.equal(calls[0]?.body.observe, false);
  assert.equal(calls[0]?.body.compact, true);
  assert.equal(calls[1]?.path, '/episodes/episode-plus/exec');
  assert.equal(calls[1]?.body.language, 'bash');
  assert.equal(calls[2]?.path, '/episodes/episode-plus/exec');
  assert.equal(calls[2]?.body.language, 'python');

  for (const result of [elements, bash, python]) {
    assert.ok(result.content.length > 0);
    assert.ok(result.content.every((part) => part.type === 'text'));
  }
} finally {
  globalThis.fetch = originalFetch;
}

console.log('semantic-plus exact tool-surface and text-only transport tests passed');
