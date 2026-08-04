import { createServer } from 'node:http';
import assert from 'node:assert/strict';
import { createComputerTools } from '../src/computerTools.js';

async function main(): Promise<void> {
  const commands: string[] = [];
  const server = createServer((request, response) => {
    let body = '';
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      const parsed = body ? JSON.parse(body) as { command?: string } : {};
      if (parsed.command) commands.push(parsed.command);
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ steps: commands.length }));
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
    const ordinary = createComputerTools(client)
      .find((tool) => tool.name === 'task_complete');
    assert(ordinary);
    const done = await ordinary.execute(
      'call-1', { summary: 'done' }, new AbortController().signal,
    );
    assert.equal(commands.at(-1), 'DONE');
    assert.equal(done.terminate, true);
    assert.deepEqual(done.details, { infeasible: false, terminal: true });

    const impossible = await ordinary.execute(
      'call-2', { summary: 'impossible', infeasible: true }, new AbortController().signal,
    );
    assert.equal(commands.at(-1), 'FAIL');
    assert.equal(impossible.terminate, true);
    assert.deepEqual(impossible.details, { infeasible: true, terminal: true });

    const gated = createComputerTools(client, true)
      .find((tool) => tool.name === 'task_complete');
    assert(gated);
    const challenge = await gated.execute(
      'call-3', { summary: 'maybe' }, new AbortController().signal,
    );
    assert.equal(commands.at(-1), 'WAIT');
    assert.notEqual(challenge.terminate, true);

    const accepted = await gated.execute(
      'call-4', { summary: 'verified' }, new AbortController().signal,
    );
    assert.equal(commands.at(-1), 'DONE');
    assert.equal(accepted.terminate, true);

    process.stdout.write('PASS task_complete records DONE/FAIL before terminal result\n');
    process.stdout.write('PASS verification gate is nonterminal once, then terminal\n');
  } finally {
    server.close();
  }
}

void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
