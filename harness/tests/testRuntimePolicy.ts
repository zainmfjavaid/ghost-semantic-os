import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import {
  createAgentSession,
  ModelRuntime,
  SessionManager,
} from '@earendil-works/pi-coding-agent';
import { registerOpenAICompatibleEndpoint } from '../src/modelEndpoint.js';
import {
  assertAuthoritativeSessionPrompt,
  authoritativeResourceLoader,
  budgetCheckpoint,
  createBenchmarkSettings,
  createProviderTurnDeadlineExtension,
  createProviderTurnDeadlineTelemetry,
  DEFAULT_PROVIDER_TURN_TIMEOUT_MS,
  executionBoundaryPreamble,
  isInfrastructureToolTimeout,
  isProviderTurnTimeout,
} from '../src/runEpisode.js';

assert.match(budgetCheckpoint(20, 40) ?? '', /Half the tool-call budget remains/);
assert.match(budgetCheckpoint(20, 40) ?? '', /smallest direct acceptance check/);
assert.match(budgetCheckpoint(20, 40) ?? '', /authoritative inspection/);
assert.match(budgetCheckpoint(30, 40) ?? '', /10 tool calls remain/);
assert.match(budgetCheckpoint(35, 40) ?? '', /5 tool calls remain/);
assert.match(budgetCheckpoint(38, 40) ?? '', /Only 2 tool calls remain/);
assert.match(budgetCheckpoint(38, 40) ?? '', /optional deeper tests can break a valid state/);
assert.equal(budgetCheckpoint(19, 40), undefined);
assert.equal(budgetCheckpoint(21, 40), undefined);

// Small budgets must not emit a halfway and ten-left checkpoint at the same
// position; the explicit ten-left policy remains deterministic.
assert.match(budgetCheckpoint(10, 20) ?? '', /10 tool calls remain/);

assert.equal(
  isInfrastructureToolTimeout('The operation was aborted due to timeout DETAILS: {}'),
  true,
);
assert.equal(
  isInfrastructureToolTimeout('env server request timed out', true),
  true,
);
assert.equal(
  isInfrastructureToolTimeout('Action error: selector timed out after 5000ms', true),
  false,
);

assert.equal(DEFAULT_PROVIDER_TURN_TIMEOUT_MS, 300_000);
assert.equal(isProviderTurnTimeout('Upstream idle timeout exceeded'), true);
assert.equal(isProviderTurnTimeout('Request timed out.'), true);
assert.equal(isProviderTurnTimeout('UND_ERR_BODY_TIMEOUT'), true);
assert.equal(isProviderTurnTimeout('tool execution timed out after 120 seconds'), false);
assert.equal(isProviderTurnTimeout(undefined), false);

const benchmarkSettings = createBenchmarkSettings(true, 12_345);
assert.deepEqual(benchmarkSettings.getRetrySettings(), {
  enabled: false,
  maxRetries: 0,
  baseDelayMs: 2000,
});
assert.deepEqual(benchmarkSettings.getProviderRetrySettings(), {
  timeoutMs: 12_345,
  maxRetries: 0,
  maxRetryDelayMs: 0,
});
assert.equal(benchmarkSettings.getHttpIdleTimeoutMs(), 12_345);
assert.equal(benchmarkSettings.getBlockImages(), true);
assert.equal(benchmarkSettings.getCompactionEnabled(), true);
assert.throws(() => createBenchmarkSettings(false, 0), /positive integer/);

// A transport-idle timeout does not help when an upstream keeps sending SSE
// bytes without ever finishing. Exercise the real Pi/OpenAI-compatible stream
// path and prove the wall-clock extension aborts exactly one request anyway.
let hangingRequests = 0;
const hangingServer = createServer((request, response) => {
  hangingRequests += 1;
  response.writeHead(200, {
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
  });
  let sequence = 0;
  const writeChunk = (): void => {
    response.write(`data: ${JSON.stringify({
      id: 'chatcmpl-never-finishes',
      object: 'chat.completion.chunk',
      created: 1,
      model: 'deadline-test',
      choices: [{
        index: 0,
        delta: sequence === 0
          ? { role: 'assistant', content: '.' }
          : { content: '.' },
        finish_reason: null,
      }],
    })}\n\n`);
    sequence += 1;
  };
  writeChunk();
  const interval = setInterval(writeChunk, 10);
  request.once('close', () => clearInterval(interval));
});
await new Promise<void>((resolve, reject) => {
  hangingServer.once('error', reject);
  hangingServer.listen(0, '127.0.0.1', resolve);
});
const hangingAddress = hangingServer.address();
assert(hangingAddress && typeof hangingAddress === 'object');
const deadlineRuntime = await ModelRuntime.create({ modelsPath: null });
registerOpenAICompatibleEndpoint(deadlineRuntime, {
  provider: 'deadline-provider',
  modelId: 'deadline-test',
  baseUrl: `http://127.0.0.1:${hangingAddress.port}/v1`,
  contextWindow: 8_192,
  maxTokens: 1_024,
  input: ['text'],
});
const deadlineModel = deadlineRuntime.getModel('deadline-provider', 'deadline-test');
assert(deadlineModel);
const deadlineTelemetry = createProviderTurnDeadlineTelemetry();
const deadlineLoader = await authoritativeResourceLoader('DEADLINE TEST', [
  createProviderTurnDeadlineExtension(120, deadlineTelemetry),
]);
const { session: deadlineSession } = await createAgentSession({
  cwd: '/home/user',
  model: deadlineModel,
  noTools: 'all',
  customTools: [],
  tools: [],
  resourceLoader: deadlineLoader,
  settingsManager: createBenchmarkSettings(false, 5_000),
  sessionManager: SessionManager.inMemory('/home/user'),
  modelRuntime: deadlineRuntime,
});
const deadlineStartedAt = Date.now();
await deadlineSession.prompt('keep streaming forever');
const deadlineElapsed = Date.now() - deadlineStartedAt;
assert.equal(deadlineTelemetry.timedOut, true);
assert.equal(deadlineTelemetry.timedOutTurn, 1);
assert.equal(hangingRequests, 1, 'timed-out provider turn must not be retried');
assert(deadlineElapsed >= 100 && deadlineElapsed < 1_500, `unexpected deadline ${deadlineElapsed}ms`);
deadlineSession.dispose();
hangingServer.closeAllConnections();
await new Promise<void>((resolve, reject) => {
  hangingServer.close((error) => error ? reject(error) : resolve());
});

const hybridBoundary = executionBoundaryPreamble(false);
assert.match(hybridBoundary, /remote\s+Ubuntu guest/);
assert.match(hybridBoundary, /\/home\/user/);
assert.match(hybridBoundary, /host filesystem and working directory[\s\S]*unavailable/);
assert.doesNotMatch(hybridBoundary, /osworld-pi|harness\/src/);
const browserBoundary = executionBoundaryPreamble(true);
assert.match(browserBoundary, /supplied browser tools/);
assert.doesNotMatch(browserBoundary, /\/home\/user/);
const visionBoundary = executionBoundaryPreamble(false, true);
assert.match(visionBoundary, /visual\s+mouse and keyboard tools/);
assert.doesNotMatch(visionBoundary, /Guest commands|install|\/home\/user/);
const semanticPlusBoundary = executionBoundaryPreamble(false, false, false, true);
assert.match(semanticPlusBoundary, /remote Ubuntu guest/);
assert.match(semanticPlusBoundary, /computer_exec\/computer_python/);
assert.match(semanticPlusBoundary, /pixels, screenshots, raw\s+input devices/);
assert.doesNotMatch(semanticPlusBoundary, /harness\/src|pyautogui/);
const loader = await authoritativeResourceLoader('AUTHORITATIVE REMOTE PROMPT');
assert.equal(loader.getSystemPrompt(), 'AUTHORITATIVE REMOTE PROMPT');
assert.deepEqual(loader.getAppendSystemPrompt(), []);
assert.deepEqual(loader.getAgentsFiles().agentsFiles, []);
assert.doesNotThrow(() => assertAuthoritativeSessionPrompt(
  `${hybridBoundary}\nCurrent working directory: /home/user`,
));
assert.throws(() => assertAuthoritativeSessionPrompt(
  `${hybridBoundary}\nCurrent working directory: ${process.cwd()}`,
), /leaked the harness host cwd/);
const modelRuntime = await ModelRuntime.create();
const smokeModel = modelRuntime.getModel('openrouter', 'qwen/qwen3.6-27b');
assert.ok(smokeModel);
const { session: promptSession } = await createAgentSession({
  cwd: '/home/user',
  model: smokeModel,
  noTools: 'all',
  customTools: [],
  tools: [],
  resourceLoader: loader,
  sessionManager: SessionManager.inMemory('/home/user'),
  modelRuntime,
});
assert.doesNotThrow(() => assertAuthoritativeSessionPrompt(promptSession.systemPrompt));
assert.doesNotMatch(promptSession.systemPrompt, new RegExp(process.cwd()));
promptSession.dispose();

console.log('PASS generalized budget checkpoints are deterministic');
console.log('PASS only fatal environment transport timeouts trigger infra retry');
console.log('PASS provider turns are bounded once with typed timeout classification');
console.log('PASS authoritative execution boundary hides the harness host cwd');
console.log('PASS vision-only boundary exposes no shell or filesystem fiction');
console.log('PASS resource loader replaces rather than appends to Pi coding context');
console.log('PASS fully assembled Pi prompt is pinned to the guest cwd');
