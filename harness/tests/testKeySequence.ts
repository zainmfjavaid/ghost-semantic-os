import { createServer } from 'node:http';
import assert from 'node:assert/strict';
import {
  createComputerTools, createSemanticDesktopTools, createSomTools, createWebTools,
} from '../src/computerTools.js';

async function main(): Promise<void> {
  const commands: string[] = [];
  const requests: Array<{ url: string; body: Record<string, unknown> }> = [];
  const server = createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      const parsed = body ? JSON.parse(body) as Record<string, unknown> : {};
      requests.push({ url: request.url ?? '', body: parsed });
      if (typeof parsed.command === 'string') commands.push(parsed.command);
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({
        steps: commands.length,
        ...((request.url ?? '').endsWith('/element/find') ? { candidate_count: 1 } : {}),
        ...((request.url ?? '').endsWith('/obs') ? {
          desktop_accessibility_ready: true,
          desktop_surfaces: [{
            role: 'alert', name: 'Confirm operation', states: ['showing'],
            context: ['application:Google Chrome', 'frame:Example'],
          }],
        } : {}),
      }));
    });
  });

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const address = server.address();
  assert(address && typeof address !== 'string');
  const client = {
    episodeId: 'test',
    baseUrl: `http://127.0.0.1:${address.port}`,
  };

  try {
    const key = createComputerTools(client).find((tool) => tool.name === 'key');
    assert(key);
    await key.execute(
      'call-1', { keys: 'ctrl+a backspace' }, new AbortController().signal,
    );
    assert.match(commands.at(-1) ?? '', /pyautogui\.hotkey\('ctrl', 'a'\)/);
    assert.match(commands.at(-1) ?? '', /pyautogui\.press\('backspace'\)/);

    const commandCount = commands.length;
    const rejected = await key.execute(
      'call-2', { keys: 'xdotool key Return' }, new AbortController().signal,
    );
    assert.equal(commands.length, commandCount);
    assert.match(
      rejected.content.map((item) => item.type === 'text' ? item.text : '').join(' '),
      /not shell commands/i,
    );

    const webFirstNames = new Set(
      createWebTools(client, true, true).map((tool) => tool.name),
    );
    assert(webFirstNames.has('key'));
    assert(webFirstNames.has('type_text'));
    assert(webFirstNames.has('scroll'));
    assert(!webFirstNames.has('click_element'));
    const desktopSomNames = new Set(
      createSomTools(client, true).map((tool) => tool.name),
    );
    assert(desktopSomNames.has('desktop_find'));
    assert(desktopSomNames.has('desktop_actions'));
    assert(desktopSomNames.has('computer_exec'));
    assert(desktopSomNames.has('computer_python'));
    const semanticDesktopNames = new Set(
      createSemanticDesktopTools(client, true).map((tool) => tool.name),
    );
    assert(semanticDesktopNames.has('desktop_find'));
    assert(semanticDesktopNames.has('desktop_actions'));
    assert(semanticDesktopNames.has('computer_exec'));
    assert(semanticDesktopNames.has('computer_python'));
    assert(semanticDesktopNames.has('key'));
    assert(!semanticDesktopNames.has('click_element'));
    assert(!semanticDesktopNames.has('click'));
    assert(!semanticDesktopNames.has('python'));
    const hybridSomTools = createWebTools(
      client, true, true, false, false, false, true,
    );
    const hybridSomNames = new Set(hybridSomTools.map((tool) => tool.name));
    assert(hybridSomNames.has('desktop_find'));
    assert(hybridSomNames.has('desktop_click'));
    assert(hybridSomNames.has('desktop_hover'));
    assert(hybridSomNames.has('desktop_type'));
    assert(hybridSomNames.has('desktop_actions'));
    assert(hybridSomNames.has('ui_actions'));
    assert(hybridSomNames.has('computer_exec'));
    assert(hybridSomNames.has('computer_python'));
    assert(hybridSomNames.has('web_search'));
    assert(hybridSomNames.has('web_read_pages'));
    assert(!hybridSomNames.has('click_element'));
    assert(!hybridSomNames.has('type_into_element'));
    assert(!hybridSomNames.has('click'));
    assert(!hybridSomNames.has('python'));

    const uiActions = hybridSomTools.find((tool) => tool.name === 'ui_actions');
    assert(uiActions);
    const crossSurfaceStart = requests.length;
    const crossSurfaceResult = await uiActions.execute(
      'cross-surface',
      {
        actions: [
          { surface: 'web', op: 'navigate', url: 'https://example.com/item' },
          {
            surface: 'web', op: 'click', by: 'role', role: 'button',
            name: 'Continue', exact: true, after_ms: 700,
          },
          {
            surface: 'desktop', op: 'click', query: 'Confirm',
            role: 'push-button', context: 'alert:Confirm operation',
          },
          { surface: 'wait', op: 'wait', seconds: 0.25 },
        ],
      },
      new AbortController().signal,
    );
    assert.match(
      crossSurfaceResult.content
        .map((item) => item.type === 'text' ? item.text : '').join('\n'),
      /Current desktop surfaces[\s\S]*Confirm operation/,
    );
    const crossSurface = requests.slice(crossSurfaceStart);
    assert.deepEqual(crossSurface.map((request) => request.url), [
      '/episodes/test/web', '/episodes/test/web',
      '/episodes/test/element/match', '/episodes/test/step', '/episodes/test/obs',
    ]);
    assert.deepEqual(crossSurface[0]?.body, {
      action: 'navigate', compact: true, observe: false,
      url: 'https://example.com/item',
    });
    assert.deepEqual(crossSurface[1]?.body, {
      action: 'actions', compact: true, observe: false,
      actions: [{
        op: 'click', by: 'role', role: 'button', name: 'Continue',
        exact: true, after_ms: 700,
      }],
    });
    assert.deepEqual(crossSurface[2]?.body, {
      query: 'Confirm', role: 'push-button', context: 'alert:Confirm operation',
      action: 'click',
    });
    assert.deepEqual(crossSurface[3]?.body, { command: 'WAIT', pause: 0.25 });

    const computerExec = hybridSomTools.find(
      (tool) => tool.name === 'computer_exec',
    );
    assert(computerExec);
    await computerExec.execute(
      'guest-exec',
      {
        script: 'printf hello', timeout_seconds: 12,
        working_dir: '/home/oai/share',
      },
      new AbortController().signal,
    );
    const execRequest = requests.at(-1);
    assert.equal(execRequest?.url, '/episodes/test/exec');
    assert.deepEqual(execRequest?.body, {
      script: 'printf hello', language: 'bash', timeout_seconds: 12,
      working_dir: '/home/oai/share',
    });

    const computerPython = hybridSomTools.find(
      (tool) => tool.name === 'computer_python',
    );
    assert(computerPython);
    await computerPython.execute(
      'guest-python',
      {
        code: 'print(6 * 7)', timeout_seconds: 9,
        working_dir: '/home/user',
      },
      new AbortController().signal,
    );
    const pythonRequest = requests.at(-1);
    assert.equal(pythonRequest?.url, '/episodes/test/exec');
    assert.deepEqual(pythonRequest?.body, {
      script: 'print(6 * 7)', language: 'python', timeout_seconds: 9,
      working_dir: '/home/user',
    });

    const webSearch = hybridSomTools.find((tool) => tool.name === 'web_search');
    assert(webSearch);
    await webSearch.execute(
      'research', { queries: ['alpha', 'beta'], result_limit: 4 },
      new AbortController().signal,
    );
    const searchRequest = requests.at(-1);
    assert.equal(searchRequest?.url, '/episodes/test/web');
    assert.deepEqual(searchRequest?.body, {
      action: 'search', compact: false, observe: false,
      queries: ['alpha', 'beta'], result_limit: 4,
    });

    const desktopClick = hybridSomTools.find(
      (tool) => tool.name === 'desktop_click',
    );
    assert(desktopClick);
    await desktopClick.execute(
      'semantic-call',
      {
        query: 'Mode', role: 'combo-box', state: 'expanded',
        context: 'Primary Window',
      },
      new AbortController().signal,
    );
    const semanticRequest = requests.at(-1);
    assert.equal(semanticRequest?.url, '/episodes/test/element/match');
    assert.deepEqual(semanticRequest?.body, {
      query: 'Mode', role: 'combo-box', state: 'expanded',
      context: 'Primary Window', action: 'click',
    });

    const desktopFind = hybridSomTools.find(
      (tool) => tool.name === 'desktop_find',
    );
    assert(desktopFind);
    await desktopFind.execute(
      'list-current-surface', {}, new AbortController().signal,
    );
    const listingRequest = requests.at(-1);
    assert.equal(listingRequest?.url, '/episodes/test/element/find');
    assert.deepEqual(listingRequest?.body, {});

    const desktopActions = hybridSomTools.find(
      (tool) => tool.name === 'desktop_actions',
    );
    assert(desktopActions);
    const beforeProgram = commands.length;
    await desktopActions.execute(
      'call-3',
      {
        actions: [
          {
            op: 'wait_for', query: 'Ready', role: 'status',
            context: 'Primary Window', condition: 'present',
          },
          { op: 'scroll', direction: 'down', amount: 3 },
          { op: 'key', keys: 'ctrl+alt+t' },
          { op: 'text', text: 'echo safe' },
          { op: 'key', keys: 'enter' },
        ],
      },
      new AbortController().signal,
    );
    assert.equal(commands.length, beforeProgram + 4);
    assert.match(commands[beforeProgram] ?? '', /pyautogui\.scroll\(-3\)/);
    assert.match(commands[beforeProgram + 1] ?? '', /hotkey\('ctrl', 'alt', 't'\)/);
    assert.match(commands[beforeProgram + 2] ?? '', /typewrite\('echo safe'/);
    const waitRequest = requests.findLast(
      (request) => request.url.endsWith('/element/find')
        && request.body.query === 'Ready',
    );
    assert.equal(waitRequest?.body.context, 'Primary Window');

    process.stdout.write('PASS key executes ordered key/chord sequences\n');
    process.stdout.write('PASS key rejects shell/text misuse and web-first keeps type_text\n');
    process.stdout.write('PASS desktop_actions batches safe focused text and key input\n');
    process.stdout.write('PASS ui_actions batches guarded DOM-to-native causal sequences\n');
    process.stdout.write('PASS semantic desktop filters reach the typed executor\n');
    process.stdout.write('PASS empty desktop_find requests a current-surface listing\n');
    process.stdout.write('PASS hybrid guest execution stays structured and traced\n');
    process.stdout.write('PASS guest Python and batch research stay structured and traced\n');
    process.stdout.write('PASS semantic wait conditions compose with ordered actions\n');
  } finally {
    server.close();
  }
}

void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
