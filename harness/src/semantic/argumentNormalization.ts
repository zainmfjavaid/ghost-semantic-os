import type { TSchema } from 'typebox';

type SchemaNode = TSchema & {
  type?: string;
  anyOf?: TSchema[];
  oneOf?: TSchema[];
  $ref?: string;
  $defs?: Record<string, TSchema>;
  properties?: Record<string, TSchema>;
  patternProperties?: Record<string, TSchema>;
  additionalProperties?: boolean | TSchema;
  items?: TSchema;
  required?: string[];
  const?: unknown;
};

export interface ModelArgumentNormalization {
  readonly decoded_json_paths: readonly string[];
  readonly normalized_args: unknown;
}

export interface NormalizedModelArguments {
  readonly value: unknown;
  readonly evidence: ModelArgumentNormalization | null;
}

type Definitions = ReadonlyMap<string, TSchema>;

function withDefinitions(schema: SchemaNode, inherited: Definitions): Definitions {
  if (!schema.$defs) return inherited;
  const merged = new Map(inherited);
  for (const [name, definition] of Object.entries(schema.$defs)) {
    merged.set(name, definition);
  }
  return merged;
}

function refName(ref: string): string {
  const fragment = ref.split('/').pop();
  return fragment ?? ref;
}

function expandCandidates(
  schema: TSchema,
  inherited: Definitions,
  seenRefs = new Set<string>(),
): Array<{ schema: SchemaNode; definitions: Definitions }> {
  const node = schema as SchemaNode;
  const definitions = withDefinitions(node, inherited);
  if (node.$ref) {
    const name = refName(node.$ref);
    if (seenRefs.has(name)) return [];
    const target = definitions.get(name);
    if (!target) return [];
    const nextSeen = new Set(seenRefs);
    nextSeen.add(name);
    return expandCandidates(target, definitions, nextSeen);
  }
  const alternatives = node.anyOf ?? node.oneOf;
  if (alternatives) {
    return alternatives.flatMap((alternative) => (
      expandCandidates(alternative, definitions, new Set(seenRefs))
    ));
  }
  return [{ schema: node, definitions }];
}

function jsonKind(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return 'array';
  return typeof value === 'object' ? 'object' : typeof value;
}

function allowedKinds(schema: TSchema, definitions: Definitions): Set<string> {
  return new Set(
    expandCandidates(schema, definitions)
      .map((candidate) => candidate.schema.type)
      .filter((kind): kind is string => typeof kind === 'string'),
  );
}

function structuredKindExpected(schema: TSchema, definitions: Definitions): Set<string> | null {
  const kinds = allowedKinds(schema, definitions);
  if (kinds.has('string')) return null;
  const structured = new Set([...kinds].filter((kind) => kind === 'object' || kind === 'array'));
  return structured.size > 0 ? structured : null;
}

function propertyPath(parent: string, property: string): string {
  return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(property)
    ? `${parent}.${property}`
    : `${parent}[${JSON.stringify(property)}]`;
}

function objectCandidateScore(schema: SchemaNode, value: Record<string, unknown>): number {
  const properties = schema.properties ?? {};
  const keys = Object.keys(value);
  if (schema.additionalProperties === false && keys.some((key) => !(key in properties))) {
    return -1;
  }
  let score = 0;
  for (const [key, propertySchema] of Object.entries(properties)) {
    if (!(key in value)) continue;
    score += 1;
    const expected = (propertySchema as SchemaNode).const;
    if (expected !== undefined) {
      if (value[key] !== expected) return -1;
      score += 100;
    }
  }
  for (const required of schema.required ?? []) {
    if (!(required in value)) return -1;
  }
  return score;
}

function selectCandidate(
  schema: TSchema,
  definitions: Definitions,
  value: unknown,
): { schema: SchemaNode; definitions: Definitions } | null {
  const kind = jsonKind(value);
  const candidates = expandCandidates(schema, definitions)
    .filter((candidate) => candidate.schema.type === kind);
  if (candidates.length === 0) return null;
  if (kind !== 'object' || value === null || Array.isArray(value)) return candidates[0] ?? null;
  const object = value as Record<string, unknown>;
  const ranked = candidates
    .map((candidate) => ({ candidate, score: objectCandidateScore(candidate.schema, object) }))
    .filter(({ score }) => score >= 0)
    .sort((left, right) => right.score - left.score);
  return ranked[0]?.candidate ?? null;
}

function normalizeAtSchema(
  value: unknown,
  schema: TSchema,
  inherited: Definitions,
  path: string,
  decodedPaths: string[],
): unknown {
  const root = schema as SchemaNode;
  const definitions = withDefinitions(root, inherited);
  let candidateValue = value;

  if (typeof candidateValue === 'string') {
    const expectedKinds = structuredKindExpected(schema, definitions);
    const trimmed = candidateValue.trim();
    const possibleKind = trimmed.startsWith('{')
      ? 'object'
      : trimmed.startsWith('[') ? 'array' : null;
    if (expectedKinds && possibleKind && expectedKinds.has(possibleKind)) {
      try {
        const parsed = JSON.parse(candidateValue) as unknown;
        if (jsonKind(parsed) === possibleKind) {
          candidateValue = parsed;
          decodedPaths.push(path);
        }
      } catch {
        // Leave malformed JSON untouched so the unchanged canonical validator
        // produces the ordinary typed validation failure.
      }
    }
  }

  const selected = selectCandidate(schema, definitions, candidateValue);
  if (!selected) return candidateValue;
  const selectedSchema = selected.schema;
  const selectedDefinitions = selected.definitions;

  if (selectedSchema.type === 'array' && Array.isArray(candidateValue) && selectedSchema.items) {
    return candidateValue.map((item, index) => normalizeAtSchema(
      item,
      selectedSchema.items as TSchema,
      selectedDefinitions,
      `${path}[${index}]`,
      decodedPaths,
    ));
  }

  if (
    selectedSchema.type === 'object'
    && candidateValue !== null
    && typeof candidateValue === 'object'
    && !Array.isArray(candidateValue)
  ) {
    const object = candidateValue as Record<string, unknown>;
    const normalized: Record<string, unknown> = { ...object };
    const properties = selectedSchema.properties ?? {};
    for (const [key, propertySchema] of Object.entries(properties)) {
      if (!(key in object)) continue;
      normalized[key] = normalizeAtSchema(
        object[key],
        propertySchema,
        selectedDefinitions,
        propertyPath(path, key),
        decodedPaths,
      );
    }
    for (const [pattern, propertySchema] of Object.entries(selectedSchema.patternProperties ?? {})) {
      const regex = new RegExp(pattern);
      for (const [key, child] of Object.entries(object)) {
        if (!regex.test(key)) continue;
        normalized[key] = normalizeAtSchema(
          child,
          propertySchema,
          selectedDefinitions,
          propertyPath(path, key),
          decodedPaths,
        );
      }
    }
    if (
      selectedSchema.additionalProperties
      && typeof selectedSchema.additionalProperties === 'object'
    ) {
      for (const [key, child] of Object.entries(object)) {
        if (key in properties) continue;
        normalized[key] = normalizeAtSchema(
          child,
          selectedSchema.additionalProperties,
          selectedDefinitions,
          propertyPath(path, key),
          decodedPaths,
        );
      }
    }
    return normalized;
  }

  return candidateValue;
}

/**
 * Repair only one provider serialization defect: JSON text in a position whose
 * advertised TypeBox schema requires an object or array. The provider schema
 * remains unchanged, and Pi validates the returned value immediately after
 * this hook. String-valued fields (including code, text, commands, and URLs)
 * are never decoded, even when their contents happen to look like JSON.
 */
export function normalizeModelArguments(
  args: unknown,
  schema: TSchema,
): NormalizedModelArguments {
  const decodedPaths: string[] = [];
  const value = normalizeAtSchema(args, schema, new Map(), '$', decodedPaths);
  return {
    value,
    evidence: decodedPaths.length === 0 ? null : {
      decoded_json_paths: decodedPaths,
      normalized_args: value,
    },
  };
}
