import assert from 'node:assert/strict';
import {
  createSemanticSimpleRuntimeTools,
  SEMANTIC_SIMPLE_TOOL_NAMES,
} from '../src/semanticSimpleRuntimeTools.js';
import {
  buildSemanticSimpleSystemPrompt,
  SEMANTIC_SIMPLE_PROMPT_SOURCE,
  SEMANTIC_SIMPLE_PROMPT_VERSION,
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  SEMANTIC_SIMPLE_SYSTEM_PROMPT_SHA256,
} from '../src/semanticSimpleSystemPrompt.js';
import { createHash } from 'node:crypto';

const client = { baseUrl: 'http://semantic-simple.invalid', episodeId: 'episode-simple' };
const tools = createSemanticSimpleRuntimeTools(client);
assert.deepEqual(tools.map((tool) => tool.name), [
  'read_computer',
  'computer_click',
  'computer_type',
]);
assert.deepEqual(tools.map((tool) => tool.name), [...SEMANTIC_SIMPLE_TOOL_NAMES]);
assert.equal(new Set(tools.map((tool) => tool.name)).size, 3);
assert.equal(tools.some((tool) => tool.name === 'task_complete'), false);
assert.equal(tools.some((tool) => /verify|screenshot|key|exec|javascript/i.test(tool.name)), false);

assert.equal(SEMANTIC_SIMPLE_PROMPT_VERSION, '1.4');
assert.equal(SEMANTIC_SIMPLE_PROMPT_SOURCE, 'harness/prompts/semantic-simple-v1.4.txt');
assert.equal(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT_SHA256,
  createHash('sha256').update(SEMANTIC_SIMPLE_SYSTEM_PROMPT, 'utf8').digest('hex'),
);
for (const toolName of SEMANTIC_SIMPLE_TOOL_NAMES) {
  assert.match(SEMANTIC_SIMPLE_SYSTEM_PROMPT, new RegExp(`\\b${toolName}\\b`));
}
assert.doesNotMatch(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /task_complete|computer\.query|computer\.act|computer\.verify|computer\.run|critic|planner|checklist|task card|coordinate|keyboard|\bshell\b|javascript/i,
);
assert.doesNotMatch(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /Chrome|Firefox|Thunderbird|LibreOffice|Writer|Calc|GIMP|VLC|VS Code/i,
);
assert.match(SEMANTIC_SIMPLE_SYSTEM_PROMPT, /lists every current surface/i);
assert.match(SEMANTIC_SIMPLE_SYSTEM_PROMPT, /active surface's concise current scene/i);
assert.match(SEMANTIC_SIMPLE_SYSTEM_PROMPT, /Tool results remain in the conversation/i);
assert.match(SEMANTIC_SIMPLE_SYSTEM_PROMPT, /Read web content with query="page text"/i);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /query an exact distinctive title or label from that text to recover its actionable link/i,
);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /within only with a listed container ID/i,
);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /successful click or type result is already a fresh read/i,
);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /computer_click only on a listed surface or a line advertising click/i,
);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /computer_type only on a line advertising type=/i,
);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /Never repeat a successful action when the requested state is visible/i,
);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /document marked modified is not complete.*advertised Save capability.*confirm modified is gone/is,
);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /grid, query its intended range or headers.*first intended editable cell.*tabs separate columns and newlines separate rows/is,
);
assert.match(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT,
  /moment current state proves the task is satisfied, stop naturally/i,
);
assert.match(SEMANTIC_SIMPLE_SYSTEM_PROMPT, /stop naturally/i);
assert.ok(
  SEMANTIC_SIMPLE_SYSTEM_PROMPT.length < 2_000,
  'low-context prompt should remain compact',
);
const assembledPrompt = buildSemanticSimpleSystemPrompt('Current date reference: TEST DATE.');
assert.equal(
  assembledPrompt,
  `${SEMANTIC_SIMPLE_SYSTEM_PROMPT}\nCurrent date reference: TEST DATE.`,
);

const originalFetch = globalThis.fetch;
const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
globalThis.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input.url);
  const body = init?.body ? JSON.parse(String(init.body)) as Record<string, unknown> : {};
  calls.push({ path: url.pathname, body });
  const action = url.pathname.endsWith('/simple/read') ? undefined : {
    status: url.pathname.endsWith('/simple/type') ? 'uncertain' : 'applied',
    execution_path: 'native_api',
    ...(url.pathname.endsWith('/simple/type') ? {
      error: {
        code: 'timeout',
        side_effect_state: 'unknown',
      },
    } : {}),
  };
  return new Response(JSON.stringify({
    ok: true,
    text: 'COMPUTER\n\nSurfaces\n[A] Example — active\n\nActive Surface [A] Example — active',
    active_surface: 'A',
    surface_count: 1,
    element_count: 0,
    returned_elements: 0,
    next_cursor: null,
    ...(action ? { action } : {}),
  }), { status: 200, headers: { 'content-type': 'application/json' } });
};

try {
  const byName = new Map(tools.map((tool) => [tool.name, tool]));
  const read = await byName.get('read_computer')!.execute('read-call', { query: 'Save' });
  const click = await byName.get('computer_click')!.execute('click-call', { element: 'A2' });
  const type = await byName.get('computer_type')!.execute('type-call', {
    element: 'A3', text: 'literal text',
  });

  assert.deepEqual(calls, [
    { path: '/episodes/episode-simple/simple/read', body: { query: 'Save' } },
    { path: '/episodes/episode-simple/simple/click', body: { element: 'A2' } },
    {
      path: '/episodes/episode-simple/simple/type',
      body: { element: 'A3', text: 'literal text' },
    },
  ]);
  for (const result of [read, click, type]) {
    assert.equal(result.content.length, 1);
    assert.equal(result.content[0]?.type, 'text');
    assert.match(result.content[0]?.text ?? '', /Active Surface \[A\] Example — active/);
  }
  assert.deepEqual(
    (click.details as { simpleComputer: { actionOutcome: unknown } }).simpleComputer.actionOutcome,
    {
      status: 'applied',
      executionPath: 'native_api',
      errorCode: undefined,
      sideEffectState: undefined,
    },
  );
  assert.deepEqual(
    (type.details as { simpleComputer: { actionOutcome: unknown } }).simpleComputer.actionOutcome,
    {
      status: 'uncertain',
      executionPath: 'native_api',
      errorCode: 'timeout',
      sideEffectState: 'unknown',
    },
  );

  // Pi marks a custom-tool result as an error only when execute throws. A
  // typed semantic failure must therefore reject instead of returning a
  // superficially successful text result that can mislead the policy model.
  globalThis.fetch = async () => new Response(JSON.stringify({
    ok: false,
    error: {
      code: 'stale_ref',
      message: 'element changed',
      side_effect_state: 'none',
    },
  }), { status: 200, headers: { 'content-type': 'application/json' } });
  await assert.rejects(
    () => byName.get('computer_click')!.execute('failed-click', { element: 'A2' }),
    /Computer click failed: stale_ref: element changed\. side_effect_state=none/,
  );
} finally {
  globalThis.fetch = originalFetch;
}

console.log('semantic-simple exact three-tool surface and text-only transport tests passed');
