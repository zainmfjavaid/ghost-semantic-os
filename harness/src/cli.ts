import 'dotenv/config';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { Command } from 'commander';
import {
  runOsworldEpisode, type EpisodeOutcome, type RuntimeName,
} from './runEpisode.js';
import type { LocalThinkingFormat } from './modelEndpoint.js';

const program = new Command()
  .name('semantic-os-harness')
  .description('Run OSWorld tasks through the Ghost text-only semantic computer harness')
  .requiredOption('--env-url <url>', 'OSWorld env server base URL')
  .requiredOption('--tasks-file <path>', 'JSON file listing task paths')
  .option('--task-base <path>', 'alternate base for relative task paths sent to a remote env server')
  .option('--provider <name>', 'pi-ai provider', 'anthropic')
  .option('--model <id>', 'model ID', 'claude-opus-4-8')
  .option('--model-base-url <url>', 'OpenAI-compatible model endpoint; registers --provider/--model locally')
  .option('--model-context-window <number>', 'context window for --model-base-url', '128000')
  .option('--model-max-tokens <number>', 'maximum output tokens for --model-base-url', '32768')
  .option('--model-input <types>', 'comma-separated text,image support for --model-base-url', 'text')
  .option('--model-thinking-format <format>', 'OpenAI-compatible thinking format; use none to disable', 'qwen-chat-template')
  .option('--thinking <level>', 'off | low | medium | high', 'medium')
  .option('--max-tool-calls <number>', 'per-episode tool-call budget', '60')
  .option(
    '--provider-turn-timeout-ms <number>',
    'hard timeout for one model-provider turn; no automatic retry',
    '300000',
  )
  .option('--concurrency <number>', 'episodes to run in parallel (one VM each)', '1')
  .option('--limit <number>', 'only run the first N tasks')
  .option('--variant <name>', 'harness variant label recorded in results', 'baseline')
  .option(
    '--runtime <name>',
    'runtime: vision-v15 | hybrid-v15 | semantic-v1 | semantic-plus-v1 | semantic-simple-v1 | semantic-visual-v1',
    'hybrid-v15',
  )
  .option('--som', 'use Set-of-Marks: annotated screenshots + element-index actions', false)
  .option('--semantic-desktop', 'plain screenshots + typed desktop accessibility actions', false)
  .option('--vision-only', 'screenshots + raw coordinate/keyboard actions only (baseline arm)', false)
  .option('--web', 'use CDP browser tools: live-DOM element lists and in-page actions', false)
  .option('--web-text-only', 'omit screenshots from web tool results (A/B arm)', false)
  .option('--web-first', 'drop raw coordinate/pyautogui tools in web mode (A/B arm)', false)
  .option('--browser-only', 'environment has no desktop: offer browser tools only', false)
  .option('--compact-web', 'cap element listings and skip re-sending after actions (A/B arm)', false)
  .option('--browser-prompt', 'system prompt matching the browser-only toolset (A/B arm)', false)
  .option('--code-first', 'prefer model-written DOM programs over indexed clicks (A/B arm)', false)
  .option('--budget-hints', 'warn the agent when tool calls are running out (A/B arm)', false)
  .option('--verify-gate', 'first task_complete returns current state and asks for evidence (A/B arm)', false)
  .option('--guidance <paths>', 'comma-separated guidance files appended to the system prompt')
  .option('--label <name>', 'run label for the output directory')
  .option('--output-root <path>', 'result directory', 'results');

program.parse();
const options = program.opts<{
  envUrl: string;
  tasksFile: string;
  taskBase?: string;
  provider: string;
  model: string;
  modelBaseUrl?: string;
  modelContextWindow: string;
  modelMaxTokens: string;
  modelInput: string;
  modelThinkingFormat: string;
  thinking: 'off' | 'low' | 'medium' | 'high';
  maxToolCalls: string;
  providerTurnTimeoutMs: string;
  concurrency: string;
  limit?: string;
  variant: string;
  runtime: string;
  som: boolean;
  semanticDesktop: boolean;
  visionOnly: boolean;
  web: boolean;
  webTextOnly: boolean;
  webFirst: boolean;
  verifyGate: boolean;
  browserOnly: boolean;
  compactWeb: boolean;
  browserPrompt: boolean;
  codeFirst: boolean;
  budgetHints: boolean;
  guidance?: string;
  label?: string;
  outputRoot: string;
}>();

const guidanceFiles = (options.guidance ?? '').split(',').map((f) => f.trim()).filter(Boolean);
const extraGuidance = (await Promise.all(
  guidanceFiles.map((f) => readFile(f, 'utf8')),
)).join('\n\n');

const tasksFile = path.resolve(options.tasksFile);
const taskBase = path.resolve(options.taskBase ?? path.dirname(tasksFile));
const listedTasks = JSON.parse(await readFile(tasksFile, 'utf8')) as string[];
// Pools created for this repo can use paths relative to the pool JSON. That
// makes the exact same pool portable between macOS and /home/... on GCP while
// preserving compatibility with the historical absolute-path pools.
const allTasks = listedTasks.map((task) =>
  path.isAbsolute(task) ? task : path.resolve(taskBase, task));
const tasks = options.limit ? allTasks.slice(0, Number.parseInt(options.limit, 10)) : allTasks;
const concurrency = Math.max(1, Number.parseInt(options.concurrency, 10));
const maxToolCalls = Number.parseInt(options.maxToolCalls, 10);
const providerTurnTimeoutMs = Number.parseInt(options.providerTurnTimeoutMs, 10);
if (!Number.isSafeInteger(providerTurnTimeoutMs) || providerTurnTimeoutMs <= 0) {
  throw new Error('--provider-turn-timeout-ms must be a positive integer');
}
const localThinkingFormats = new Set<LocalThinkingFormat>([
  'openai', 'qwen', 'qwen-chat-template', 'chat-template', 'string-thinking',
]);
const modelInput = options.modelInput.split(',').map((entry) => entry.trim()).filter(Boolean);
if (modelInput.some((entry) => entry !== 'text' && entry !== 'image') || !modelInput.length) {
  throw new Error('--model-input must be a non-empty comma-separated subset of text,image');
}
const validRuntimes = new Set<RuntimeName>([
  'vision-v15', 'hybrid-v15', 'semantic-v1', 'semantic-plus-v1',
  'semantic-simple-v1', 'semantic-visual-v1',
]);
if (!validRuntimes.has(options.runtime as RuntimeName)) {
  throw new Error(
    '--runtime must be vision-v15, hybrid-v15, semantic-v1, semantic-plus-v1, '
    + 'semantic-simple-v1, '
    + 'or semantic-visual-v1',
  );
}
const runtime = options.runtime as RuntimeName;
const strictSemantic = runtime === 'semantic-v1';
const semanticRuntime = strictSemantic
  || runtime === 'semantic-plus-v1'
  || runtime === 'semantic-simple-v1';
const effectiveVisionOnly = runtime === 'vision-v15';
if (runtime === 'vision-v15' && [
  options.som, options.semanticDesktop, options.web, options.webTextOnly, options.webFirst,
  options.verifyGate, options.browserOnly, options.compactWeb, options.browserPrompt,
  options.codeFirst, options.budgetHints,
].some(Boolean)) {
  throw new Error('vision-v15 rejects semantic, browser, verification, and budget-hint flags');
}
if (runtime === 'hybrid-v15' && options.visionOnly) {
  throw new Error('hybrid-v15 contradicts --vision-only; use --runtime vision-v15');
}
if (semanticRuntime && [
  options.som, options.semanticDesktop, options.visionOnly, options.web,
  options.webTextOnly, options.webFirst, options.verifyGate, options.browserOnly,
  options.compactWeb, options.browserPrompt, options.codeFirst, options.budgetHints,
].some(Boolean)) {
  throw new Error(`--runtime ${runtime} rejects all legacy v15 feature flags`);
}
const effectiveVerifyGate = options.verifyGate && !effectiveVisionOnly;
const effectiveBudgetHints = options.budgetHints && !effectiveVisionOnly;
if (semanticRuntime && (modelInput.length !== 1 || modelInput[0] !== 'text')) {
  throw new Error(`--runtime ${runtime} requires --model-input text`);
}
if (semanticRuntime && guidanceFiles.length) {
  throw new Error(`--runtime ${runtime} rejects external guidance files`);
}
if (runtime === 'semantic-visual-v1') {
  throw new Error('semantic-visual-v1 is reserved for the later sidecar stage and is not enabled');
}
const modelThinkingFormat = options.modelThinkingFormat === 'none'
  ? undefined : options.modelThinkingFormat as LocalThinkingFormat;
if (modelThinkingFormat && !localThinkingFormats.has(modelThinkingFormat)) {
  throw new Error(
    '--model-thinking-format must be none, openai, qwen, qwen-chat-template, '
    + 'chat-template, or string-thinking',
  );
}
const modelContextWindow = Number.parseInt(options.modelContextWindow, 10);
const modelMaxTokens = Number.parseInt(options.modelMaxTokens, 10);

const stamp = new Date().toISOString().replaceAll(/[:.]/g, '-');
const runId = `${stamp}_${options.label ?? `${options.provider}_${options.model.replaceAll('/', '-')}`}`;
const outputDirectory = path.resolve(options.outputRoot, runId);
await mkdir(outputDirectory, { recursive: true });

const results: EpisodeOutcome[] = [];
let started = 0;
let writeChain: Promise<void> = Promise.resolve();

function persist(): void {
  // Serialize writes so concurrent episodes cannot interleave a partial file.
  writeChain = writeChain.then(async () => {
    const solved = results.filter((r) => r.score > 0).length;
    await writeFile(
      path.join(outputDirectory, 'results.json'),
      `${JSON.stringify({
        runId,
        harness: 'pi @earendil-works/pi-coding-agent',
        harnessRevision: process.env.HARNESS_REVISION ?? 'unknown',
        parentCommit: process.env.PARENT_COMMIT ?? 'unknown',
        nestedOSWorldCommit: process.env.NESTED_OSWORLD_COMMIT ?? 'unknown',
        runtimeManifestSha256: process.env.RUNTIME_MANIFEST_SHA256 ?? 'unknown',
        runtimeFilesSha256: process.env.SEMANTIC_SERVER_RUNTIME_SHA256 ?? 'unknown',
        taskPoolSha256: process.env.TASK_POOL_SHA256 ?? 'unknown',
        benchmark: 'OSWorld',
        runtime,
        semanticProtocolVersion: semanticRuntime ? '1.0' : undefined,
        variant: options.variant,
        som: options.som,
        semanticDesktop: options.semanticDesktop,
        visionOnly: effectiveVisionOnly,
        web: options.web,
        webTextOnly: options.webTextOnly,
        webFirst: options.webFirst,
        verifyGate: effectiveVerifyGate,
        noDesktop: options.browserOnly,
        compactWeb: options.compactWeb,
        browserPrompt: options.browserPrompt,
        codeFirst: options.codeFirst,
        budgetHints: effectiveBudgetHints,
        guidance: guidanceFiles,
        tasksFile: options.tasksFile,
        provider: options.provider,
        model: options.model,
        modelBaseUrl: options.modelBaseUrl,
        thinkingLevel: options.thinking,
        maxToolCalls,
        providerTurnTimeoutMs,
        concurrency,
        completed: results.length,
        solved,
        successRate: results.length ? solved / results.length : 0,
        // Arm comparison needs cost per attempt, not just score: an arm that
        // matches baseline accuracy on half the tokens is a real win, and one
        // that buys a point by tripling spend is not.
        tokensTotal: results.reduce((a, r) => a + r.tokensTotal, 0),
        tokensPerEpisode: results.length
          ? Math.round(results.reduce((a, r) => a + r.tokensTotal, 0) / results.length) : 0,
        costUsd: Number(results.reduce((a, r) => a + r.costUsd, 0).toFixed(4)),
        imagePolicy: semanticRuntime ? {
          screenshotsCaptured: results.reduce(
            (sum, result) => sum + (result.semanticPolicy?.screenshotsCaptured ?? 0), 0,
          ),
          imagePartsCreated: results.reduce(
            (sum, result) => sum + (result.semanticPolicy?.imagePartsCreated ?? 0), 0,
          ),
          imagePartsInSession: results.reduce(
            (sum, result) => sum + (result.semanticPolicy?.imagePartsInSession ?? 0), 0,
          ),
          imagePartsSent: results.reduce(
            (sum, result) => sum + (result.semanticPolicy?.imagePartsSent ?? 0), 0,
          ),
          pixelsSentToPolicyModel: results.reduce(
            (sum, result) => sum + (result.semanticPolicy?.pixelsSentToPolicyModel ?? 0), 0,
          ),
          visualSidecarCalls: results.reduce(
            (sum, result) => sum + (result.semanticPolicy?.visualSidecarCalls ?? 0), 0,
          ),
        } : undefined,
        results,
      }, null, 2)}\n`,
      'utf8',
    );
  }).catch(() => undefined);
}

async function worker(workerId: number): Promise<void> {
  for (;;) {
    const index = started;
    if (index >= tasks.length) return;
    started += 1;
    const taskPath = tasks[index]!;
    const tag = `[w${workerId} ${index + 1}/${tasks.length}]`;
    process.stdout.write(`${tag} ${taskPath}\n`);
    try {
      let outcome: EpisodeOutcome;
      let infraRetries = 0;
      for (;;) {
        outcome = await runOsworldEpisode({
          baseUrl: options.envUrl,
          taskPath,
          provider: options.provider,
          modelId: options.model,
          runtime,
          thinkingLevel: options.thinking,
          maxToolCalls,
          providerTurnTimeoutMs,
          som: options.som,
          semanticDesktop: options.semanticDesktop,
          visionOnly: effectiveVisionOnly,
          web: options.web,
          webTextOnly: options.webTextOnly,
          webFirst: options.webFirst,
          verifyGate: effectiveVerifyGate,
          noDesktop: options.browserOnly,
          compactWeb: options.compactWeb,
          browserPrompt: options.browserPrompt,
          codeFirst: options.codeFirst,
          budgetHints: effectiveBudgetHints,
          ...(extraGuidance ? { extraGuidance } : {}),
          apiKeys: {
            anthropic: process.env.ANTHROPIC_API_KEY ?? '',
            openrouter: process.env.OPENROUTER_API_KEY ?? '',
          },
          ...(options.modelBaseUrl ? {
            modelEndpoint: {
              baseUrl: options.modelBaseUrl,
              ...(process.env.LOCAL_MODEL_API_KEY
                ? { apiKey: process.env.LOCAL_MODEL_API_KEY } : {}),
              contextWindow: modelContextWindow,
              maxTokens: modelMaxTokens,
              input: modelInput as Array<'text' | 'image'>,
              ...(modelThinkingFormat ? { thinkingFormat: modelThinkingFormat } : {}),
            },
          } : {}),
          onEvent: (line) => process.stdout.write(`${tag} ${line}\n`),
        });
        if (
          outcome.error?.startsWith('infrastructure tool timeout')
          && infraRetries < 1
        ) {
          infraRetries += 1;
          process.stdout.write(
            `${tag} infrastructure-invalid episode; retrying task once from a fresh snapshot\n`,
          );
          continue;
        }
        break;
      }
      if (infraRetries) outcome.infraRetries = infraRetries;
      results.push(outcome);
      process.stdout.write(
        `${tag} => score=${outcome.score} tools=${outcome.toolCalls} `
        + `attempts=${outcome.toolAttempts} `
        + `nudges=${outcome.nudges} ${outcome.stopReason} `
        + `tok=${outcome.tokensTotal} $${outcome.costUsd.toFixed(4)}\n`,
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      process.stdout.write(`${tag} => harness error: ${message}\n`);
      results.push({
        taskId: taskPath.split('/').at(-1)?.replace('.json', '') ?? taskPath,
        domain: taskPath.split('/').at(-2) ?? 'unknown',
        instruction: '',
        model: `${options.provider}/${options.model}`,
        runtime,
        score: 0,
        steps: 0,
        toolCalls: 0,
        toolAttempts: 0,
        elapsedMs: 0,
        stopReason: 'error',
        nudges: 0,
        tokensInput: 0,
        tokensOutput: 0,
        tokensTotal: 0,
        costUsd: 0,
        trace: [],
        error: message,
      });
    }
    persist();
  }
}

const startedAt = Date.now();
await Promise.all(
  Array.from({ length: Math.min(concurrency, tasks.length) }, (_, i) => worker(i + 1)),
);
await writeChain;

const solved = results.filter((r) => r.score > 0).length;
process.stdout.write(
  `\n${options.variant} | ${options.provider}/${options.model}: ${solved}/${results.length} `
  + `(${((solved / Math.max(1, results.length)) * 100).toFixed(1)}%) `
  + `in ${((Date.now() - startedAt) / 60000).toFixed(1)} min\n`,
);
process.stdout.write(`Results: ${outputDirectory}\n`);
