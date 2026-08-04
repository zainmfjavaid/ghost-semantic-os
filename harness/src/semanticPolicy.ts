import type { InlineExtension } from '@earendil-works/pi-coding-agent';
import { canonicalSemanticToolName } from './semanticRuntimeTools.js';

export interface SemanticPolicyTelemetry {
  providerCalls: number;
  imagePartsCreated: number;
  imagePartsInSession: number;
  imagePartsSent: number;
  pixelsSentToPolicyModel: number;
  compactionEvents: number;
  expandedObservationCount: number;
  supersededObservationCount: number;
  largestRetainedSemanticResult: number;
  activeDataHandles: number;
  lastMessageCount: number;
  lastToolResultCharacters: number;
  lastEstimatedInputTokens: number;
}

export function createSemanticPolicyTelemetry(): SemanticPolicyTelemetry {
  return {
    providerCalls: 0,
    imagePartsCreated: 0,
    imagePartsInSession: 0,
    imagePartsSent: 0,
    pixelsSentToPolicyModel: 0,
    compactionEvents: 0,
    expandedObservationCount: 0,
    supersededObservationCount: 0,
    largestRetainedSemanticResult: 0,
    activeDataHandles: 0,
    lastMessageCount: 0,
    lastToolResultCharacters: 0,
    lastEstimatedInputTokens: 0,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isImageMime(value: unknown): boolean {
  return typeof value === 'string' && value.toLowerCase().startsWith('image/');
}

/**
 * Recursively inspect the fully serialized provider payload. This deliberately
 * recognizes provider-specific shapes as well as Pi's canonical image block.
 * Structural paths are safe to trace; image data itself is never returned.
 */
export function findImagePayloadPaths(value: unknown, path = '$'): string[] {
  const found: string[] = [];
  const walk = (current: unknown, currentPath: string, seen: Set<object>): void => {
    if (typeof current === 'string') {
      if (/^data:image\//i.test(current.trim())) found.push(currentPath);
      return;
    }
    if (!current || typeof current !== 'object') return;
    if (seen.has(current)) return;
    seen.add(current);
    if (Array.isArray(current)) {
      current.forEach((item, index) => walk(item, `${currentPath}[${index}]`, seen));
      return;
    }
    const record = current as Record<string, unknown>;
    if (record.type === 'image' || record.type === 'image_url' || record.type === 'input_image') {
      found.push(currentPath);
    }
    if (
      isRecord(record.source)
      && record.source.type === 'base64'
      && (isImageMime(record.source.media_type) || isImageMime(record.source.mimeType))
    ) {
      found.push(`${currentPath}.source`);
    }
    for (const [key, child] of Object.entries(record)) {
      const normalized = key.replaceAll('_', '').toLowerCase();
      if (normalized === 'imageurl' || normalized === 'imagedata' || normalized === 'imagebytes') {
        found.push(`${currentPath}.${key}`);
      }
      walk(child, `${currentPath}.${key}`, seen);
    }
  };
  walk(value, path, new Set());
  return [...new Set(found)];
}

type ToolResultMessage = {
  role: 'toolResult';
  toolName?: string;
  content?: Array<{ type?: string; text?: string; [key: string]: unknown }>;
  details?: unknown;
  [key: string]: unknown;
};

function semanticDetails(message: ToolResultMessage): Record<string, unknown> | undefined {
  if (!isRecord(message.details)) return undefined;
  const nested = message.details.semantic;
  return isRecord(nested) ? nested : message.details;
}

function compactMessage(message: ToolResultMessage, reason: string): ToolResultMessage {
  const details = semanticDetails(message) ?? {};
  const observation = details.observationId ?? details.observation_id ?? 'none';
  const receipt = details.receiptId ?? details.receipt_id ?? 'none';
  const resource = details.resource ?? 'none';
  const scope = details.scope ?? 'none';
  const revision = details.revision ?? details.afterRevision ?? details.after_revision ?? 'none';
  const overflowHandle = details.overflowHandle ?? details.overflow_handle
    ?? details.dataHandle ?? details.data_handle;
  const collectionHandle = details.collectionHandle ?? details.collection_handle;
  return {
    ...message,
    content: [{
      type: 'text',
      text: `[semantic result superseded: reason=${reason} observation=${String(observation)} `
        + `receipt=${String(receipt)} scope=${JSON.stringify(scope)} revision=${String(revision)} `
        + `resource=${String(resource)}`
        + `${overflowHandle ? ` overflow_handle=${String(overflowHandle)}` : ''}`
        + `${collectionHandle ? ` collection_handle=${String(collectionHandle)}` : ''}. `
        + 'Re-query this resource or the named handle if its contents are needed.]',
    }],
  };
}

function compactEphemeralWebMessage(
  message: ToolResultMessage, reason: string,
): ToolResultMessage {
  const details = isRecord(message.details) ? message.details : {};
  return {
    ...message,
    content: [{
      type: 'text',
      text: `[web observation superseded: reason=${reason} tool=${String(message.toolName ?? '')} `
        + `step=${String(details.steps ?? 'unknown')}. Re-run the relevant web observation `
        + 'before using element, tab, or frame indices.]',
    }],
  };
}

function resultCharacters(message: ToolResultMessage): number {
  return (message.content ?? []).reduce(
    (sum, item) => sum + (typeof item.text === 'string' ? item.text.length : 0), 0,
  );
}

/**
 * Compact semantic results and ephemeral web index spaces. The authoritative
 * session and trace stay complete; this returns a provider-context projection
 * with re-query guidance. The three-tool semantic-simple experiment deliberately
 * retains every result verbatim. It is testing whether complete observation
 * history helps the policy model; applying the semantic kernel's replacement
 * policy here would confound that experiment and erase cross-application facts.
 */
export function compactSemanticContext(
  messages: unknown[], telemetry: SemanticPolicyTelemetry,
): unknown[] {
  const output = [...messages];
  const newestQueryKeys = new Set<string>();
  const activeHandles = new Set<string>();
  let fullQueries = 0;
  let fullActions = 0;
  let fullRuns = 0;
  let expanded = 0;
  let superseded = 0;
  let changed = false;
  const newestEphemeralWebKeys = new Set<string>();

  for (let index = output.length - 1; index >= 0; index -= 1) {
    const candidate = output[index];
    if (!isRecord(candidate) || candidate.role !== 'toolResult') continue;
    const message = candidate as ToolResultMessage;
    const imagePaths = findImagePayloadPaths(message);
    if (imagePaths.length) {
      telemetry.imagePartsInSession += imagePaths.length;
      throw new Error(`policy_violation: image content in semantic session at ${imagePaths.join(', ')}`);
    }
    if (
      message.toolName === 'read_computer'
      || message.toolName === 'computer_click'
      || message.toolName === 'computer_type'
    ) {
      expanded += 1;
      telemetry.largestRetainedSemanticResult = Math.max(
        telemetry.largestRetainedSemanticResult, resultCharacters(message),
      );
      continue;
    }
    const tool = canonicalSemanticToolName(message.toolName ?? '');
    const ephemeralWebKey = (
      tool === 'web_elements' || tool === 'web_find' || tool === 'web_switch_tab'
    )
      ? 'web-element-index-space'
      : tool === 'web_tabs'
        ? 'web-tab-index-space'
        : tool === 'web_frames'
          ? 'web-frame-index-space'
          : null;
    if (ephemeralWebKey) {
      if (newestEphemeralWebKeys.has(ephemeralWebKey)) {
        output[index] = compactEphemeralWebMessage(
          message, `newer ${ephemeralWebKey} observation`,
        );
        superseded += 1;
        changed = true;
      } else {
        newestEphemeralWebKeys.add(ephemeralWebKey);
        expanded += 1;
        telemetry.largestRetainedSemanticResult = Math.max(
          telemetry.largestRetainedSemanticResult, resultCharacters(message),
        );
      }
      continue;
    }
    if (!tool.startsWith('computer.')) continue;
    const details = semanticDetails(message) ?? {};
    const overflowHandle = details.overflowHandle ?? details.overflow_handle
      ?? details.dataHandle ?? details.data_handle;
    const collectionHandle = details.collectionHandle ?? details.collection_handle;
    if (typeof overflowHandle === 'string') activeHandles.add(overflowHandle);
    if (typeof collectionHandle === 'string') activeHandles.add(collectionHandle);
    let keep = true;
    let reason = 'retention limit';
    if (tool === 'computer.query') {
      const key = JSON.stringify([
        details.adapter ?? details.adapterId ?? details.adapter_id ?? '',
        details.surface ?? details.surfaceId ?? details.surface_id ?? '',
        details.resource ?? '',
        details.scope ?? {},
        details.where ?? {},
        details.parameters ?? {},
        details.cursor ?? null,
      ]);
      if (newestQueryKeys.has(key)) {
        keep = false;
        reason = 'newer observation for resource';
      } else if (fullQueries >= 8) {
        keep = false;
        reason = 'eight-observation context limit';
      } else {
        newestQueryKeys.add(key);
        fullQueries += 1;
      }
    } else if (tool === 'computer.act') {
      fullActions += 1;
      keep = fullActions <= 12;
      reason = 'twelve-action receipt limit';
    } else if (tool === 'computer.run') {
      fullRuns += 1;
      keep = fullRuns <= 4;
      reason = 'older run output retained by handles';
    }
    if (keep) {
      expanded += 1;
      telemetry.largestRetainedSemanticResult = Math.max(
        telemetry.largestRetainedSemanticResult, resultCharacters(message),
      );
    } else {
      output[index] = compactMessage(message, reason);
      superseded += 1;
      changed = true;
    }
  }
  telemetry.expandedObservationCount = expanded;
  telemetry.supersededObservationCount = superseded;
  telemetry.activeDataHandles = activeHandles.size;
  telemetry.lastMessageCount = output.length;
  telemetry.lastToolResultCharacters = output.reduce<number>((sum, candidate) => {
    if (!isRecord(candidate) || candidate.role !== 'toolResult') return sum;
    return sum + resultCharacters(candidate as ToolResultMessage);
  }, 0);
  // This is telemetry, not a tokenizer-dependent admission control.
  telemetry.lastEstimatedInputTokens = Math.ceil(telemetry.lastToolResultCharacters / 4);
  if (changed) telemetry.compactionEvents += 1;
  return output;
}

export function createSemanticPolicyExtension(
  telemetry: SemanticPolicyTelemetry,
): InlineExtension {
  return {
    name: 'semantic-zero-image-policy',
    hidden: true,
    factory: (pi) => {
      pi.on('context', (event) => ({
        messages: compactSemanticContext(event.messages, telemetry) as typeof event.messages,
      }));
      pi.on('before_provider_request', (event) => {
        telemetry.providerCalls += 1;
        const imagePaths = findImagePayloadPaths(event.payload);
        if (imagePaths.length) {
          telemetry.imagePartsSent += imagePaths.length;
          throw new Error(
            `policy_violation: provider payload contains image blocks at ${imagePaths.join(', ')}`,
          );
        }
        return event.payload;
      });
    },
  };
}

export function assertZeroImageTelemetry(telemetry: SemanticPolicyTelemetry): void {
  const counters = {
    image_parts_created: telemetry.imagePartsCreated,
    image_parts_in_session: telemetry.imagePartsInSession,
    image_parts_sent: telemetry.imagePartsSent,
    pixels_sent_to_policy_model: telemetry.pixelsSentToPolicyModel,
  };
  const violations = Object.entries(counters).filter(([, value]) => value !== 0);
  if (violations.length) {
    throw new Error(
      `policy_violation: strict semantic image counters are nonzero: ${JSON.stringify(counters)}`,
    );
  }
}
