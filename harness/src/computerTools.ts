import { defineTool } from '@earendil-works/pi-coding-agent';
import { Type } from 'typebox';

/**
 * OSWorld computer tools for a pi agent session.
 *
 * Visible UI tools bottom out in OSWorld's native `pyautogui` action space via
 * the env server. A separate bounded computer_exec lane handles legitimate
 * guest filesystem/CLI work without becoming an invisible GUI automation path.
 * Observations come back as screenshots, which pi threads into the conversation
 * as image content.
 */

export interface EnvClient {
  episodeId: string;
  baseUrl: string;
}

interface StepResponse {
  screenshot?: string;
  media_type?: string;
  screenshot_bytes?: number;
  screenshot_unavailable?: boolean;
  elements?: string;
  element_count?: number;
  elements_unavailable?: boolean;
  desktop_accessibility_ready?: boolean;
  desktop_surfaces?: Array<{
    role?: string;
    name?: string;
    states?: string[];
    context?: string[];
  }>;
  candidate_count?: number;
  semantic_snapshot?: number;
  screen_unchanged?: boolean;
  unchanged_streak?: number;
  repeated_action?: number;
  blind_action_streak?: number;
  readonly_js_streak?: number;
  acted_on?: { index: number; x: number; y: number; execution?: string };
  web_elements?: string;
  web_elements_note?: string;
  web_no_change?: number;
  web_element_count?: number;
  page_text?: string;
  result?: string;
  done?: boolean;
  steps?: number;
  errors?: string[];
}

const DEFAULT_MEDIA_TYPE = 'image/jpeg';
// Long but bounded guest installs/builds may legitimately use the five-minute
// computer_exec ceiling. Keep the HTTP envelope slightly wider so one useful
// operation does not become a chain of budget-wasting background-process polls.
const ENV_REQUEST_TIMEOUT_MS = 330_000;

async function post(
  client: EnvClient,
  path: string,
  body: unknown,
): Promise<StepResponse> {
  const response = await fetch(`${client.baseUrl}/episodes/${client.episodeId}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(ENV_REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new Error(`env server ${response.status}: ${(await response.text()).slice(0, 400)}`);
  }
  return await response.json() as StepResponse;
}

async function get(client: EnvClient, path: string): Promise<StepResponse> {
  const response = await fetch(`${client.baseUrl}/episodes/${client.episodeId}${path}`, {
    signal: AbortSignal.timeout(ENV_REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) {
    throw new Error(`env server ${response.status}: ${(await response.text()).slice(0, 400)}`);
  }
  return await response.json() as StepResponse;
}

type ToolContent =
  | { type: 'text'; text: string }
  | { type: 'image'; data: string; mimeType: string };

/** Per-episode out-of-band notice slot, drained into the next tool result.
 *  Injecting through the tool result rather than a mid-turn user message keeps
 *  it strictly in-band: no risk of interleaving an extra turn into the loop. */
export type Notices = { pending?: string | undefined };

function screenshotResult(
  result: StepResponse,
  note?: string,
  omitImage = false,
  notices?: Notices,
): { content: ToolContent[]; details: Record<string, unknown> } {
  const content: ToolContent[] = [];
  const lines: string[] = [];
  if (note) lines.push(note);
  if (notices?.pending) { lines.push(notices.pending); notices.pending = undefined; }
  if (result.errors?.length) lines.push(`Action error: ${result.errors.join('; ')}`);
  if (result.done) lines.push('The environment reports the episode is finished.');
  if (result.screen_unchanged) {
    const streak = result.unchanged_streak ?? 1;
    lines.push(
      streak >= 2
        ? `The generic image-delta detector found no material visible-frame change for `
          + `${streak} actions. This is evidence, not proof of failure: inspect current `
          + 'state before retrying or changing strategy.'
        : 'The generic image-delta detector found no material visible-frame change. '
          + 'Small, off-screen, focus-only, download, or native-state changes may not '
          + 'appear here; inspect state rather than assuming success or failure.',
    );
  }
  if (result.web_no_change) {
    // Generic DOM-level signal: the interactive-element signature did not
    // change. Useful for detecting stalls, but not proof of failure: focus,
    // downloads, native UI and content outside the signature can still change.
    const n = result.web_no_change;
    lines.push(
      n >= 3
        ? `The page's interactive-element signature was unchanged for ${n} actions. `
          + 'That suggests a stalled plan, but does not prove failure when an action '
          + 'affects focus, a download, content outside the signature, or native UI.'
        : n === 2
          ? 'The page interactive-element signature was unchanged twice. Inspect page '
            + 'and desktop state before deciding whether the action failed.'
          : 'The page interactive-element signature is unchanged. This is a generic '
            + 'diagnostic signal, not proof that the action had no effect.',
    );
  }
  if (result.repeated_action) {
    lines.push(
      `You issued the exact same serialized action ${result.repeated_action + 1} times `
      + 'in a row. Unless this is an intentional retry after a state change or wait, '
      + 'inspect state before repeating it again.',
    );
  }
  if (result.blind_action_streak) {
    lines.push(
      `${result.blind_action_streak} consecutive keyboard/text actions have occurred `
      + 'without a named semantic target. Re-ground from the current screenshot and '
      + 'accessibility state before continuing; if the action error says the latest '
      + 'action was withheld, it did not execute.',
    );
  }
  if (result.readonly_js_streak) {
    lines.push(
      `${result.readonly_js_streak} consecutive read-only browser programs have run `
      + 'without an action. This is diagnostic evidence only; act when the needed '
      + 'state is known, or keep inspecting if it is not.',
    );
  }
  if (result.result) lines.push(result.result);
  if (result.page_text) {
    lines.push(`Page text:\n${result.page_text.slice(0, 3500)}`);
  }
  if (result.web_elements_note) {
    // Compact mode returns a short note instead of the refreshed table.
    lines.push(result.web_elements_note);
  }
  if (result.web_elements) {
    lines.push(
      'Interactive elements on the page (index / tag / role / state / label). These come from '
      + `the live DOM and are exact:\n${result.web_elements}`,
    );
  }
  if (result.elements) {
    lines.push(
      `Interactive elements currently on screen (index / role / name / text). The `
      + `screenshot is annotated with these numbers:\n${result.elements}`,
    );
  }
  if (result.desktop_accessibility_ready) {
    lines.push(
      'Desktop accessibility snapshot refreshed. Chrome/native controls can now be '
      + 'targeted by visible label with desktop_find, desktop_click, desktop_hover, '
      + 'or desktop_type.',
    );
  }
  if (result.desktop_surfaces?.length) {
    lines.push(
      'Current desktop surfaces (application/window/dialog/document identity):\n'
      + result.desktop_surfaces.map((surface) => JSON.stringify({
        role: surface.role ?? '',
        name: surface.name ?? '',
        states: surface.states ?? [],
        context: surface.context ?? [],
      })).join('\n'),
    );
  }
  if (result.elements_unavailable) {
    lines.push(
      'Element annotations are unavailable for this frame; act by coordinates or '
      + 'take another screenshot.',
    );
  }
  if (result.screenshot_unavailable) {
    lines.push(
      'The screen capture for this step failed, so no image is attached. The action '
      + 'itself still ran. Call screenshot again to see the current state.',
    );
  }
  if (lines.length > 0) content.push({ type: 'text', text: lines.join('\n') });
  if (result.screenshot && !omitImage) {
    content.push({
      type: 'image',
      data: result.screenshot,
      mimeType: result.media_type ?? DEFAULT_MEDIA_TYPE,
    });
  }
  if (content.length === 0) content.push({ type: 'text', text: 'OK' });
  return { content, details: { steps: result.steps ?? null, done: result.done ?? false } };
}

/** Escape a JS string for embedding in a single-quoted Python literal. */
function pythonString(value: string): string {
  return `'${value.replaceAll('\\', '\\\\').replaceAll("'", "\\'").replaceAll('\n', '\\n')}'`;
}

function keyCommand(keys: string): { command?: string; invalid: string[] } {
  const aliases: Record<string, string> = {
    control: 'ctrl', return: 'enter', escape: 'esc', del: 'delete',
    pgup: 'pageup', pgdn: 'pagedown',
  };
  const allowed = /^(?:[a-z0-9]|f(?:[1-9]|1[0-9]|2[0-4])|ctrl|alt|shift|enter|tab|esc|space|backspace|delete|up|down|left|right|home|end|pageup|pagedown|insert|win|super|menu)$/;
  const sequence = keys.trim().split(/\s+/).filter(Boolean).map((step) =>
    step.split('+').map((part) => aliases[part.toLowerCase()] ?? part.toLowerCase())
      .filter(Boolean));
  const invalid = sequence.flat().filter((part) => !allowed.test(part));
  if (!sequence.length || invalid.length) return { invalid };
  const steps = sequence.map((parts) => parts.length > 1
    ? `pyautogui.hotkey(${parts.map(pythonString).join(', ')})`
    : `pyautogui.press(${pythonString(parts[0] ?? '')})`);
  return {
    invalid,
    command: 'import pyautogui, time; '
      + steps.join('; time.sleep(0.12); '),
  };
}

/** Element-indexed tools: the agent picks a number off the annotated screenshot
 * and the server resolves it to real coordinates. Removes pixel grounding,
 * which is the dominant failure mode for smaller models. */
export function createSomTools(client: EnvClient, somVerifyGate = false) {
  const elementAction = (
    name: string,
    label: string,
    description: string,
    action: string,
    withText: boolean,
  ) => defineTool({
    name,
    label,
    description,
    parameters: withText
      ? Type.Object({
        index: Type.Integer({ description: 'Element number from the annotated screenshot' }),
        text: Type.String({ description: 'Text to type into the element' }),
      })
      : Type.Object({
        index: Type.Integer({ description: 'Element number from the annotated screenshot' }),
      }),
    execute: async (_id, params) => {
      const body: Record<string, unknown> = { index: params.index, action };
      if (withText) body.text = (params as unknown as { text: string }).text;
      return screenshotResult(await post(client, '/element', body));
    },
  });

  const clickElement = elementAction(
    'click_element', 'Click element',
    'Click the numbered element shown on the annotated screenshot. Prefer this over '
    + 'raw coordinates: the numbers come from the accessibility tree and are exact.',
    'click', false,
  );
  const doubleClickElement = elementAction(
    'double_click_element', 'Double-click element',
    'Double-click a numbered element (open a file, select a word).', 'double_click', false,
  );
  const rightClickElement = elementAction(
    'right_click_element', 'Right-click element',
    'Right-click a numbered element to open its context menu.', 'right_click', false,
  );
  const typeIntoElement = elementAction(
    'type_into_element', 'Type into element',
    'Click a numbered field, clear it, and type text into it. Use this for search '
    + 'boxes, address bars, and form fields.',
    'type', true,
  );
  const findDesktop = defineTool({
    name: 'desktop_find',
    label: 'Find desktop element',
    description:
      'Search the complete live operating-system accessibility snapshot. Returns role, '
      + 'name, value, state, advertised actions, parent context and a snapshot-scoped ref. '
      + 'Call with no filters '
      + 'to list the current surfaces and actionable controls; use filters to narrow or '
      + 'disambiguate controls that share a label. When unlabeled/duplicate controls remain, '
      + 'pass the exact current ref to desktop_click/hover/type.',
    parameters: Type.Object({
      query: Type.Optional(Type.String({ description: 'Visible name, text or value' })),
      role: Type.Optional(Type.String({ description: 'Accessibility role filter' })),
      state: Type.Optional(Type.String({ description: 'Required state, such as expanded' })),
      context: Type.Optional(Type.String({
        description: 'Owning application, window, dialog or parent-context filter',
      })),
    }),
    execute: async (_id, params) =>
      screenshotResult(await post(client, '/element/find', params), undefined, true),
  });
  const clickDesktop = defineTool({
    name: 'desktop_click',
    label: 'Click named desktop control',
    description:
      'Click one unique live desktop control resolved by semantic fields. Native '
      + 'accessibility invocation is preferred when advertised, with current bounds '
      + 'as the real-input fallback.',
    parameters: Type.Object({
      ref: Type.Optional(Type.String({
        description: 'Exact snapshot-scoped ref returned by the latest desktop_find',
      })),
      query: Type.Optional(Type.String({ description: 'Exact visible name, text or value' })),
      role: Type.Optional(Type.String({ description: 'Accessibility role filter' })),
      state: Type.Optional(Type.String({ description: 'Required current state' })),
      context: Type.Optional(Type.String({
        description: 'Owning application, window, dialog or parent-context filter',
      })),
    }),
    execute: async (_id, params) =>
      screenshotResult(await post(client, '/element/match', {
        ref: params.ref, query: params.query ?? '', role: params.role, state: params.state,
        context: params.context, action: 'click',
      })),
  });
  const hoverDesktop = defineTool({
    name: 'desktop_hover',
    label: 'Hover over named desktop control',
    description:
      'Move the pointer over one unique live desktop control resolved by semantic fields.',
    parameters: Type.Object({
      ref: Type.Optional(Type.String({
        description: 'Exact snapshot-scoped ref returned by the latest desktop_find',
      })),
      query: Type.Optional(Type.String({ description: 'Exact visible name, text or value' })),
      role: Type.Optional(Type.String({ description: 'Accessibility role filter' })),
      state: Type.Optional(Type.String({ description: 'Required current state' })),
      context: Type.Optional(Type.String({
        description: 'Owning application, window, dialog or parent-context filter',
      })),
    }),
    execute: async (_id, params) =>
      screenshotResult(await post(client, '/element/match', {
        ref: params.ref, query: params.query ?? '', role: params.role, state: params.state,
        context: params.context, action: 'hover',
      })),
  });
  const typeDesktop = defineTool({
    name: 'desktop_type',
    label: 'Type into named desktop control',
    description:
      'Focus one unique live desktop field, replace its value and type literal text.',
    parameters: Type.Object({
      ref: Type.Optional(Type.String({
        description: 'Exact snapshot-scoped ref returned by the latest desktop_find',
      })),
      query: Type.Optional(Type.String({ description: 'Exact visible name, text or value' })),
      role: Type.Optional(Type.String({ description: 'Accessibility role filter' })),
      state: Type.Optional(Type.String({ description: 'Required current state' })),
      context: Type.Optional(Type.String({
        description: 'Owning application, window, dialog or parent-context filter',
      })),
      text: Type.String({ description: 'Literal text to enter' }),
    }),
    execute: async (_id, params) =>
      screenshotResult(await post(client, '/element/match', {
        ref: params.ref, query: params.query ?? '', role: params.role, state: params.state,
        context: params.context, action: 'type', text: params.text,
      })),
  });
  const desktopActions = defineTool({
    name: 'desktop_actions',
    label: 'Run ordered desktop actions',
    description:
      'Execute up to 12 desktop UI actions in order. Each named control is '
      + 're-resolved from the fresh accessibility snapshot after the previous action. '
      + 'Use wait_for as a semantic pre/postcondition; the sequence stops on the first '
      + 'missing, stale, ambiguous or timed-out target.',
    parameters: Type.Object({
      actions: Type.Array(Type.Union([
        Type.Object({
          op: Type.Literal('click'),
          ref: Type.Optional(Type.String({ description: 'Current desktop_find ref' })),
          query: Type.Optional(Type.String({ description: 'Visible name, text or value' })),
          role: Type.Optional(Type.String({ description: 'Accessibility role filter' })),
          state: Type.Optional(Type.String({ description: 'Required current state' })),
          context: Type.Optional(Type.String({ description: 'Owning/parent context filter' })),
        }),
        Type.Object({
          op: Type.Literal('hover'),
          ref: Type.Optional(Type.String({ description: 'Current desktop_find ref' })),
          query: Type.Optional(Type.String({ description: 'Visible name, text or value' })),
          role: Type.Optional(Type.String({ description: 'Accessibility role filter' })),
          state: Type.Optional(Type.String({ description: 'Required current state' })),
          context: Type.Optional(Type.String({ description: 'Owning/parent context filter' })),
        }),
        Type.Object({
          op: Type.Literal('type'),
          ref: Type.Optional(Type.String({ description: 'Current desktop_find ref' })),
          query: Type.Optional(Type.String({ description: 'Visible name, text or value' })),
          role: Type.Optional(Type.String({ description: 'Accessibility role filter' })),
          state: Type.Optional(Type.String({ description: 'Required current state' })),
          context: Type.Optional(Type.String({ description: 'Owning/parent context filter' })),
          text: Type.String({ description: 'Literal replacement field value' }),
        }),
        Type.Object({
          op: Type.Literal('key'),
          keys: Type.String({
            description: 'Real key/chord sequence, e.g. ctrl+l',
          }),
        }),
        Type.Object({
          op: Type.Literal('text'),
          text: Type.String({
            description: 'Literal text typed into the currently focused control',
          }),
        }),
        Type.Object({
          op: Type.Literal('wait'),
          seconds: Type.Optional(Type.Number({
            description: 'Wait duration, 0.5 to 5 seconds',
          })),
        }),
        Type.Object({
          op: Type.Literal('wait_for'),
          query: Type.Optional(Type.String({ description: 'Visible name, text or value' })),
          role: Type.Optional(Type.String({ description: 'Accessibility role filter' })),
          state: Type.Optional(Type.String({ description: 'Required current state' })),
          context: Type.Optional(Type.String({ description: 'Owning/parent context filter' })),
          condition: Type.Optional(Type.Union([
            Type.Literal('present'), Type.Literal('absent'),
          ])),
          seconds: Type.Optional(Type.Number({
            description: 'Maximum wait, 0.5 to 10 seconds',
          })),
        }),
        Type.Object({
          op: Type.Literal('scroll'),
          direction: Type.Union([Type.Literal('up'), Type.Literal('down')]),
          amount: Type.Optional(Type.Integer({
            description: 'Scroll notches, 1 to 10',
          })),
        }),
      ]), { minItems: 1, maxItems: 12 }),
    }),
    execute: async (_id, params) => {
      let latest: StepResponse = {};
      const completed: string[] = [];
      for (const [index, action] of params.actions.entries()) {
        if (action.op === 'click' || action.op === 'hover' || action.op === 'type') {
          latest = await post(client, '/element/match', {
            ref: action.ref, query: action.query ?? '', role: action.role, state: action.state,
            context: action.context,
            action: action.op === 'hover' ? 'hover' : action.op,
            ...(action.op === 'type' ? { text: action.text ?? '' } : {}),
          });
        } else if (action.op === 'wait_for') {
          const timeoutMs = Math.min(10, Math.max(0.5, action.seconds ?? 5)) * 1000;
          const deadline = Date.now() + timeoutMs;
          const condition = action.condition ?? 'present';
          let satisfied = false;
          do {
            latest = await post(client, '/element/find', {
              query: action.query ?? '', role: action.role, state: action.state,
              context: action.context,
            });
            const present = (latest.candidate_count ?? 0) > 0;
            satisfied = condition === 'present' ? present : !present;
            if (satisfied) break;
            await new Promise((resolve) => setTimeout(resolve, 500));
          } while (Date.now() < deadline);
          if (!satisfied) {
            latest.errors = [
              `desktop_actions step ${index}: semantic wait timed out (${condition})`,
            ];
          } else {
            delete latest.errors;
          }
        } else if (action.op === 'scroll') {
          const amount = Math.min(10, Math.max(1, action.amount ?? 4));
          const signed = action.direction === 'up' ? amount : -amount;
          latest = await post(client, '/step', {
            command: `import pyautogui; pyautogui.scroll(${signed})`,
          });
        } else if (action.op === 'key') {
          const built = keyCommand(action.keys ?? '');
          if (!built.command) {
            latest = {
              errors: [
                `desktop_actions step ${index}: invalid key sequence`
                + `${built.invalid.length ? ` (${built.invalid.join(', ')})` : ''}`,
              ],
            };
            break;
          }
          latest = await post(client, '/step', { command: built.command });
        } else if (action.op === 'text') {
          latest = await post(client, '/step', {
            command: 'import pyautogui; '
              + `pyautogui.typewrite(${pythonString(action.text)}, interval=0.02)`,
          });
        } else {
          const seconds = Math.min(5, Math.max(0.5, action.seconds ?? 1));
          latest = await post(client, '/step', { command: 'WAIT', pause: seconds });
        }
        if (latest.errors?.length) break;
        completed.push(
          `${index}:${action.op}${'query' in action ? `(${action.query})` : ''}`,
        );
      }
      const prior = latest.result ? `${latest.result}\n` : '';
      latest.result = `${prior}desktop_actions completed ${completed.length}/`
        + `${params.actions.length}: ${completed.join(' -> ')}`;
      return screenshotResult(latest);
    },
  });

  // Coordinate/keyboard tools that have no element equivalent are shared.
  const shared = createComputerTools(client, somVerifyGate);
  const keep = new Set([
    'screenshot', 'key', 'scroll', 'computer_exec', 'computer_python', 'wait', 'python',
    'task_complete', 'drag',
  ]);
  return [
    findDesktop, clickDesktop, hoverDesktop, typeDesktop, desktopActions,
    clickElement, typeIntoElement, doubleClickElement, rightClickElement,
    ...shared.filter((tool) => keep.has(tool.name)),
  ];
}

/** Typed desktop graph without visual marks or raw GUI escape hatches.
 *
 * This is intentionally capability-based rather than app- or task-based: the
 * same tools operate Chrome chrome, LibreOffice, Files, Terminal and any other
 * app that advertises AT-SPI semantics. computer_exec covers non-UI computer
 * work, but invisible mouse/keyboard/browser automation is rejected server-side.
 */
export function createSemanticDesktopTools(
  client: EnvClient,
  verifyGate = false,
) {
  const keep = new Set([
    'desktop_find', 'desktop_click', 'desktop_hover', 'desktop_type',
    'desktop_actions', 'screenshot', 'key', 'scroll', 'computer_exec',
    'computer_python',
    'wait', 'task_complete',
  ]);
  return createSomTools(client, verifyGate)
    .filter((tool) => keep.has(tool.name));
}

/** Browser tools driven through Chrome DevTools Protocol.
 *
 * Element labels come from the live DOM, so unlike the AT-SPI tree they always
 * include page content. Clicks are dispatched in-page, which means they cannot
 * touch browser chrome (bookmarks bar, settings dialogs) — the desktop tools
 * remain available for that. */
/**
 * @param textOnly Omit the screenshot from web tool results. Every web action
 * currently attaches a full VM screenshot, so a twenty-call episode carries
 * twenty images — a large share of a local model's context, spent on a page the
 * element list and page text already describe. The agent keeps an explicit
 * `screenshot` tool for when it genuinely needs to look. Off by default and run
 * as an A/B arm, because "fewer images helps" is a hypothesis until measured.
 */
export function createWebTools(
  client: EnvClient, textOnly = false, webFirst = false, verifyGate = false,
  noDesktop = false, compact = false, som = false, notices?: Notices,
) {
  const web = (
    name: string, label: string, description: string,
    action: string, params: unknown,
  ) => defineTool({
    name, label, description,
    parameters: params as never,
    execute: async (_id, p) =>
      screenshotResult(
        await post(client, '/web', {
          action, compact, observe: !textOnly, ...(p as object),
        }),
        undefined, textOnly, notices,
      ),
  });

  const list = web(
    'web_elements', 'List page elements',
    'List every interactive element on the current web page with an index. Do this '
    + 'before clicking or typing, and again after the page changes — indices move.',
    'elements', Type.Object({}),
  );
  const click = web(
    'web_click', 'Click page element',
    'Click a numbered element from web_elements. Exact — resolved from the live DOM, '
    + 'so prefer this over clicking pixel coordinates on a web page.',
    'click', Type.Object({ index: Type.Integer({ description: 'Element index' }) }),
  );
  const typeInto = web(
    'web_type', 'Type into page element',
    'Click a numbered field, clear it, and type text into it.',
    'type', Type.Object({
      index: Type.Integer({ description: 'Element index' }),
      text: Type.String({ description: 'Text to type' }),
    }),
  );
  const navigate = web(
    'web_navigate', 'Go to URL',
    'Navigate directly to an absolute URL when the task or observed page establishes the '
    + 'destination. Preserve known locale/account context and prefer real links or controls '
    + 'over guessing alternate paths.',
    'navigate', Type.Object({ url: Type.String({ description: 'Absolute URL' }) }),
  );
  const readText = web(
    'web_read', 'Read page text',
    'Read the visible text of the page. Pass a query to search the WHOLE page and get '
    + 'the text around each match — long pages are truncated otherwise, and the value '
    + 'you need is often past the cut.',
    'text', Type.Object({
      query: Type.Optional(Type.String({
        description: 'Word or value to locate in the page text',
      })),
    }),
  );

  const searchWeb = web(
    'web_search', 'Search the public web',
    'Run up to 8 public web searches in one traced browser call and return the visible '
    + 'result titles, URLs and snippets. Use this for multi-record research instead of '
    + 'repeating navigate/read/tab-cleanup loops. It uses a temporary tab in the same '
    + 'Chrome context, closes it, and restores the original task tab.',
    'search', Type.Object({
      queries: Type.Array(Type.String({ description: 'A specific public-web query' }), {
        minItems: 1, maxItems: 8,
      }),
      result_limit: Type.Optional(Type.Integer({
        description: 'Results per query, default 5, maximum 8', minimum: 1, maximum: 8,
      })),
    }),
  );

  const readPages = web(
    'web_read_pages', 'Read public web pages',
    'Read the visible text from up to 8 public HTTP(S) pages in one traced browser call. '
    + 'Use URLs returned by web_search. Pages load in a temporary tab in the same Chrome '
    + 'context; local/private addresses are rejected, the tab is closed, and the original '
    + 'task tab is restored. This reads evidence only and does not change task-page state.',
    'read_pages', Type.Object({
      urls: Type.Array(Type.String({ description: 'Absolute public HTTP(S) URL' }), {
        minItems: 1, maxItems: 8,
      }),
      text_limit: Type.Optional(Type.Integer({
        description: 'Maximum visible-text characters per page, default 2500',
        minimum: 500, maximum: 5000,
      })),
    }),
  );

  const scrollPage = web(
    'web_scroll', 'Scroll the page',
    'Scroll the web page and get the refreshed element list back in one call. Note that '
    + 'you can usually click an element marked "below/above fold" directly — it is '
    + 'scrolled into view for you — so scroll mainly to read content, not to reach a control.',
    'scroll', Type.Object({
      direction: Type.Optional(Type.Union([Type.Literal('down'), Type.Literal('up')])),
      amount: Type.Optional(Type.Integer({ description: 'Wheel notches, default 3' })),
    }),
  );

  // Observed on real tasks: when the model struggles it INVENTS desktop tools
  // that were never offered (click/move/python with string coordinates). pi
  // answers with a generic unknown-tool error, the model reads a run of those
  // as "nothing in this environment works", and declares a perfectly feasible
  // task infeasible. These stubs catch the names it actually reaches for and
  // point it back at the browser equivalents. They carry no task information --
  // only a correction about which tools exist.
  const redirect = (name: string, hint: string) => defineTool({
    name,
    label: name,
    description: `Not available here. ${hint}`,
    parameters: Type.Object({}, { additionalProperties: true }),
    execute: async () => ({
      content: [{
        type: 'text' as const,
        text: `There is no "${name}" tool in this environment, so nothing happened. `
          + `${hint} The environment is working — only this tool does not exist.`,
      }],
      details: { redirected: true },
    }),
  });

  const redirects = noDesktop ? [
    redirect('click', 'Use web_click with an index from web_elements.'),
    redirect('move', 'There is no cursor. Use web_click with an element index.'),
    redirect('type_text', 'Use web_type with an element index.'),
    redirect('key', 'There is no keyboard. Navigate with web_navigate or web_click.'),
    redirect('python', 'There is no shell. Use the web_* tools.'),
    redirect('scroll', 'Use web_scroll, or just click an element marked below the fold.'),
  ] : [];

  const listTabs = web(
    'web_tabs', 'List tabs',
    'List the open browser tabs with their index, title and URL, and which is active. '
    + 'Clicking a link can open a new tab and move you there without warning, and some '
    + 'tasks are graded on which tabs are open.',
    'tabs', Type.Object({}),
  );

  const switchTab = web(
    'web_switch_tab', 'Switch tab',
    'Make a tab active by its index from web_tabs. Later actions apply to it.',
    'switch_tab', Type.Object({ index: Type.Integer() }),
  );

  const closeTab = web(
    'web_close_tab', 'Close tab',
    'Close a tab by its index from web_tabs.',
    'close_tab', Type.Object({ index: Type.Integer() }),
  );

  const listFrames = web(
    'web_frames', 'List page frames',
    'List the main document and embedded frames with a frame index, title and URL. '
    + 'Use this when a booking widget, payment form or other control is inside an iframe; '
    + 'then pass that frame index to web_js.',
    'frames', Type.Object({}),
  );

  const runJs = web(
    'web_js', 'Run JavaScript',
    'Execute JavaScript in the page and get the JSON result back. Use this to compute '
    + '(dates, arithmetic), to read or set form values directly, to query the DOM, and to '
    + 'extract structured data in one step instead of many clicks. Runs in the selected '
    + 'page frame, so document, window and site JS are available. A reliable helper is '
    + 'preinstalled: ghost.inspect(css?,limit?), ghost.find(text,css?), '
    + 'ghost.fill(css,value,index?), ghost.click(css,index?), ghost.select(css,value,index?), '
    + 'ghost.check(css,wanted,index?), ghost.submit(css?), ghost.value(css,index?), and '
    + 'ghost.wait(ms). Prefer these helpers for reactive forms. Always return a compact '
    + 'object describing what you found or changed. Set expect_change=true for scripts '
    + 'that click, fill, select, check, submit or mutate so the harness verifies the effect.',
    'js', Type.Object({
      code: Type.String({ description: 'JavaScript to run, e.g. an IIFE returning a value' }),
      frame: Type.Optional(Type.Integer({
        description: 'Frame index from web_frames; defaults to the main document (0)',
      })),
      expect_change: Type.Optional(Type.Boolean({
        description: 'True when this script is intended to change page state',
      })),
    }),
  );

  const runActions = web(
    'web_actions', 'Run ordered browser actions',
    'Execute up to 20 actions in order through Playwright: click, fill, select, check, '
    + 'press, or wait. Target controls semantically with by=role plus role/name, by=label, '
    + 'by=placeholder, by=text or by=testid whenever possible; use by=css plus selector '
    + 'for selectors from ghost.inspect/find. Semantic targets are re-resolved after each '
    + 'reactive re-render. Prefer this over ghost.click/fill for mutations: these are '
    + 'trusted browser actions, and one call can perform several causal steps. The program '
    + 'stops at the first failure and returns every completed step.',
    'actions', Type.Object({
      actions: Type.Array(Type.Object({
        op: Type.Union([
          Type.Literal('click'), Type.Literal('fill'), Type.Literal('select'),
          Type.Literal('check'), Type.Literal('press'), Type.Literal('wait'),
        ]),
        by: Type.Optional(Type.Union([
          Type.Literal('css'), Type.Literal('role'), Type.Literal('label'),
          Type.Literal('placeholder'), Type.Literal('text'), Type.Literal('testid'),
        ], {
          description: 'Target strategy; defaults to css when selector is present',
        })),
        selector: Type.Optional(Type.String({
          description: 'CSS selector when by=css',
        })),
        role: Type.Optional(Type.String({
          description: 'ARIA role when by=role, e.g. button, option, combobox',
        })),
        name: Type.Optional(Type.String({
          description: 'Target accessible name/label/text; distinct from the value to type',
        })),
        exact: Type.Optional(Type.Boolean({
          description: 'Require an exact semantic-name match; default false',
        })),
        index: Type.Optional(Type.Integer({
          description: 'Zero-based match when the target matches multiple elements',
        })),
        value: Type.Optional(Type.String({ description: 'fill/select value or option label' })),
        checked: Type.Optional(Type.Boolean({ description: 'desired state for check' })),
        key: Type.Optional(Type.String({ description: 'Playwright key, e.g. Enter or ArrowDown' })),
        ms: Type.Optional(Type.Integer({ description: 'milliseconds for wait, maximum 5000' })),
        after_ms: Type.Optional(Type.Integer({
          description: 'settle time after an action, default 250, maximum 1200',
        })),
      }), { minItems: 1, maxItems: 20 }),
      frame: Type.Optional(Type.Integer({
        description: 'Frame index from web_frames; defaults to main document (0)',
      })),
    }),
  );

  const uiActions = defineTool({
    name: 'ui_actions',
    label: 'Run ordered cross-surface actions',
    description:
      'Execute up to 12 causal actions that cross between webpage DOM and native '
      + 'browser/operating-system UI. Use this when one visible page action opens a '
      + 'native confirmation, picker, menu or dialog: the program can perform the web '
      + 'action, wait for it, then resolve the native control by its live accessibility '
      + 'fields. Every subaction uses the same guarded web/desktop primitives, stops on '
      + 'the first failure, and returns a fresh final screenshot. Prefer one verified '
      + 'cross-surface program over separate click/find/click model turns.',
    parameters: Type.Object({
      actions: Type.Array(Type.Union([
        Type.Object({
          surface: Type.Literal('web'),
          op: Type.Literal('navigate'),
          url: Type.String({ description: 'Absolute URL' }),
        }),
        Type.Object({
          surface: Type.Literal('web'),
          op: Type.Union([
            Type.Literal('click'), Type.Literal('fill'), Type.Literal('press'),
            Type.Literal('select'), Type.Literal('check'),
          ]),
          by: Type.Optional(Type.Union([
            Type.Literal('css'), Type.Literal('role'), Type.Literal('label'),
            Type.Literal('placeholder'), Type.Literal('text'), Type.Literal('testid'),
          ])),
          selector: Type.Optional(Type.String()),
          role: Type.Optional(Type.String()),
          name: Type.Optional(Type.String()),
          exact: Type.Optional(Type.Boolean()),
          index: Type.Optional(Type.Integer()),
          value: Type.Optional(Type.String()),
          checked: Type.Optional(Type.Boolean()),
          key: Type.Optional(Type.String()),
          after_ms: Type.Optional(Type.Integer({ minimum: 0, maximum: 1200 })),
        }),
        Type.Object({
          surface: Type.Literal('desktop'),
          op: Type.Union([
            Type.Literal('click'), Type.Literal('type'), Type.Literal('hover'),
          ]),
          ref: Type.Optional(Type.String({ description: 'Current desktop_find ref' })),
          query: Type.Optional(Type.String({ description: 'Visible name, text or value' })),
          role: Type.Optional(Type.String()),
          state: Type.Optional(Type.String()),
          context: Type.Optional(Type.String()),
          text: Type.Optional(Type.String()),
        }),
        Type.Object({
          surface: Type.Literal('wait'),
          op: Type.Literal('wait'),
          seconds: Type.Number({ minimum: 0.25, maximum: 5 }),
        }),
      ]), { minItems: 1, maxItems: 12 }),
    }),
    execute: async (_id, params) => {
      const program = params.actions as Array<Record<string, unknown> & {
        surface: 'web' | 'desktop' | 'wait'; op: string;
      }>;
      const completed: Array<Record<string, unknown>> = [];
      let latest: StepResponse = {};
      for (const [index, action] of program.entries()) {
        if (action.surface === 'web' && action.op === 'navigate') {
          latest = await post(client, '/web', {
            action: 'navigate', compact: true, observe: false, url: action.url,
          });
        } else if (action.surface === 'web') {
          const trustedAction = Object.fromEntries(
            Object.entries(action).filter(([key, value]) =>
              key !== 'surface' && value !== undefined),
          );
          latest = await post(client, '/web', {
            action: 'actions', compact: true, observe: false,
            actions: [trustedAction],
          });
        } else if (action.surface === 'desktop') {
          latest = await post(client, '/element/match', {
            ref: action.ref, query: action.query ?? '', role: action.role,
            state: action.state, context: action.context, action: action.op,
            ...(action.op === 'type' ? { text: action.text ?? '' } : {}),
          });
        } else {
          latest = await post(client, '/step', {
            command: 'WAIT', pause: action.seconds,
          });
        }
        completed.push({
          index, surface: action.surface, op: action.op,
          ok: !latest.errors?.length,
          ...(latest.result ? { result: latest.result.slice(0, 500) } : {}),
          ...(latest.errors?.length ? { errors: latest.errors } : {}),
        });
        if (latest.errors?.length) break;
      }
      const observed = await get(client, '/obs');
      if (latest.errors?.length) observed.errors = latest.errors;
      observed.result = `ui_actions completed ${completed.length}/${program.length}: `
        + JSON.stringify(completed);
      return screenshotResult(observed, undefined, textOnly, notices);
    },
  });

  const shared = createComputerTools(client, verifyGate, textOnly);
  // CDP can see page DOM, but it cannot see Chrome's toolbar, menus, bookmark
  // bubble, print preview, extension picker, or native OS dialogs. When the
  // episode requested Set-of-Marks, add only its semantic element actions to
  // the hybrid toolset. Do not re-add raw coordinate/python UI escape hatches:
  // those were the source of the original web-first thrashing. computer_exec
  // is separately bounded to non-UI filesystem/CLI work by the env server.
  const semanticDesktopNames = new Set([
    'desktop_find', 'desktop_click', 'desktop_hover', 'desktop_type',
    'desktop_actions',
  ]);
  const somActions = som
    ? createSomTools(client, verifyGate)
      .filter((tool) => semanticDesktopNames.has(tool.name))
    : [];
  // webFirst drops the raw desktop escape hatches. In stub runs the model spent
  // roughly half its budget on coordinate clicks and pyautogui even with working
  // web tools in front of it, so the question is whether removing the escape
  // hatch focuses it or just removes a route it genuinely needs. Page-level tasks
  // are ~85% of the browser split and need none of these; browser-level tasks
  // (settings, bookmarks) need all of them, which is why this is an arm and not
  // the default.
  // noDesktop is for environments with no desktop at all (the local-Chrome
  // runner). Leaving `key` in would be worse than removing it: every press
  // fails, and the model reads a string of failures as "nothing works here" and
  // declares the task infeasible. That is an environment artifact masquerading
  // as a judgement, and on the infeasible-by-design tasks it would score a
  // point for entirely the wrong reason.
  const keep = noDesktop
    ? new Set(['screenshot', 'wait', 'task_complete'])
    : webFirst
      ? new Set([
        'screenshot', 'key', 'type_text', 'scroll', 'computer_exec',
        'computer_python', 'wait',
        'task_complete',
      ])
      : new Set(['screenshot', 'key', 'wait', 'python', 'task_complete',
        'click', 'type_text', 'scroll', 'computer_exec', 'computer_python']);
  const findElement = web(
    'web_find', 'Find elements',
    'Search ALL interactive elements on the page by a word from the control you want '
    + '(its label, button text, or link text) and get back just the matches, with fresh '
    + 'indices for web_click and web_type. The plain element list is capped, so use this '
    + 'when the control you need is not in it.',
    'find', Type.Object({
      query: Type.String({ description: 'Text appearing on or in the control' }),
    }),
  );

  return [list, findElement, click, typeInto, navigate, readText, searchWeb, readPages,
    scrollPage,
    listFrames, runJs, runActions, ...(som && !noDesktop ? [uiActions] : []),
    listTabs, switchTab, closeTab,
    ...somActions, ...shared.filter((t) => keep.has(t.name)), ...redirects];
}

export function createComputerTools(
  client: EnvClient, verifyGate = false, textOnlyGate = false,
) {
  let gateTripped = false;
  const screenshot = defineTool({
    name: 'screenshot',
    label: 'Screenshot',
    description:
      'Capture the current screen. Use this to see the desktop before acting and to verify '
      + 'that your last action had the effect you expected.',
    parameters: Type.Object({}),
    execute: async () => screenshotResult(await get(client, '/obs')),
  });

  const click = defineTool({
    name: 'click',
    label: 'Click',
    description:
      'Click at absolute screen coordinates on the 1920x1080 desktop. Use button="right" for '
      + 'context menus and clicks=2 to double-click (e.g. to open a file).',
    parameters: Type.Object({
      x: Type.Integer({ description: 'Absolute x pixel coordinate' }),
      y: Type.Integer({ description: 'Absolute y pixel coordinate' }),
      button: Type.Optional(Type.Union([
        Type.Literal('left'), Type.Literal('right'), Type.Literal('middle'),
      ], { description: 'Mouse button, default left' })),
      clicks: Type.Optional(Type.Integer({ description: 'Number of clicks, default 1' })),
    }),
    execute: async (_id, params) => {
      const button = params.button ?? 'left';
      const clicks = params.clicks ?? 1;
      const command = `import pyautogui; pyautogui.click(${params.x}, ${params.y}, `
        + `clicks=${clicks}, button='${button}')`;
      return screenshotResult(await post(client, '/step', { command }));
    },
  });

  const type_ = defineTool({
    name: 'type_text',
    label: 'Type text',
    description:
      'Type literal text into whatever currently has keyboard focus. Click the target field '
      + 'first. Does not press Enter unless the text contains a newline.',
    parameters: Type.Object({
      text: Type.String({ description: 'Text to type' }),
    }),
    execute: async (_id, params) => {
      const command = `import pyautogui; pyautogui.typewrite(${pythonString(params.text)}, interval=0.02)`;
      return screenshotResult(await post(client, '/step', { command }));
    },
  });

  const key = defineTool({
    name: 'key',
    label: 'Press keys',
    description:
      'Press a key, chord, or whitespace-separated key sequence. Examples: "enter", '
      + '"ctrl+s", "alt+f up up right enter", "ctrl+a backspace". This is a Linux '
      + 'desktop, so use ctrl (not cmd). This is NOT a shell: never pass xdotool, code, '
      + 'a URL, or literal text here; use type_text for literal text.',
    parameters: Type.Object({
      keys: Type.String({
        description: 'Key/chord sequence, e.g. "ctrl+s" or "alt+f up right enter"',
      }),
    }),
    execute: async (_id, params) => {
      const built = keyCommand(params.keys);
      if (!built.command) {
        return {
          content: [{
            type: 'text' as const,
            text: `Invalid key sequence${built.invalid.length
              ? `: ${built.invalid.join(', ')}` : ''}. `
              + 'The key tool accepts only real key names/chords, not shell commands, URLs, '
              + 'or literal text. Use type_text for literal text.',
          }],
          details: { invalidKeys: built.invalid },
        };
      }
      return screenshotResult(await post(client, '/step', { command: built.command }));
    },
  });

  const scroll = defineTool({
    name: 'scroll',
    label: 'Scroll',
    description:
      'Scroll the wheel at a position. Positive amount scrolls up, negative scrolls down. '
      + 'One unit is roughly one wheel click.',
    parameters: Type.Object({
      x: Type.Integer({ description: 'x coordinate to scroll at' }),
      y: Type.Integer({ description: 'y coordinate to scroll at' }),
      amount: Type.Integer({ description: 'Wheel clicks; negative scrolls down' }),
    }),
    execute: async (_id, params) => {
      const command = `import pyautogui; pyautogui.scroll(${params.amount}, `
        + `x=${params.x}, y=${params.y})`;
      return screenshotResult(await post(client, '/step', { command }));
    },
  });

  const drag = defineTool({
    name: 'drag',
    label: 'Drag',
    description:
      'Press the left button at a start point, move to an end point, and release. Use for '
      + 'selecting text or ranges, moving objects, and resizing.',
    parameters: Type.Object({
      startX: Type.Integer(), startY: Type.Integer(),
      endX: Type.Integer(), endY: Type.Integer(),
    }),
    execute: async (_id, params) => {
      const command = 'import pyautogui; '
        + `pyautogui.moveTo(${params.startX}, ${params.startY}); pyautogui.dragTo(`
        + `${params.endX}, ${params.endY}, duration=0.5, button='left')`;
      return screenshotResult(await post(client, '/step', { command }));
    },
  });

  const pythonTool = defineTool({
    name: 'python',
    label: 'Run pyautogui snippet',
    description:
      'Run a raw Python snippet inside the desktop VM (OSWorld\'s native action space). '
      + 'pyautogui is available. Use this for anything the other tools do not cover, such as '
      + 'multi-step input sequences or precise mouse control. Keep snippets short.',
    parameters: Type.Object({
      code: Type.String({ description: 'Python source to execute in the VM' }),
    }),
    execute: async (_id, params) =>
      screenshotResult(await post(client, '/step', { command: params.code })),
  });

  const computerExec = defineTool({
    name: 'computer_exec',
    label: 'Run guest computer code',
    description:
      'Run a bounded Bash script inside the Ubuntu guest for filesystem, CLI, archive, '
      + 'repository, download, conversion or data-processing work. Output, errors and exit '
      + 'code are returned and the exact script stays in the trace. Do not use this to drive '
      + 'Chrome or desktop UI: mouse/keyboard injection, accessibility automation, '
      + 'Playwright/Selenium and Chrome DevTools are rejected. Use web_* or desktop_* for '
      + 'visible interaction, then inspect their state separately.',
    parameters: Type.Object({
      script: Type.String({
        description: 'Bash script to run in the guest; keep it concise and bounded. '
          + 'Quote package/version constraints containing < or >. If this is a direct '
          + 'acceptance check and it passes, stop mutating the environment.',
        maxLength: 12000,
      }),
      timeout_seconds: Type.Optional(Type.Integer({
        description: 'Execution timeout in seconds, default 30, maximum 300. '
          + 'Use one adequate timeout for a necessary install/build instead of background polling.',
        minimum: 1,
        maximum: 300,
      })),
      working_dir: Type.Optional(Type.String({
        description: 'Absolute working directory inside the guest; defaults to /home/user',
      })),
    }),
    execute: async (_id, params) => screenshotResult(await post(client, '/exec', {
      script: params.script,
      language: 'bash',
      timeout_seconds: params.timeout_seconds ?? 30,
      working_dir: params.working_dir,
    }), undefined, true),
  });

  const computerPython = defineTool({
    name: 'computer_python',
    label: 'Run guest Python',
    description:
      'Run bounded Python 3 source inside the Ubuntu guest for structured data, files, '
      + 'documents, spreadsheets, downloads, conversion or computation. Output, errors and '
      + 'exit code are returned and the exact source stays in the trace. Do not include a '
      + 'shell command or shebang. Browser/desktop automation libraries and CDP access are '
      + 'rejected; use web_* or desktop_* for visible UI interaction.',
    parameters: Type.Object({
      code: Type.String({
        description: 'Python 3 source to run in the guest; keep it concise and bounded. '
          + 'If this is a direct acceptance check and it passes, stop mutating the environment.',
        maxLength: 12000,
      }),
      timeout_seconds: Type.Optional(Type.Integer({
        description: 'Execution timeout in seconds, default 30, maximum 300. '
          + 'Use one adequate timeout for a necessary install/build instead of background polling.',
        minimum: 1,
        maximum: 300,
      })),
      working_dir: Type.Optional(Type.String({
        description: 'Absolute working directory inside the guest; defaults to /home/user',
      })),
    }),
    execute: async (_id, params) => screenshotResult(await post(client, '/exec', {
      script: params.code,
      language: 'python',
      timeout_seconds: params.timeout_seconds ?? 30,
      working_dir: params.working_dir,
    }), undefined, true),
  });

  const wait = defineTool({
    name: 'wait',
    label: 'Wait',
    description: 'Wait for the screen to settle (application launch, page load, dialog).',
    parameters: Type.Object({
      seconds: Type.Optional(Type.Number({ description: 'Seconds to wait, default 2, max 10' })),
    }),
    execute: async (_id, params) => {
      const seconds = Math.min(10, Math.max(0.5, params.seconds ?? 2));
      return screenshotResult(await post(client, '/step', { command: 'WAIT', pause: seconds }));
    },
  });

  const done = defineTool({
    name: 'task_complete',
    label: 'Task complete',
    description:
      'Call this only when the task is fully finished and you have visually verified the '
      + 'result. If the task is impossible, call it with infeasible=true.',
    parameters: Type.Object({
      infeasible: Type.Optional(Type.Boolean({
        description: 'True if the task cannot be completed in this environment',
      })),
      summary: Type.String({ description: 'One sentence on what you did' }),
    }),
    execute: async (_id, params) => {
      // Premature completion is the second-largest measured failure mode: the
      // model announces success while the page plainly does not show it. The
      // gate makes the FIRST task_complete non-terminal — it hands back the
      // current state and asks for the specific evidence — and only accepts the
      // second one. Deliberately state-based rather than a wording rule, so it
      // does not encode anything about any particular task.
      //
      // Infeasible is exempt because evidence for absence often cannot be shown
      // in one screenshot; challenging it would push a model to invent visual
      // proof. The evaluator, not this gate, remains the verdict.
      if (verifyGate && !params.infeasible && !gateTripped) {
        gateTripped = true;
        const state = await post(client, '/step', { command: 'WAIT', pause: 0.5 })
          .catch(() => undefined);
        const challenge =
          `You said: "${params.summary}". Before this is accepted, check it.\n`
          + 'Look at the current state below and answer concretely: what in it shows '
          + 'the task is actually done? Name the value, text or page you can see.\n'
          + 'If the evidence is there, call task_complete again and it will be '
          + 'accepted. If it is not there, the task is NOT finished — keep working. '
          + 'Do not call task_complete again just to get past this message.';
        return state
          ? screenshotResult(state, challenge, textOnlyGate)
          : { content: [{ type: 'text', text: challenge }], details: { gated: true } };
      }
      // OSWorld scores the VM's final state; this only ends the agent loop.
      await post(client, '/step', {
        command: params.infeasible ? 'FAIL' : 'DONE',
      }).catch(() => undefined);
      return {
        content: [{
          type: 'text',
          text: `Completion recorded: ${params.summary}. The episode is over — `
            + 'stop here and do not call any further tools.',
        }],
        details: { infeasible: params.infeasible ?? false, terminal: true },
        terminate: true,
      };
    },
  });

  return [
    screenshot, click, type_, key, scroll, drag, pythonTool, computerExec,
    computerPython, wait, done,
  ];
}
