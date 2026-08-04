import { randomUUID } from 'node:crypto';
import { defineTool } from '@earendil-works/pi-coding-agent';
import type { TSchema } from 'typebox';
import { Check, Errors, Value } from 'typebox/value';
import type { EnvClient } from './computerTools.js';
import {
  SEMANTIC_PROTOCOL_VERSION,
  assertZeroImageContent,
  validateSemanticRequest,
  validateSemanticResponse,
  type SemanticResponse,
} from './semantic/protocol.js';
import {
  normalizeModelArguments,
} from './semantic/argumentNormalization.js';
import { SEMANTIC_TOOL_DEFINITIONS } from './semantic/tools.js';

const REQUEST_TIMEOUT_MS = 330_000;

export type SemanticToolTransport = 'canonical' | 'anthropic';

const ANTHROPIC_TOOL_ALIASES = {
  'computer.query': 'computer_query',
  'computer.act': 'computer_act',
  'computer.verify': 'computer_verify',
  'computer.run': 'computer_run',
  task_complete: 'task_complete',
} as const;

type CanonicalSemanticToolName = keyof typeof ANTHROPIC_TOOL_ALIASES;
type AnthropicSemanticToolName = (typeof ANTHROPIC_TOOL_ALIASES)[CanonicalSemanticToolName];

type ComputerToolName = Exclude<CanonicalSemanticToolName, 'task_complete'>;

interface PreparedValidationFailure {
  readonly message: string;
}

const VALIDATION_CARRIER_PREFIX = '__ghost_semantic_invalid_request__';

const CANONICAL_BY_ANTHROPIC_ALIAS = Object.fromEntries(
  Object.entries(ANTHROPIC_TOOL_ALIASES).map(([canonical, alias]) => [alias, canonical]),
) as Record<AnthropicSemanticToolName, CanonicalSemanticToolName>;

/**
 * Anthropic's API rejects dots in custom tool names. Keep the semantic
 * protocol and registry canonical, and change only the provider-facing tool
 * spelling for Anthropic models (including those routed through OpenRouter).
 */
export function semanticToolTransport(
  provider: string, modelId: string,
): SemanticToolTransport {
  return provider === 'anthropic'
    || (provider === 'openrouter' && modelId.startsWith('anthropic/'))
    ? 'anthropic'
    : 'canonical';
}

export function transportSemanticToolName(
  name: CanonicalSemanticToolName, transport: SemanticToolTransport,
): CanonicalSemanticToolName | AnthropicSemanticToolName {
  return transport === 'anthropic' ? ANTHROPIC_TOOL_ALIASES[name] : name;
}

/** Normalize provider event names before logging, tracing, and lifecycle checks. */
export function canonicalSemanticToolName(name: string): string {
  return CANONICAL_BY_ANTHROPIC_ALIAS[name as AnthropicSemanticToolName] ?? name;
}

function validationCarrier(toolName: ComputerToolName, token: string): Record<string, unknown> {
  const marker = `${VALIDATION_CARRIER_PREFIX}${token}`;
  if (toolName === 'computer.query') return { resource: marker };
  if (toolName === 'computer.act') {
    return { target: { ref: marker }, action: marker };
  }
  if (toolName === 'computer.verify') {
    return {
      mode: 'all',
      freshness: 'live',
      assertions: [{
        claim_id: marker,
        query: {
          resource: marker,
          scope: {},
          order_by: [],
          parameters: {},
          freshness: 'live',
        },
        assert: { op: 'exists' },
      }],
    };
  }
  return { code: marker };
}

function validationCarrierToken(
  toolName: ComputerToolName, payload: Record<string, unknown>,
): string | null {
  let marker: unknown;
  if (toolName === 'computer.query') marker = payload.resource;
  else if (toolName === 'computer.act') marker = payload.action;
  else if (toolName === 'computer.run') marker = payload.code;
  else {
    const assertion = Array.isArray(payload.assertions) ? payload.assertions[0] : null;
    marker = assertion && typeof assertion === 'object' && !Array.isArray(assertion)
      ? (assertion as Record<string, unknown>).claim_id : null;
  }
  return typeof marker === 'string' && marker.startsWith(VALIDATION_CARRIER_PREFIX)
    ? marker.slice(VALIDATION_CARRIER_PREFIX.length) : null;
}

function prepareComputerArguments(
  toolName: ComputerToolName,
  args: unknown,
  schema: TSchema,
  failures: Map<string, PreparedValidationFailure>,
): unknown {
  const normalized = normalizeModelArguments(args, schema).value;
  let converted = normalized;
  try {
    converted = structuredClone(normalized);
    Value.Convert(schema, converted);
  } catch {
    // Provider arguments are JSON in production. Keep this fail-closed for
    // tests or future transports that hand us a non-cloneable value.
  }
  if (Check(schema, converted)) return converted;

  const errors = [...Errors(schema, converted)].slice(0, 12).map((error) => {
    const path = error.instancePath || '/';
    return `${path}: ${error.message}`;
  });
  const token = randomUUID();
  failures.set(token, {
    message: `Invalid ${toolName} arguments${errors.length ? `: ${errors.join('; ')}` : ''}`,
  });
  return validationCarrier(toolName, token);
}

function validationFailureResponse(
  requestId: string, failure: PreparedValidationFailure,
): SemanticResponse {
  return validateSemanticResponse({
    protocol_version: SEMANTIC_PROTOCOL_VERSION,
    request_id: requestId,
    status: 'rejected',
    adapter_id: 'semantic.tool-validation@1',
    observed_at: new Date().toISOString(),
    before_revision: null,
    after_revision: null,
    result: null,
    provenance: [],
    error: {
      code: 'invalid_request',
      message: failure.message.slice(0, 8_192),
      retryable: true,
      side_effect_state: 'none',
      missing_capability: null,
      candidates: [],
      recovery: {
        allowed_operations: ['computer.query'],
        suggested_resource: 'system.capability',
      },
    },
  });
}

function normalizeModelPayload(
  toolName: string, params: Record<string, unknown>,
): Record<string, unknown> {
  if (toolName === 'computer.query') {
    const rawFields = params.fields;
    const fields = Array.isArray(rawFields) && rawFields.length === 1 && rawFields[0] === '*'
      ? [] : rawFields;
    return {
      scope: {},
      order_by: [],
      parameters: {},
      freshness: 'live',
      ...params,
      ...(fields === undefined ? {} : { fields }),
    };
  }
  if (toolName === 'computer.act') {
    return {
      arguments: {},
      preconditions: [],
      postconditions: [],
      ...params,
    };
  }
  return params;
}

function cleanDetails(
  toolName: string, payload: Record<string, unknown>, response: SemanticResponse,
): Record<string, unknown> {
  const result = response.result && typeof response.result === 'object'
    ? response.result as Record<string, unknown> : {};
  const collectionHandles = new Set(
    (Array.isArray(result.records) ? result.records : [])
      .filter((record): record is Record<string, unknown> => (
        record !== null && typeof record === 'object' && !Array.isArray(record)
      ))
      .map((record) => record.collection_handle)
      .filter((handle): handle is string => typeof handle === 'string'),
  );
  const collectionHandle = collectionHandles.size === 1
    ? [...collectionHandles][0] : undefined;
  return {
    semantic: {
      kind: toolName.slice('computer.'.length),
      resource: payload.resource,
      scope: payload.scope,
      where: payload.where,
      parameters: payload.parameters,
      cursor: payload.cursor,
      adapter_id: response.adapter_id,
      before_revision: response.before_revision,
      after_revision: response.after_revision,
      observation_id: result.observation_id,
      receipt_id: result.receipt_id,
      verification_id: result.verification_id,
      overflow_handle: result.overflow_handle ?? result.data_handle,
      collection_handle: collectionHandle,
      // Compatibility for older context-governor consumers.
      data_handle: result.overflow_handle ?? result.data_handle,
      semantic_operations: result.operation_count ?? 1,
      status: response.status,
    },
  };
}

async function postJson(
  client: EnvClient, path: string, body: unknown,
): Promise<unknown> {
  const response = await fetch(`${client.baseUrl}/episodes/${client.episodeId}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  const text = await response.text();
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`semantic env server returned non-JSON HTTP ${response.status}`);
  }
  if (!response.ok) {
    throw new Error(`semantic env server HTTP ${response.status}: ${JSON.stringify(parsed).slice(0, 800)}`);
  }
  assertZeroImageContent(parsed);
  return parsed;
}

export function createSemanticRuntimeTools(
  client: EnvClient,
  onSemanticOperations: (count: number) => void = () => {},
  transport: SemanticToolTransport = 'canonical',
) {
  const preparedValidationFailures = new Map<string, PreparedValidationFailure>();
  const tools = SEMANTIC_TOOL_DEFINITIONS.map((definition) => defineTool({
    name: transportSemanticToolName(definition.name, transport),
    label: definition.name,
    description: definition.description,
    parameters: definition.inputSchema as TSchema as never,
    prepareArguments: (args) => {
      if (definition.name === 'task_complete') {
        return normalizeModelArguments(args, definition.inputSchema).value as never;
      }
      return prepareComputerArguments(
        definition.name, args, definition.inputSchema, preparedValidationFailures,
      ) as never;
    },
    execute: async (_toolCallId, params) => {
      const payload = normalizeModelPayload(
        definition.name, params as Record<string, unknown>,
      );
      if (definition.name === 'task_complete') {
        const result = await postJson(client, '/semantic/complete', payload) as {
          accepted?: boolean;
          terminal?: boolean;
          infeasible?: boolean;
          warnings?: string[];
          error?: unknown;
        };
        return {
          content: [{ type: 'text' as const, text: JSON.stringify(result) }],
          details: {
            semantic: { kind: 'complete', status: result.accepted ? 'ok' : 'rejected' },
            terminal: Boolean(result.accepted && result.terminal),
            infeasible: Boolean(result.infeasible),
          },
        };
      }
      const validationToken = validationCarrierToken(definition.name, payload);
      const validationFailure = validationToken
        ? preparedValidationFailures.get(validationToken) : undefined;
      if (validationToken && validationFailure) {
        preparedValidationFailures.delete(validationToken);
        const response = validationFailureResponse(randomUUID(), validationFailure);
        return {
          content: [{ type: 'text' as const, text: JSON.stringify(response) }],
          details: {
            semantic: {
              kind: definition.name.slice('computer.'.length),
              adapter_id: response.adapter_id,
              before_revision: null,
              after_revision: null,
              semantic_operations: 0,
              status: response.status,
              error_code: response.error?.code,
            },
          },
        };
      }
      const operation = definition.name.slice('computer.'.length) as
        'query' | 'act' | 'verify' | 'run';
      const request = validateSemanticRequest({
        protocol_version: SEMANTIC_PROTOCOL_VERSION,
        request_id: randomUUID(),
        episode_id: client.episodeId,
        operation,
        payload,
      });
      const response = validateSemanticResponse(
        await postJson(client, '/semantic', request),
      );
      const result = response.result && typeof response.result === 'object'
        ? response.result as Record<string, unknown> : {};
      const operations = Number(result.operation_count ?? 1);
      onSemanticOperations(Number.isFinite(operations) ? Math.max(1, operations) : 1);
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(response) }],
        details: cleanDetails(definition.name, payload, response),
      };
    },
  }));
  const names = tools.map((tool) => tool.name);
  const expected = SEMANTIC_TOOL_DEFINITIONS.map(
    (definition) => transportSemanticToolName(definition.name, transport),
  );
  if (JSON.stringify(names) !== JSON.stringify(expected)) {
    throw new Error(`semantic-v1 tool surface drift: ${JSON.stringify(names)}`);
  }
  return tools;
}
