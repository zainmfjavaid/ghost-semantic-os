import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { contentText } from '@earendil-works/pi-ai';
import { ModelRuntime } from '@earendil-works/pi-coding-agent';
import { registerOpenAICompatibleEndpoint } from '../src/modelEndpoint.js';

let requestPath = '';
let authorization = '';
const server = createServer((request, response) => {
  requestPath = request.url ?? '';
  authorization = request.headers.authorization ?? '';
  response.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  });
  response.write(`data: ${JSON.stringify({
    id: 'chatcmpl-local-test',
    object: 'chat.completion.chunk',
    created: 1,
    model: 'qwen-test',
    choices: [{ index: 0, delta: { role: 'assistant', content: 'local pong' }, finish_reason: null }],
  })}\n\n`);
  response.write(`data: ${JSON.stringify({
    id: 'chatcmpl-local-test',
    object: 'chat.completion.chunk',
    created: 1,
    model: 'qwen-test',
    choices: [{ index: 0, delta: {}, finish_reason: 'stop' }],
  })}\n\n`);
  response.end('data: [DONE]\n\n');
});
await new Promise<void>((resolve, reject) => {
  server.once('error', reject);
  server.listen(0, '127.0.0.1', resolve);
});
const address = server.address();
assert(address && typeof address === 'object');

const runtime = await ModelRuntime.create({ modelsPath: null });
registerOpenAICompatibleEndpoint(runtime, {
  provider: 'local-qwen-test',
  modelId: 'qwen-test',
  baseUrl: `http://127.0.0.1:${address.port}/v1/`,
  contextWindow: 128000,
  maxTokens: 32768,
  input: ['text', 'image'],
  thinkingFormat: 'qwen-chat-template',
});

const model = runtime.getModel('local-qwen-test', 'qwen-test');
assert(model, 'registered local model should resolve through ModelRuntime');
assert.equal(model.baseUrl, `http://127.0.0.1:${address.port}/v1`);
assert.equal(model.api, 'openai-completions');
assert.equal(model.contextWindow, 128000);
assert.equal(model.maxTokens, 32768);
assert.equal(model.reasoning, true);
assert.deepEqual(model.input, ['text', 'image']);
assert.equal(model.compat?.thinkingFormat, 'qwen-chat-template');

const completion = await runtime.complete(model, {
  messages: [{ role: 'user', content: 'ping', timestamp: Date.now() }],
});
assert.equal(contentText(completion.content), 'local pong');
assert.equal(requestPath, '/v1/chat/completions');
assert.equal(authorization, 'Bearer local-no-auth');
await new Promise<void>((resolve, reject) => {
  server.close((error) => error ? reject(error) : resolve());
});

await assert.rejects(async () => {
  registerOpenAICompatibleEndpoint(runtime, {
    provider: 'bad-local',
    modelId: 'bad-model',
    baseUrl: '   ',
    contextWindow: 128000,
    maxTokens: 32768,
    input: ['text'],
  });
}, /base URL/);

process.stdout.write('model endpoint registration: ok\n');
