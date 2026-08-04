import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';

export const SEMANTIC_SIMPLE_PROMPT_VERSION = '1.4';
export const SEMANTIC_SIMPLE_PROMPT_SOURCE = 'harness/prompts/semantic-simple-v1.4.txt';

/** Exact, versioned provider-facing prompt source for semantic-simple-v1. */
export const SEMANTIC_SIMPLE_SYSTEM_PROMPT = readFileSync(
  new URL('../prompts/semantic-simple-v1.4.txt', import.meta.url),
  'utf8',
);

export const SEMANTIC_SIMPLE_SYSTEM_PROMPT_SHA256 = createHash('sha256')
  .update(SEMANTIC_SIMPLE_SYSTEM_PROMPT, 'utf8')
  .digest('hex');

/** Add only episode clock context; no variant or application guidance. */
export function buildSemanticSimpleSystemPrompt(dateLine: string): string {
  if (!dateLine.trim()) throw new Error('semantic-simple-v1 requires a date reference');
  return `${SEMANTIC_SIMPLE_SYSTEM_PROMPT}\n${dateLine}`;
}
