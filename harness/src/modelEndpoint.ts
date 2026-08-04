import type { Model } from '@earendil-works/pi-ai';
import { createProvider } from '@earendil-works/pi-ai';
import { openAICompletionsApi } from '@earendil-works/pi-ai/api/openai-completions.lazy';
import type { ModelRuntime } from '@earendil-works/pi-coding-agent';

export type LocalThinkingFormat =
  | 'openai'
  | 'qwen'
  | 'qwen-chat-template'
  | 'chat-template'
  | 'string-thinking';

export interface OpenAICompatibleEndpoint {
  provider: string;
  modelId: string;
  baseUrl: string;
  apiKey?: string;
  contextWindow: number;
  maxTokens: number;
  input: Array<'text' | 'image'>;
  thinkingFormat?: LocalThinkingFormat;
}

/**
 * Add one OpenAI-compatible endpoint to the same ModelRuntime used by the
 * benchmark session. This keeps local and hosted runs on the identical agent
 * loop: only the model transport changes.
 */
export function registerOpenAICompatibleEndpoint(
  runtime: ModelRuntime,
  endpoint: OpenAICompatibleEndpoint,
): void {
  const baseUrl = endpoint.baseUrl.trim().replace(/\/$/, '');
  if (!baseUrl) throw new Error('model base URL must not be empty');
  if (!endpoint.provider.trim()) throw new Error('model provider must not be empty');
  if (!endpoint.modelId.trim()) throw new Error('model ID must not be empty');
  if (!Number.isSafeInteger(endpoint.contextWindow) || endpoint.contextWindow <= 0) {
    throw new Error('model context window must be a positive integer');
  }
  if (!Number.isSafeInteger(endpoint.maxTokens) || endpoint.maxTokens <= 0) {
    throw new Error('model max tokens must be a positive integer');
  }
  if (!endpoint.input.length) throw new Error('model input types must not be empty');

  const model: Model<'openai-completions'> = {
    id: endpoint.modelId,
    name: endpoint.modelId,
    api: 'openai-completions',
    provider: endpoint.provider,
    baseUrl,
    reasoning: Boolean(endpoint.thinkingFormat),
    input: endpoint.input,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: endpoint.contextWindow,
    maxTokens: endpoint.maxTokens,
    ...(endpoint.thinkingFormat ? {
      compat: {
        thinkingFormat: endpoint.thinkingFormat,
      },
    } : {}),
  };

  // pi-ai's OpenAI client requires a non-empty key even when a local server
  // ignores Authorization. A caller-supplied key wins; the inert placeholder
  // makes unauthenticated vLLM/Ollama-style endpoints work without special
  // transport code.
  const apiKey = endpoint.apiKey || 'local-no-auth';
  runtime.registerNativeProvider(createProvider({
    id: endpoint.provider,
    name: endpoint.provider,
    baseUrl,
    auth: {
      apiKey: {
        name: `${endpoint.provider} API key`,
        async resolve() {
          return { auth: { apiKey }, source: endpoint.apiKey ? 'runtime API key' : 'local endpoint' };
        },
      },
    },
    models: [model],
    api: openAICompletionsApi(),
  }));
}
