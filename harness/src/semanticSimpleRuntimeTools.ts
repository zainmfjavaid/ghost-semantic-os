import { defineTool } from '@earendil-works/pi-coding-agent';
import { Type } from 'typebox';
import type { EnvClient } from './computerTools.js';
import { assertZeroImageContent } from './semantic/protocol.js';

const REQUEST_TIMEOUT_MS = 120_000;

export const SEMANTIC_SIMPLE_TOOL_NAMES = [
  'read_computer',
  'computer_click',
  'computer_type',
] as const;

async function postSimple(
  client: EnvClient, path: string, payload: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const response = await fetch(`${client.baseUrl}/episodes/${client.episodeId}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  const text = await response.text();
  let parsed: unknown;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`simple computer returned non-JSON HTTP ${response.status}`);
  }
  if (!response.ok) {
    throw new Error(`simple computer HTTP ${response.status}: ${JSON.stringify(parsed).slice(0, 800)}`);
  }
  assertZeroImageContent(parsed);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('simple computer returned a non-object response');
  }
  return parsed as Record<string, unknown>;
}

function textResult(result: Record<string, unknown>, kind: string) {
  const error = result.error && typeof result.error === 'object'
    ? result.error as Record<string, unknown> : undefined;
  const action = result.action && typeof result.action === 'object'
    ? result.action as Record<string, unknown> : undefined;
  const actionError = action?.error && typeof action.error === 'object'
    ? action.error as Record<string, unknown> : undefined;
  if (result.ok === false) {
    throw new Error(
      `Computer ${kind} failed: ${String(error?.code ?? 'error')}: `
      + `${String(error?.message ?? 'unknown failure')}. `
      + `side_effect_state=${String(error?.side_effect_state ?? 'unknown')}. `
      + 'Read the computer again if the element or surface may have changed.',
    );
  }
  const text = String(result.text ?? 'Computer state unavailable.');
  return {
    content: [{ type: 'text' as const, text }],
    details: {
      simpleComputer: {
        kind,
        ok: result.ok !== false,
        activeSurface: result.active_surface,
        surfaceCount: result.surface_count,
        elementCount: result.element_count,
        returnedElements: result.returned_elements,
        nextCursor: result.next_cursor,
        errorCode: error?.code,
        errorSideEffectState: error?.side_effect_state,
        actionOutcome: action ? {
          status: action.status,
          executionPath: action.execution_path,
          errorCode: actionError?.code,
          sideEffectState: actionError?.side_effect_state,
        } : undefined,
      },
    },
  };
}

export function createSemanticSimpleRuntimeTools(client: EnvClient) {
  const read = defineTool({
    name: 'read_computer',
    label: 'Read computer',
    description:
      'Read the remote Ubuntu computer as compact text. It lists every current surface, then the '
      + 'active surface current scene. Surface IDs are letters such as A or B; elements '
      + 'are surface-qualified IDs such as A1 or B10. Use query to find text or controls on '
      + 'the active surface (including installed apps when Desktop is active), '
      + 'within to expand any current container, '
      + 'query="page text" for complete browser-page copy, and cursor only when the prior '
      + 'result explicitly provides one.',
    parameters: Type.Object({
      query: Type.Optional(Type.String({
        description: 'Optional text to find on the active surface', maxLength: 500,
      })),
      within: Type.Optional(Type.String({
        description: 'Optional current container ID such as A12',
        pattern: '^[A-Z]+[1-9][0-9]*$',
      })),
      cursor: Type.Optional(Type.String({
        description: 'Opaque continuation emitted by the immediately prior compatible read',
        maxLength: 256,
      })),
    }, { additionalProperties: false }),
    execute: async (_id, params) => textResult(
      await postSimple(client, '/simple/read', params as Record<string, unknown>),
      'read',
    ),
  });

  const click = defineTool({
    name: 'computer_click',
    label: 'Click computer element',
    description:
      'Activate one exact current semantic capability. Pass a bare surface letter such as B '
      + 'to switch to that application/window, or an element ID such as B10 to click that '
      + 'control even when B is not active; the harness activates B first when required. This is '
      + 'not a coordinate. Stale IDs are rejected and never retargeted. The result automatically '
      + 'includes the refreshed surface list and concise current scene.',
    parameters: Type.Object({
      element: Type.String({
        description: 'Current surface or element ID, e.g. B or B10',
        pattern: '^[A-Z]+(?:[1-9][0-9]*)?$',
      }),
    }, { additionalProperties: false }),
    execute: async (_id, params) => textResult(
      await postSimple(client, '/simple/click', params as Record<string, unknown>),
      'click',
    ),
  });

  const type = defineTool({
    name: 'computer_type',
    label: 'Type into computer element',
    description:
      'Type literal text into one exact current editable element such as A4 or C12. The '
      + 'element listing states whether its native behavior is replace, insert, or send. '
      + 'Tab/newline-separated text may be used for a spreadsheet range paste. The result '
      + 'automatically includes the refreshed surface list and concise current scene. A background surface is '
      + 'activated automatically before typing into its exact element.',
    parameters: Type.Object({
      element: Type.String({
        description: 'Current editable element ID, e.g. A4 or C12',
        pattern: '^[A-Z]+[1-9][0-9]*$',
      }),
      text: Type.String({ description: 'Literal text to type', maxLength: 65_536 }),
    }, { additionalProperties: false }),
    execute: async (_id, params) => textResult(
      await postSimple(client, '/simple/type', params as Record<string, unknown>),
      'type',
    ),
  });

  const tools = [read, click, type];
  const names = tools.map((tool) => tool.name);
  if (JSON.stringify(names) !== JSON.stringify(SEMANTIC_SIMPLE_TOOL_NAMES)) {
    throw new Error(`semantic-simple-v1 tool surface drift: ${JSON.stringify(names)}`);
  }
  return tools;
}
