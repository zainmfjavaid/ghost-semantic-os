import { createWebTools, type EnvClient } from './computerTools.js';
import {
  createSemanticRuntimeTools,
  transportSemanticToolName,
  type SemanticToolTransport,
} from './semanticRuntimeTools.js';
import { SEMANTIC_TOOL_DEFINITIONS } from './semantic/tools.js';

/**
 * The deliberately broader, still-zero-pixel semantic runtime.
 *
 * Keep this list explicit. `createWebTools` also contains screenshots, raw
 * keyboard/coordinate controls, legacy completion, and compatibility stubs.
 * Selecting by a pinned allow-list prevents those capabilities from silently
 * returning when the frozen v15 tool factory changes.
 */
export const SEMANTIC_PLUS_LEGACY_TOOL_NAMES = [
  'computer_exec',
  'computer_python',
  'web_elements',
  'web_find',
  'web_click',
  'web_type',
  'web_navigate',
  'web_read',
  'web_search',
  'web_read_pages',
  'web_scroll',
  'web_frames',
  'web_js',
  'web_actions',
  'web_tabs',
  'web_switch_tab',
  'web_close_tab',
] as const;

export const SEMANTIC_PLUS_FORBIDDEN_TOOL_NAMES = [
  'screenshot',
  'click',
  'move',
  'type_text',
  'key',
  'scroll',
  'drag',
  'python',
  'wait',
  'ui_actions',
  'desktop_find',
  'desktop_click',
  'desktop_hover',
  'desktop_type',
  'desktop_actions',
] as const;

export function semanticPlusExpectedToolNames(
  transport: SemanticToolTransport = 'canonical',
): string[] {
  return [
    ...SEMANTIC_TOOL_DEFINITIONS.map(
      (definition) => transportSemanticToolName(definition.name, transport),
    ),
    ...SEMANTIC_PLUS_LEGACY_TOOL_NAMES,
  ];
}

export function createSemanticPlusRuntimeTools(
  client: EnvClient,
  onSemanticOperations: (count: number) => void = () => {},
  transport: SemanticToolTransport = 'canonical',
) {
  const semanticTools = createSemanticRuntimeTools(
    client,
    onSemanticOperations,
    transport,
  );
  // `textOnly=true` makes every /web call send observe:false and suppresses
  // image parts. computer_exec/computer_python already return bounded text.
  const legacyCandidates = createWebTools(
    client,
    true,
    false,
    false,
    false,
    true,
    false,
  );
  const byName = new Map(legacyCandidates.map((tool) => [tool.name, tool]));
  const legacyTools = SEMANTIC_PLUS_LEGACY_TOOL_NAMES.map((name) => {
    const tool = byName.get(name);
    if (!tool) throw new Error(`semantic-plus-v1 missing required tool: ${name}`);
    if (name === 'computer_exec' || name === 'computer_python') {
      // The frozen v15 descriptions point native-UI work at desktop_* tools,
      // which are intentionally absent here. Correct the provider-visible
      // description without mutating the frozen factory or its other runtimes.
      return {
        ...tool,
        description: tool.description.replace(
          /use web_\* or desktop_\* for visible (?:UI )?interaction/i,
          'use web_* for webpage interaction or computer.query/computer.act for native UI',
        ),
      };
    }
    return tool;
  });
  const tools = [...semanticTools, ...legacyTools];
  const names = tools.map((tool) => tool.name);
  const expected = semanticPlusExpectedToolNames(transport);
  if (JSON.stringify(names) !== JSON.stringify(expected)) {
    throw new Error(`semantic-plus-v1 tool surface drift: ${JSON.stringify(names)}`);
  }
  const forbidden = names.filter((name) => (
    (SEMANTIC_PLUS_FORBIDDEN_TOOL_NAMES as readonly string[]).includes(name)
  ));
  if (forbidden.length) {
    throw new Error(`semantic-plus-v1 exposed forbidden tools: ${JSON.stringify(forbidden)}`);
  }
  return tools;
}
