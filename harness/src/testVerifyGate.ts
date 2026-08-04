/**
 * Deterministic check of the task_complete verification gate.
 *
 * Calls the tool's execute directly rather than hoping a model happens to
 * trigger it, so the behaviour is asserted rather than observed once:
 *   1. first non-infeasible completion is challenged, NOT terminal
 *   2. second is accepted and terminal
 *   3. infeasible is exempt and terminal on the first call
 *   4. with the gate off, the first call is terminal (no behaviour change)
 */
import { createComputerTools, type EnvClient } from './computerTools.js';

const client: EnvClient = { episodeId: process.argv[2]!, baseUrl: 'http://127.0.0.1:8077' };

type Tool = { name: string; execute: (id: string, p: unknown) => Promise<unknown> };
const pick = (tools: unknown[]) =>
  (tools as Tool[]).find((t) => t.name === 'task_complete')!;

const isTerminal = (r: unknown) =>
  Boolean((r as { details?: { terminal?: boolean } }).details?.terminal);
const textOf = (r: unknown) =>
  ((r as { content: { type: string; text?: string }[] }).content
    .filter((c) => c.type === 'text').map((c) => c.text).join(' '));

let ok = true;
const check = (label: string, pass: boolean) => {
  console.log(`${pass ? 'PASS' : 'FAIL'} ${label}`);
  if (!pass) ok = false;
};

const gated = pick(createComputerTools(client, true));
const first = await gated.execute('1', { summary: 'I set the forecast to monthly' });
check('first completion is challenged, not terminal', !isTerminal(first));
check('challenge asks for concrete evidence',
  /what in it shows|Name the value/i.test(textOf(first)));
check('challenge quotes the claim back', textOf(first).includes('monthly'));

const second = await gated.execute('2', { summary: 'confirmed, the page shows it' });
check('second completion is accepted', isTerminal(second));

const infeasible = pick(createComputerTools(client, true));
const inf = await infeasible.execute('3', { summary: 'cannot be done', infeasible: true });
check('infeasible is exempt and terminal on first call', isTerminal(inf));

const ungated = pick(createComputerTools(client, false));
const plain = await ungated.execute('4', { summary: 'done' });
check('gate off leaves original behaviour terminal', isTerminal(plain));

console.log('\nRESULT:', ok ? 'ALL PASS' : 'FAILURES PRESENT');
process.exit(ok ? 0 : 1);
