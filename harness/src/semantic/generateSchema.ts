import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { SemanticProtocolMessageSchema } from "./protocol.js";

const outputPath = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../protocol/semantic-v1.schema.json",
);

export function renderSemanticProtocolSchema(): string {
  // TypeBox's runtime Cyclic form uses local symbolic refs (for example
  // "$ref":"Filter"). Give every emitted definition a unique 2020-12 anchor
  // so the checked-in artifact is portable to validators other than TypeBox.
  let anchorSequence = 0;
  const normalizeReferences = (
    value: unknown,
    inherited: Readonly<Record<string, string>> = {},
  ): unknown => {
    if (Array.isArray(value)) return value.map((item) => normalizeReferences(item, inherited));
    if (value === null || typeof value !== "object") return value;
    const input = value as Record<string, unknown>;
    const defs = input.$defs && typeof input.$defs === "object" && !Array.isArray(input.$defs)
      ? input.$defs as Record<string, unknown>
      : undefined;
    const local = { ...inherited };
    if (defs) {
      for (const name of Object.keys(defs)) {
        anchorSequence += 1;
        local[name] = `${name.toLowerCase()}_${anchorSequence}`;
      }
    }
    const output: Record<string, unknown> = {};
    for (const [key, child] of Object.entries(input)) {
      if (key === "$ref" && typeof child === "string" && local[child]) {
        output[key] = `#${local[child]}`;
      } else if (key === "$defs" && defs) {
        output[key] = Object.fromEntries(Object.entries(defs).map(([name, definition]) => [
          name, (() => {
            const normalized = normalizeReferences(definition, local) as Record<string, unknown>;
            // TypeBox adds a symbolic $id to Cyclic definitions for its own
            // evaluator. Keeping it would start a nested schema resource and
            // make the document-root anchor references invalid under JSON
            // Schema 2020-12. The unique anchor is the portable identity.
            delete normalized.$id;
            return {
            $anchor: local[name],
              ...normalized,
            };
          })(),
        ]));
      } else {
        output[key] = normalizeReferences(child, local);
      }
    }
    return output;
  };
  return `${JSON.stringify(normalizeReferences(SemanticProtocolMessageSchema), null, 2)}\n`;
}

async function main(): Promise<void> {
  const rendered = renderSemanticProtocolSchema();
  if (process.argv.includes("--check")) {
    let existing: string;
    try {
      existing = await readFile(outputPath, "utf8");
    } catch {
      throw new Error(`Generated semantic schema is missing: ${outputPath}`);
    }
    if (existing !== rendered) {
      throw new Error(
        `Generated semantic schema is stale: ${outputPath}\nRun: npx tsx src/semantic/generateSchema.ts`,
      );
    }
    process.stdout.write(`PASS semantic protocol schema is current: ${outputPath}\n`);
    return;
  }

  await writeFile(outputPath, rendered, "utf8");
  process.stdout.write(`Wrote semantic protocol schema: ${outputPath}\n`);
}

void main().catch((error: unknown) => {
  console.error(error);
  process.exitCode = 1;
});
