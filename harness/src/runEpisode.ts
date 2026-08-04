import {
  createAgentSession,
  DefaultResourceLoader,
  getAgentDir,
  ModelRuntime,
  SessionManager,
  SettingsManager,
  type InlineExtension,
} from '@earendil-works/pi-coding-agent';
import {
  createComputerTools, createSemanticDesktopTools, createSomTools, createWebTools,
  type EnvClient, type Notices,
} from './computerTools.js';
import {
  registerOpenAICompatibleEndpoint,
  type LocalThinkingFormat,
} from './modelEndpoint.js';
import {
  assertZeroImageTelemetry,
  createSemanticPolicyExtension,
  createSemanticPolicyTelemetry,
  type SemanticPolicyTelemetry,
} from './semanticPolicy.js';
import {
  canonicalSemanticToolName,
  createSemanticRuntimeTools,
  semanticToolTransport,
  transportSemanticToolName,
} from './semanticRuntimeTools.js';
import {
  createSemanticPlusRuntimeTools,
  semanticPlusExpectedToolNames,
} from './semanticPlusRuntimeTools.js';
import {
  createSemanticSimpleRuntimeTools,
  SEMANTIC_SIMPLE_TOOL_NAMES,
} from './semanticSimpleRuntimeTools.js';
import {
  buildSemanticSimpleSystemPrompt,
} from './semanticSimpleSystemPrompt.js';
import { SEMANTIC_TOOL_DEFINITIONS } from './semantic/tools.js';
import { normalizeModelArguments } from './semantic/argumentNormalization.js';

export const GUEST_HOME = '/home/user';
export const SEMANTIC_PROTOCOL_VERSION = '1.0';
export type RuntimeName =
  | 'vision-v15'
  | 'hybrid-v15'
  | 'semantic-v1'
  | 'semantic-plus-v1'
  | 'semantic-simple-v1'
  | 'semantic-visual-v1';

/**
 * One OSWorld episode driven by a pi agent session.
 *
 * pi is the harness — identical loop, identical tools, identical prompt for
 * every model. The only thing that varies between conditions is the `model`
 * argument, which is the whole point: it makes "frontier vs local" a
 * one-variable comparison instead of two different scaffolds.
 */

/**
 * Browser-only system prompt.
 *
 * The desktop prompt below instructs the model to screenshot the desktop, use
 * the dock, prefer click/type_text/key/scroll/drag, and fall back to python --
 * NONE of which exist in browser-only mode. So the "model hallucinates tools it
 * was never given" behaviour logged earlier was substantially our own prompt
 * telling it to use them, and the redirect stubs were treating a symptom of a
 * prompt/tool mismatch we created.
 */
const BROWSER_SYSTEM_PROMPT = `You are completing a task in a web browser. You interact
only through the provided tools. There is no desktop, no file system and no shell here:
the browser is the entire environment.

How to work:
- web_elements lists every interactive element on the page with an index, read live from
  the DOM. Call it before acting, and again after the page changes, because indices move.
- Act with web_click and web_type using those indices. Never guess screen coordinates —
  there is no cursor and no coordinate space in this environment.
- web_find searches ALL interactive elements by their text when the listing is capped.
- web_js runs JavaScript in the page. Prefer it for anything computational: resolving
  dates, setting date/form inputs directly, extracting lists of results, or checking
  state. It includes reliable ghost.inspect/find/fill/click/select/check/submit helpers;
  prefer them over hand-writing DOM event plumbing. Always return a compact object saying
  what the script found or changed, and set expect_change=true when it mutates the page.
- If a widget is not in the main document, call web_frames and run web_js in the matching
  frame. Element rows marked frame=N are inside that embedded frame.
- web_actions runs an ordered program through trusted Playwright input. Prefer semantic
  targets such as by=role + role/name, by=label, by=placeholder or by=text; these survive
  reactive re-renders better than generated CSS paths. CSS selectors remain available.
- web_tabs lists open tabs; web_switch_tab / web_close_tab manage them. Clicking a link
  can open a new tab and move you there without warning, and some tasks are graded on
  which tabs are open, so check with web_tabs if the page is not what you expect.
- web_read returns the page text. Use it to check what is actually on the page rather
  than assuming an action worked.
- web_search batches public-web queries; web_read_pages batches the visible text from
  their result URLs while restoring the original task tab. Use them for multi-record
  research instead of spending one navigation and cleanup cycle per source.
- Treat the preloaded page, locale and account as observed context, not an eternal domain
  restriction. Follow a cross-site destination when the task asks for it or the current
  page establishes it; otherwise prefer real links/controls over guessing alternate URLs.
- Elements marked "below/above fold" are off-screen but still directly clickable; they
  are scrolled into view for you. Do not spend turns scrolling to reach a control.
- If an action does not change the page, do not repeat it. Re-read the element list and
  target something else, or navigate directly.
- The task is graded on the final state of the browser, so leave the page in the state
  the task asks for. Do not navigate away afterwards to "check" something.
- When the task is genuinely finished and you have confirmed it on the page, call
  task_complete. If it is impossible in this environment, call task_complete with
  infeasible set to true. Do not call it just because you are running out of ideas.`;

const HYBRID_SYSTEM_PROMPT = `You are operating a real Ubuntu desktop to complete a task.
You interact only through the provided browser and desktop tools; they are two semantic
views of the same live machine.

How to work:
- Start with a screenshot. Use web_* tools for controls and content inside the current page.
  Use desktop_* tools for any control exposed by the operating-system accessibility tree.
- The screenshot result includes compact desktop_surfaces records for the current app,
  window, dialog and document title. When a task refers to "this file", an open document,
  or information "listed here", identify and read that source artifact first. Never infer
  its contents from the task wording or from memory before creating the output.
- After opening or switching a native surface, call desktop_find with no filters to list its
  current controls. Then query by visible name, role and state. If candidates share a label,
  pass their returned application/window/dialog context to narrow the action. If unlabeled or
  identical candidates remain, act with that record's current snapshot-scoped ref. Never guess,
  reuse a ref after the UI changes, or use private coordinates.
- Use bounded web_actions or desktop_actions programs for causal sequences. Express waits
  as semantic state conditions when possible; each target is resolved again after changes.
- When one causal sequence crosses from webpage DOM into browser chrome or an OS dialog,
  use ui_actions to execute the guarded web and native steps as one ordered program. A
  repeated native control may be targeted again by exact label/role/context because it is
  re-resolved live; never reuse a stale ref or spend a separate listing call between
  repetitions when the previous result already established the unique control pattern.
- key and type_text operate the currently focused desktop control. key accepts real key
  names/chords only, not shell commands, URLs or literal text.
- Use computer_exec for bounded filesystem, archive, repository, conversion and CLI work
  inside the Ubuntu guest. It cannot drive browser/desktop UI; use web_* or desktop_* for
  visible interaction and inspect that live state after any computer-side change.
- For a necessary long install or build, use one synchronous computer_exec call with an
  adequate timeout instead of launching it in the background and spending calls polling it.
- If code changes a file that is currently open in an application, reload or reopen that
  file before the final UI save and verify the visible content. Otherwise the application's
  stale in-memory copy can overwrite the correct disk edit.
- Use computer_python when structured parsing, document/spreadsheet generation or data
  transformation is clearer in Python; do not paste Python source into computer_exec Bash.
- For multi-record public research, batch queries with web_search and read selected result
  URLs with web_read_pages. Both restore the original task tab after the traced call.
- When the user names a source, derive every output record from that source or a link it
  exposes. Keep exact per-record title, URL, date and version evidence; do not replace a
  missing source result with a plausible item from memory or a broader search.
- Match the strategy to the task's scale before acting. For multi-record research, data
  extraction, or document/spreadsheet updates, inspect the input once and batch public HTTP,
  parsing, conversion and file edits with bounded computer_exec where possible. Use browser
  or desktop controls for authenticated/live UI state and verify the saved artifact visibly.
- For repeated same-shaped setup operations, resolve all destinations and prerequisites first,
  then execute the shortest verified loop. Do not interleave discovery and irreversible UI
  mutation one item at a time when one bounded search/read call can establish the whole plan.
- Treat every explicit output field, file path, ordering rule and preservation requirement as
  a completion checklist. Do not spend many calls collecting one item at a time when the same
  general operation can be executed and checked as a batch.
- Make edits to existing artifacts surgical. Preserve untouched text byte-for-byte, including
  whitespace and punctuation, and retain existing formatting and neighboring structure instead
  of rebuilding or normalizing content that the task did not ask to change.
- Treat the requested artifact schema as exact. Put only requested fields and content in the
  deliverable; keep diagnostic evidence, explanations and progress notes out unless requested.
- This is a real stateful machine. Re-observe after transitions, save requested work, and
  verify the final state rather than assuming an action succeeded.
- If authoritative inspection proves a required device, file, permission or account is
  absent, do not spend the remaining calls trying to fabricate that prerequisite. Leave
  the closest requested surface open, preserve the machine, and finish as infeasible.
- Verify generated or converted artifacts structurally (format plus required contents),
  not merely by checking that a path exists. For setup/configuration tasks, identify the
  smallest direct acceptance check early; once it passes, stop changing the environment.
- When the task is genuinely finished, call task_complete. If it is demonstrably impossible
  in the environment, call task_complete with infeasible=true; difficulty is not proof.`;

const SEMANTIC_DESKTOP_SYSTEM_PROMPT = `You are operating a real Ubuntu desktop to
complete a task. You interact through a live typed accessibility graph plus bounded
computer-side code execution.

How to work:
- Start with a screenshot, then call desktop_find with no filters to list the focused app,
  window/dialog and its leading controls. The result says how many controls exist; add
  name, role, state or app/window context filters to search the complete live graph.
- The screenshot also includes compact desktop_surfaces records naming the current app,
  window/dialog and document. If the task refers to an open/input file or a list "here",
  identify and read that artifact before editing or generating any output; never guess its
  contents from task wording or prior knowledge.
- Use desktop_click, desktop_hover and desktop_type only after one unique current control
  is identified. desktop_actions can combine named actions, semantic waits, key presses
  and focused text; every named target is resolved again after the previous transition.
- Treat role, name, text, value, state, advertised actions and owning app/window context
  as one record. If labels repeat, narrow by the returned role/state/context. If controls
  are still unlabeled or identical, use the exact current ref returned with the record;
  refs expire when the UI changes. Never guess or reuse private coordinates.
- Use computer_exec for bounded filesystem, archive, repository, conversion and CLI work.
  It cannot drive app UI. Use named desktop controls for visible interaction, and inspect
  the live state after transitions.
- For a necessary long install or build, use one synchronous computer_exec call with an
  adequate timeout instead of launching it in the background and spending calls polling it.
- If code changes a file currently open in an application, reload or reopen it before the
  final UI save and verify the visible content. A stale in-memory document can otherwise
  overwrite the correct disk edit.
- Use computer_python for structured data, document/spreadsheet generation or conversion;
  it runs Python 3 directly, so do not wrap it in a shell command or shebang.
- When the user names a source, derive every output record from that source or a link it
  exposes. Preserve exact per-record titles, URLs, dates and versions rather than filling
  source gaps from memory or a broader substitute search.
- Match the strategy to the task's scale. Inspect input files once, then batch public HTTP,
  parsing, conversion and structured file edits with bounded computer_exec when appropriate;
  reserve desktop calls for visible app state and final verification. Track every requested
  output field, file path, ordering rule and preservation constraint as a completion checklist.
- Make edits to existing artifacts surgical. Preserve untouched text byte-for-byte, including
  whitespace and punctuation, and retain existing formatting and neighboring structure instead
  of rebuilding or normalizing content that the task did not ask to change.
- Treat the requested artifact schema as exact. Put only requested fields and content in the
  deliverable; keep diagnostic evidence, explanations and progress notes out unless requested.
- key operates the currently focused control and accepts real key names/chords only. It is
  not a shell, URL field or literal-text tool.
- Verify generated or converted artifacts structurally (format plus required contents),
  not merely by checking that a path exists. For setup/configuration tasks, identify the
  smallest direct acceptance check early; once it passes, stop changing the environment.
- If authoritative inspection proves a required device, file, permission or account is
  absent, stop attempts that cannot create it, leave the closest requested surface open,
  and finish as infeasible rather than exhausting the budget.
- Save requested work and verify the final app/document state. When genuinely finished,
  call task_complete. If demonstrably impossible, call task_complete with infeasible=true;
  difficulty is not proof.`;

const SYSTEM_PROMPT = `You are operating a real Ubuntu 22.04 desktop (GNOME, 1920x1080) to
complete a task for the user. You interact only through the provided tools.

How to work:
- Start by taking a screenshot to see the current state of the desktop.
- After every action that should change the screen, take a screenshot and check that it
  actually did what you expected before moving on. Do not assume an action worked.
- Applications are in the dock on the left (Chrome, VS Code, VLC, LibreOffice Writer/Calc/
  Impress, GIMP, Thunderbird, Files, Terminal) and in the Activities app grid.
- This is Linux: use ctrl for shortcuts, not cmd. Applications may take several seconds to
  launch, so use the wait tool rather than clicking repeatedly.
- Prefer the specific tools (click, type_text, key, scroll, drag) and fall back to the
  python tool for anything they cannot express.
- Use computer_exec instead of typing into a terminal for filesystem, archive, repository,
  conversion and other CLI work. It cannot drive browser or desktop UI.
- Use computer_python when the work is naturally expressed as Python 3 rather than Bash.
- Coordinates are absolute pixels on the 1920x1080 screen.
- Save your work when a task involves editing a document or file. The task is graded on the
  final state of the machine, so an unsaved change scores zero.
- When the task is genuinely finished and you have verified the result on screen, call
  task_complete. If the task is impossible in this environment, call task_complete with
  infeasible set to true. Do not call it just because you are running out of ideas.`;

const VISION_ONLY_SYSTEM_PROMPT = `You are operating a real Ubuntu 22.04 desktop (GNOME,
1920x1080) to complete a task for the user. You can see the desktop only through screenshots
and act only through ordinary mouse and keyboard controls.

How to work:
- Start with a screenshot and visually locate the application, control, or content you need.
- Coordinates are absolute pixels on the 1920x1080 screen. Use click, type_text, key, scroll,
  and drag exactly as a person would. No shell, Python, DOM, accessibility tree, semantic
  element index, or filesystem tool is available. This is Linux, so use ctrl rather than cmd.
- Re-check the screenshot after every meaningful state transition. If an action did not work,
  inspect the new image and change strategy instead of repeating blind clicks.
- Applications may take several seconds to launch; use wait rather than clicking repeatedly.
- Save any edited document or file and visually verify the final state. The benchmark grades
  the machine state, not the explanation.
- When the task is genuinely finished, call task_complete. If the required state is visibly
  impossible in this environment, call task_complete with infeasible=true; difficulty alone
  is not proof of impossibility.`;

const STRICT_SEMANTIC_SYSTEM_PROMPT = `You are operating a real Ubuntu desktop through a
strictly text-only semantic computer interface. You receive no screenshots, pixels, screen
coordinates, raw keyboard, shell, browser JavaScript, or host-computer access.

How to work:
- Observe by querying system.surfaces and the compact adapter/resource index from
  system.capabilities. Filter that index by its resource field instead of opening every adapter.
- Inspect one resource schema with resource=system.capability and scope.ref set to the resource
  record's ref. If you already know an exact advertised resource name, you may instead pass it as
  parameters.resource. Resource detail is scoped to that one resource; adapter_id, name, kind,
  and ref are identities, not queryable resources. Read a kernel overflow_handle with
  resource=system.data_handle and scope.ref set to the handle. An adapter-owned
  collection_handle is instead passed to that adapter resource's advertised parameter;
  do not interchange the two handle types.
- Act only on a unique current entity ref or a semantic selector that resolves to exactly one
  target. Re-query after relevant revisions change; never guess or silently retarget stale refs.
- Use computer.run for bounded batching, pagination, filtering, transformations, and repeated
  same-shaped semantic operations. Its language has no imports, open, eval/exec, shell, f-strings,
  functions, or arbitrary attributes; call only computer.query/act/verify and documented safe
  collection/string operations.
- Verify every explicit requirement from current live state or a parsed saved artifact. Complete
  only with current passing verification receipts.
- Treat unsupported, uncertain, and representation_gap as distinct states. An uncertain mutation
  must be resolved before completion and must never be blindly replayed.
- Do not invent unavailable APIs, shell commands, selectors, keys, coordinates, or task-specific
  shortcuts. If a capability is missing, report the typed gap rather than thrashing.
- Query system.pending_state before completion so unsaved documents, pending exports/downloads,
  live/disk divergence, and uncertain actions are visible.

When the requested state is genuinely proven, call task_complete with the current passing
verification receipts. Mark infeasible only with typed evidence of the missing capability or
failed verification; difficulty or call exhaustion is not evidence.`;

const SEMANTIC_PLUS_SYSTEM_PROMPT = `You are operating a real Ubuntu desktop through a
strictly text-only computer interface. You receive no screenshots, pixels, screen coordinates,
raw keyboard, pyautogui, or harness-host access.

You have two complementary layers:
- computer.query / computer.act expose typed semantic OS, application, browser-chrome, document,
  and artifact state. computer.verify proves final state, computer.run batches operations, and
  task_complete accepts current passing verification receipts.
- web_* exposes the live webpage DOM and browser tabs directly. computer_exec and
  computer_python run bounded code inside the Ubuntu guest for filesystem, repository, archive,
  document, conversion, and computation work. They cannot automate the desktop.

How to work:
- Use the narrowest truthful layer. Prefer web_elements/web_find/web_read and web_actions for
  ordinary webpage controls; use semantic queries/actions for native UI, browser chrome, live
  application models, settings, and typed artifact state.
- Use web_search and web_read_pages for batched public research. Use web_js only inside the
  current page for compact DOM inspection or computation; it is not Node.js, a shell, or host
  JavaScript. Set expect_change=true for page mutations.
- Use computer_exec or computer_python for bounded guest-side file and data work. If code edits a
  file open in an application, reload/reconcile it before saving so stale live state cannot
  overwrite the disk result.
- Re-observe after state transitions. Numbered web elements are live-page indices and may change;
  semantic entity refs are revision-scoped. Never guess, silently retarget, or blindly replay an
  uncertain mutation.
- Verify every explicit requirement from current live state or a parsed saved artifact. Query
  system.pending_state before completion. Complete only with current passing verification
  receipts; difficulty or call exhaustion is not proof of infeasibility.
- Do not invent screenshots, coordinates, raw keys, pyautogui, host paths, evaluator state, or
  unavailable APIs. Report an actual typed capability gap instead of thrashing.`;

/**
 * pi-coding-agent's default system prompt describes the harness process's host
 * working directory. That is actively false for OSWorld: every supplied action
 * targets a remote Ubuntu guest, while host tools are disabled. Smaller models
 * copied the leaked host cwd into computer_exec and failed otherwise-valid file
 * operations. Replace, rather than append to, the coding prompt so there is one
 * authoritative execution boundary.
 */
export function executionBoundaryPreamble(
  browserOnly: boolean, visionOnly = false, strictSemantic = false,
  semanticPlus = false,
): string {
  if (semanticPlus) {
    return `You are not operating the harness host and you are not a coding-repository agent.
The only machine you can observe or control is the remote Ubuntu guest. Use the supplied typed
semantic tools for live OS/application state, the supplied web tools for webpage DOM, and only
computer_exec/computer_python for bounded guest-side code. Host paths, pixels, screenshots, raw
input devices, direct CDP ports, evaluator state, and any other execution route are unavailable.`;
  }
  if (strictSemantic) {
    return `You are not operating the harness host and you are not a coding-repository agent.
The only machine you can observe or control is the remote Ubuntu guest, through the supplied
typed semantic computer tools. Host paths, arbitrary shell/Python, pixels, raw input devices,
browser selectors, and evaluator state are unavailable. Use only advertised semantic resources.`;
  }
  if (visionOnly) {
    return `You are not operating the harness host and you are not a coding-repository agent.
The harness host filesystem and working directory are unavailable and irrelevant. The only
machine you can observe or control is the remote Ubuntu desktop through the supplied visual
mouse and keyboard tools. Do not invent shell, Python, filesystem, DOM, accessibility, or
semantic-index capabilities.`;
  }
  if (browserOnly) {
    return `You are not operating the harness host and you are not a coding-repository agent.
The harness host filesystem and working directory are unavailable and irrelevant. Use only
the supplied browser tools; never invent host paths, host commands, or unavailable tools.`;
  }
  return `You are not operating the harness host and you are not a coding-repository agent.
Every filesystem path and shell command exposed by the supplied tools refers to the remote
Ubuntu guest. Its user home is ${GUEST_HOME}. The harness host filesystem and working directory
are unavailable and irrelevant. Use only the supplied tools; never invent host paths, host
commands, or unavailable tools. Guest commands start in ${GUEST_HOME}. Interactive sudo is not
available; prefer existing CLI programs and the Python standard library, or install a necessary
pure-Python package once with pip3 --user and perform the actual work in the same bounded batch.`;
}

export function assertAuthoritativeSessionPrompt(prompt: string): void {
  const hostCwd = process.cwd().replaceAll('\\', '/');
  if (prompt.replaceAll('\\', '/').includes(hostCwd)) {
    throw new Error(`Pi leaked the harness host cwd into the model system prompt: ${hostCwd}`);
  }
  if (!prompt.includes(`Current working directory: ${GUEST_HOME}`)) {
    throw new Error('Pi session cwd is not the remote guest home');
  }
}

export async function authoritativeResourceLoader(
  systemPrompt: string,
  extensionFactories: InlineExtension[] = [],
): Promise<DefaultResourceLoader> {
  const resourceLoader = new DefaultResourceLoader({
    cwd: process.cwd(),
    agentDir: getAgentDir(),
    noExtensions: true,
    extensionFactories,
    noSkills: true,
    noPromptTemplates: true,
    noThemes: true,
    noContextFiles: true,
    systemPromptOverride: () => systemPrompt,
    appendSystemPromptOverride: () => [],
  });
  await resourceLoader.reload();
  return resourceLoader;
}

const SOM_GUIDANCE = `Screenshots are annotated with numbered boxes over the interactive
elements, and each observation lists those elements as "index / role / name / text".

- Act by element number: click_element, type_into_element, double_click_element,
  right_click_element. The numbers come from the accessibility tree, so they are exact —
  far more reliable than guessing pixel coordinates.
- The numbering is recomputed on every observation. Never reuse an index from an earlier
  screenshot; read the current element list before acting.
- If the element you need is not in the list, scroll or take a screenshot to refresh it.
  Fall back to the python tool with raw coordinates only when no element matches.`;

const WEB_GUIDANCE = `This task runs in Chrome, and you have direct browser tools.

- Treat the preloaded page, locale and account as observed context, not an eternal domain
  restriction. Follow a cross-site destination when the task asks for it or the current
  page establishes it; otherwise prefer real links/controls over guessing alternate URLs.
- web_elements lists the interactive elements on the page with an index, read from the
  live DOM. Call it before acting, and again after the page changes — indices move.
- The list is capped. If it says more elements exist, or the control you want is not in
  it, use web_find with a word from that control to search all of them.
- Elements marked "below/above fold" are off-screen but still directly clickable: they are
  scrolled into view for you. Do not waste turns scrolling just to reach a control.
- web_click and web_type act on those indices exactly. Prefer them over pixel clicking
  for anything inside the page.
- web_find searches ALL interactive elements by their text when the listing is capped.
- web_js runs JavaScript in the page. Prefer it for anything computational: resolving
  dates, setting date/form inputs directly, extracting lists of results, or checking
  state. It includes reliable ghost.inspect/find/fill/click/select/check/submit helpers;
  prefer them over hand-writing DOM event plumbing. Always return a compact object saying
  what the script found or changed, and set expect_change=true when it mutates the page.
- If a widget is not in the main document, call web_frames and run web_js in the matching
  frame. Element rows marked frame=N are inside that embedded frame.
- web_actions runs an ordered program through trusted Playwright input. Prefer semantic
  targets such as by=role + role/name, by=label, by=placeholder or by=text; these survive
  reactive re-renders better than generated CSS paths. CSS selectors remain available.
- web_tabs lists open tabs; web_switch_tab / web_close_tab manage them. Clicking a link
  can open a new tab and move you there without warning, and some tasks are graded on
  which tabs are open, so check with web_tabs if the page is not what you expect.
- web_read returns the page text. Use it to find information instead of squinting at a
  screenshot.
- web_search runs several public-web queries in one traced call; web_read_pages reads
  several selected public result URLs. Both use a temporary tab and restore the original
  task tab, so prefer them to repetitive navigate/read/close research loops.
- Web tools affect the inspected page DOM. Desktop tools affect other controls exposed by
  the current operating-system accessibility snapshot. Choose between them from observed
  state, not from a memorized workflow.
- desktop_find with no filters lists current native surfaces and actionable controls; filters
  query the complete live desktop snapshot by label, role, state and parent/window context.
  desktop_click/desktop_hover/desktop_type resolve one unique control without exposing
  private coordinates. Unlabeled duplicate records carry snapshot-scoped refs for exact
  actions; refresh rather than reusing a ref after UI changes. desktop_actions runs up to 12 ordered named actions and re-resolves
  the live snapshot after every state transition.
- Refresh after the interface changes. If a semantic query is ambiguous, use the returned
  role/state/context candidates to narrow it instead of guessing. key accepts only real
  key names or chords; it is not a shell, code runner, URL field or text-entry tool.
- Use computer_exec for bounded filesystem, archive, repository, conversion and CLI work
  inside the guest. It cannot automate visible browser/desktop UI; use the semantic tools
  for that and explicitly inspect their state after any computer-side change.
- Use computer_python for Python 3 data processing and artifact generation; do not send
  Python source to computer_exec, which is Bash.
- Verify results after crossing between page and desktop interfaces. Telemetry such as an
  unchanged frame or repeated action is evidence to inspect, not proof of failure.
- The task is graded on the final state of the machine, not on what you say. Leave the
  browser in the required state; never answer from your own knowledge instead of acting.`;

const DOM_CODE_GUIDANCE = `Use a hybrid browser-and-code approach.

- For a visible, named control already listed by web_elements/web_find, use web_click or
  web_type directly. A fresh indexed action is the shortest and most reliable path; do
  not write JavaScript merely to click a button or type into a listed field.
- Use web_js for work code is actually better at: date/arithmetic computation, compact
  DOM or open-shadow-root inspection with ghost.inspect/find, structured extraction, and
  verifying exact values. Do not spend turns rediscovering the same DOM with different
  querySelector snippets.
- For a reactive multi-step widget, use one web_actions program. Example shape:
  actions=[{op:"fill",by:"role",role:"combobox",name:"Departure",value:"Paris"},
  {op:"wait",ms:800},{op:"click",by:"role",role:"option",name:"Paris"}].
  The name identifies the target control; value is the text to enter. Semantic targets
  are re-resolved after every re-render.
- Batch related inspection into one short script and return compact visible evidence. If
  a page needs time, use an async IIFE and AWAIT ghost.wait(ms); calling it without await
  does not pause. Never write an unbounded loop.
- Once a usable control or state is known, act instead of repeating equivalent inspections.
- If an action reports no change or an error, do not repeat the same method with slightly
  different code. Re-list and use a real indexed/semantic action.
- Do not hide, remove, force-enable or rewrite page UI, dialogs, overlays or verification
  controls. Interact through the site's real controls; DOM surgery does not produce a
  valid user-visible state.
- A navigation destroys the script context. Preserve known locale/account state, but follow
  cross-site destinations when the task or observed page actually establishes them.`;

/** How many times to re-prompt a model that stops without declaring completion. */
const MAX_NUDGES = 6;

/**
 * One provider request must not occupy a benchmark worker indefinitely.
 *
 * Pi's transport already supports a request/stream timeout, but its default
 * agent-level retry policy can replay the same timed-out model turn three
 * times. A five-minute transport timeout therefore became a >20-minute
 * worker stall in the rc4 run. Keep the five-minute allowance (large local
 * model turns can legitimately be slow), but make it a hard one-attempt
 * bound. Callers can lower or raise it explicitly for a known endpoint.
 */
export const DEFAULT_PROVIDER_TURN_TIMEOUT_MS = 300_000;

export interface ProviderTurnDeadlineTelemetry {
  turnsStarted: number;
  timedOut: boolean;
  timedOutTurn: number | null;
  timedOutAfterMs: number | null;
}

export function createProviderTurnDeadlineTelemetry(): ProviderTurnDeadlineTelemetry {
  return {
    turnsStarted: 0,
    timedOut: false,
    timedOutTurn: null,
    timedOutAfterMs: null,
  };
}

/**
 * Enforce a wall-clock deadline around each provider generation.
 *
 * Transport timeouts alone are insufficient because an upstream can keep an
 * SSE connection alive forever with heartbeat or partial events. The context
 * hook runs immediately before each provider request. The first assistant
 * message_end runs before tool execution, so clearing there excludes long
 * semantic actions from the provider deadline.
 */
export function createProviderTurnDeadlineExtension(
  timeoutMs: number,
  telemetry: ProviderTurnDeadlineTelemetry,
): InlineExtension {
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs <= 0) {
    throw new Error('provider turn timeout must be a positive integer in milliseconds');
  }
  return {
    name: 'provider-turn-wall-clock-deadline',
    hidden: true,
    factory: (pi) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      let activeTurn = 0;
      const clear = (): void => {
        if (timer !== undefined) clearTimeout(timer);
        timer = undefined;
      };
      pi.on('context', (_event, ctx) => {
        clear();
        telemetry.turnsStarted += 1;
        activeTurn = telemetry.turnsStarted;
        const startedAt = Date.now();
        timer = setTimeout(() => {
          // A cleared or superseded generation cannot abort a later turn.
          if (activeTurn !== telemetry.turnsStarted) return;
          timer = undefined;
          telemetry.timedOut = true;
          telemetry.timedOutTurn = activeTurn;
          telemetry.timedOutAfterMs = Date.now() - startedAt;
          ctx.abort();
        }, timeoutMs);
      });
      pi.on('message_end', (event) => {
        if (event.message.role === 'assistant') clear();
      });
      pi.on('agent_end', clear);
    },
  };
}

export function isProviderTurnTimeout(message: string | undefined): boolean {
  const normalized = (message ?? '').trim().toLowerCase();
  return (
    normalized.includes('upstream idle timeout exceeded')
    || normalized.includes('provider request timed out')
    || normalized.includes('api request timed out')
    || normalized.includes('request timed out')
    || normalized.includes('und_err_headers_timeout')
    || normalized.includes('und_err_body_timeout')
  );
}

export function createBenchmarkSettings(
  strictSemantic: boolean,
  providerTurnTimeoutMs: number,
): SettingsManager {
  if (
    !Number.isSafeInteger(providerTurnTimeoutMs)
    || providerTurnTimeoutMs <= 0
  ) {
    throw new Error('provider turn timeout must be a positive integer in milliseconds');
  }
  return SettingsManager.inMemory({
    ...(strictSemantic ? { images: { blockImages: true } } : {}),
    // Keep Pi's normal context-window compaction enabled. Unlike the removed
    // eager semantic-simple replacement policy, this runs only under genuine
    // context pressure and is required for long episodes to remain runnable.
    // Disable both retry layers. A retry of a timed-out model turn is not
    // provably idempotent: the provider may have emitted an action that was
    // lost in transport. The episode records one typed infrastructure error
    // instead of silently asking the model again.
    retry: {
      enabled: false,
      maxRetries: 0,
      provider: {
        timeoutMs: providerTurnTimeoutMs,
        maxRetries: 0,
        maxRetryDelayMs: 0,
      },
    },
    // Pi uses this as the fetch/stream idle bound. Set it explicitly too so
    // custom providers that do not consume retry.provider.timeoutMs remain
    // bounded by the same configured deadline.
    httpIdleTimeoutMs: providerTurnTimeoutMs,
  });
}

export interface EpisodeOutcome {
  taskId: string;
  domain: string;
  instruction: string;
  model: string;
  runtime: RuntimeName;
  semanticProtocolVersion?: string;
  environmentIdentity?: Record<string, unknown>;
  score: number;
  steps: number;
  /** Executed harness tools; unknown/unavailable names do not consume this budget. */
  toolCalls: number;
  /** Every model-emitted tool attempt, including unknown names. Hard-capped separately. */
  toolAttempts: number;
  elapsedMs: number;
  stopReason: 'task_complete' | 'infeasible' | 'step_limit' | 'agent_end' | 'error';
  nudges: number;
  error?: string;
  /** Machine-readable top-level failure class for infrastructure auditing. */
  errorCode?: 'provider_turn_timeout';
  evaluationError?: string;
  cleanupError?: string;
  /** Fresh-episode retries caused only by a fatal environment transport timeout. */
  infraRetries?: number;
  /** Summed across the episode's assistant messages; 0 if the provider omits usage. */
  tokensInput: number;
  tokensOutput: number;
  tokensTotal: number;
  costUsd: number;
  semanticPolicy?: SemanticPolicyTelemetry & {
    screenshotsCaptured: number;
    visualSidecarCalls: number;
    semanticOperations: number;
  };
  /**
   * Chronological, image-free execution trace. Tool names alone are not enough
   * to distinguish a successful script from one that returned "not found".
   */
  trace: EpisodeTraceEvent[];
}

export interface EpisodeTraceEvent {
  atMs: number;
  kind: 'tool_start' | 'tool_end' | 'assistant';
  toolCallId?: string;
  toolName?: string;
  args?: unknown;
  argumentNormalization?: {
    decodedJsonPaths: string[];
    normalizedArgs: unknown;
  };
  resultText?: string;
  isError?: boolean;
  text?: string;
  stopReason?: string;
  errorMessage?: string;
}

function toolResultText(result: unknown): string {
  if (result && typeof result === 'object') {
    const record = result as {
      content?: Array<{ type?: string; text?: string }>;
      details?: unknown;
    };
    const text = (record.content ?? [])
      .filter((item) => item.type === 'text' && item.text)
      .map((item) => item.text)
      .join('\n');
    if (text) {
      const details = record.details === undefined
        ? ''
        : `\nDETAILS: ${JSON.stringify(record.details)}`;
      return `${text}${details}`.slice(0, 12_000);
    }
  }
  try {
    return JSON.stringify(result).slice(0, 12_000);
  } catch {
    return String(result).slice(0, 12_000);
  }
}

/** Task-agnostic budget checkpoints, measured in executed environment calls. */
export function budgetCheckpoint(executed: number, maximum: number): string | undefined {
  const left = maximum - executed;
  const halfway = Math.floor(maximum / 2);
  if (maximum >= 24 && left === halfway) {
    return `Half the tool-call budget remains (${left} calls). Before the next action, `
      + 'compare the observed machine state with every explicit task requirement and name '
      + 'the shortest missing state transition. Preserve what already worked. If the recent '
      + 'approach produced no concrete progress, change strategy now. Batch related network, '
      + 'filesystem, conversion, or structured-data work into one bounded computer_exec call '
      + 'instead of repeating equivalent inspections or navigations. For setup/configuration '
      + 'work, run the smallest direct acceptance check now if its prerequisites are present; '
      + 'if it passes, stop changing the environment and finish. If authoritative inspection '
      + 'proved a required prerequisite is absent, stop attempts that cannot create it and '
      + 'leave the closest useful state open.';
  }
  if (left === 10) {
    return `${left} tool calls remain. If the current approach has not produced concrete `
      + 'progress, repeating it is unlikely to help. Switch interface or batch the remaining '
      + 'work, then leave the machine in the best complete state you can verify.';
  }
  if (left === 5) {
    return `${left} tool calls remain. Stop exploratory work. Perform only the shortest `
      + 'remaining state changes, save any requested artifact, verify it, and finish.';
  }
  if (left === 2) {
    return `Only ${left} tool calls remain. If a direct final check just passed, stop `
      + 'mutating the machine: optional deeper tests can break a valid state, so call '
      + 'task_complete now. If the requested state is not present, make only the single '
      + 'most useful final action. The actual machine state, not the completion claim, '
      + 'is graded.';
  }
  return undefined;
}

export function isInfrastructureToolTimeout(resultText: string, isError = false): boolean {
  const normalized = resultText.toLowerCase();
  return (
    normalized.includes('the operation was aborted due to timeout')
    || normalized.includes('aborterror: the operation was aborted')
    || (isError && normalized.includes('env server') && normalized.includes('timed out'))
  );
}

function appendPendingNotice<T>(result: T, notices: Notices): T {
  const notice = notices.pending;
  if (!notice || !result || typeof result !== 'object') return result;
  const record = result as {
    content?: Array<{ type: string; text?: string; [key: string]: unknown }>;
  };
  if (!Array.isArray(record.content)) return result;
  notices.pending = undefined;
  return {
    ...result,
    content: [...record.content, { type: 'text', text: notice }],
  };
}

export async function runOsworldEpisode(input: {
  baseUrl: string;
  taskPath: string;
  provider: string;
  modelId: string;
  /** Explicit runtime selector. Old flags remain only for frozen v15 reproduction. */
  runtime?: RuntimeName;
  thinkingLevel?: 'off' | 'low' | 'medium' | 'high';
  maxToolCalls?: number;
  /** Hard bound for one provider turn. Provider/agent retries are disabled. */
  providerTurnTimeoutMs?: number;
  /** Set-of-Marks: annotated screenshots + element-index actions. */
  som?: boolean;
  semanticDesktop?: boolean;
  /** Baseline arm: screenshot plus raw mouse/keyboard tools, with no semantic or code tools. */
  visionOnly?: boolean;
  /** CDP browser tools: live-DOM element lists and in-page actions. */
  web?: boolean;
  webTextOnly?: boolean;
  webFirst?: boolean;
  verifyGate?: boolean;
  browserPrompt?: boolean;
  codeFirst?: boolean;
  budgetHints?: boolean;
  noDesktop?: boolean;
  compactWeb?: boolean;
  /** Extra operating guidance appended to the system prompt (variant-specific). */
  extraGuidance?: string;
  apiKeys?: Record<string, string>;
  modelEndpoint?: {
    baseUrl: string;
    apiKey?: string;
    contextWindow: number;
    maxTokens: number;
    input: Array<'text' | 'image'>;
    thinkingFormat?: LocalThinkingFormat;
  };
  onEvent?: (line: string) => void;
}): Promise<EpisodeOutcome> {
  const startedAt = Date.now();
  const maxToolCalls = input.maxToolCalls ?? 60;
  const providerTurnTimeoutMs = input.providerTurnTimeoutMs
    ?? DEFAULT_PROVIDER_TURN_TIMEOUT_MS;
  const runtime: RuntimeName = input.runtime
    ?? (input.visionOnly ? 'vision-v15' : 'hybrid-v15');
  if (runtime === 'semantic-visual-v1') {
    throw new Error('semantic-visual-v1 is not enabled during strict semantic-v1 scoring');
  }
  const strictSemantic = runtime === 'semantic-v1';
  const semanticPlus = runtime === 'semantic-plus-v1';
  const semanticSimple = runtime === 'semantic-simple-v1';
  const semanticRuntime = strictSemantic || semanticPlus || semanticSimple;
  const legacyFlags = [
    input.som, input.semanticDesktop, input.visionOnly, input.web, input.webTextOnly,
    input.webFirst, input.verifyGate, input.browserPrompt, input.codeFirst, input.budgetHints,
    input.noDesktop, input.compactWeb,
  ];
  if (semanticRuntime && legacyFlags.some(Boolean)) {
    throw new Error(`${runtime} rejects all legacy screenshot, browser, desktop, and v15 flags`);
  }
  if (semanticRuntime && input.modelEndpoint?.input.some((kind) => kind !== 'text')) {
    throw new Error(`${runtime} requires model endpoint input=["text"]`);
  }
  if (semanticSimple && input.extraGuidance?.trim()) {
    throw new Error(
      'semantic-simple-v1 uses its frozen versioned prompt and rejects extra guidance',
    );
  }
  const semanticPolicy = createSemanticPolicyTelemetry();
  const providerDeadline = createProviderTurnDeadlineTelemetry();
  // Invalid tool names previously consumed the same 40-call allowance as real
  // environment actions. Preserve the 40 executed-call contract while still
  // bounding hallucinated names so a broken model cannot run forever.
  const maxToolAttempts = maxToolCalls + Math.max(10, Math.ceil(maxToolCalls / 2));
  const log = input.onEvent ?? (() => {});

  const created = await fetch(`${input.baseUrl}/episodes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      task_path: input.taskPath,
      som: input.som ?? false,
      semantic_only: input.semanticDesktop ?? false,
      web: input.web ?? false,
      runtime,
      require_screenshot: !semanticRuntime,
      max_tool_calls: maxToolCalls,
      initial_observation: false,
    }),
    // A cold real OSWorld VM can take minutes to construct, but it must not be
    // allowed to occupy a worker forever.
    signal: AbortSignal.timeout(900_000),
  });
  if (!created.ok) {
    throw new Error(`env server refused episode: ${created.status} ${await created.text()}`);
  }
  const episode = await created.json() as {
    episode_id: string;
    instruction: string;
    task_id: string;
    domain: string;
    browser_clock?: {
      text?: string;
      iso?: string;
      timeZone?: string;
      offsetMinutes?: number;
    };
    environment_identity?: Record<string, unknown>;
    semantic_guest_bundle_hash?: string;
    semantic_guest_version?: string;
  };
  if (semanticRuntime) {
    let identityError: string | undefined;
    if (!episode.semantic_guest_bundle_hash || !episode.semantic_guest_version) {
      identityError = `${runtime} requires a versioned semantic guest handshake`;
    }
    const provider = String(episode.environment_identity?.outer_provider ?? '').toLowerCase();
    const guestId = String(
      episode.environment_identity?.nested_guest_machine_id ?? '',
    ).toLowerCase();
    const guestPlatform = String(
      episode.environment_identity?.guest_platform ?? '',
    ).toLowerCase();
    if (
      ['mac', 'macos', 'darwin', 'local_mac'].includes(provider)
      || guestId === 'local-mac'
      || ['mac', 'macos', 'darwin'].includes(guestPlatform)
    ) {
      identityError = `${runtime} refused an environment identified as the local Mac`;
    }
    if (!identityError && guestPlatform !== 'linux') {
      identityError = `${runtime} requires a Linux semantic guest, got ${guestPlatform || 'unknown'}`;
    }
    if (identityError) {
      await fetch(`${input.baseUrl}/episodes/${episode.episode_id}`, {
        method: 'DELETE', signal: AbortSignal.timeout(180_000),
      }).catch(() => undefined);
      throw new Error(identityError);
    }
  }
  log(`episode ${episode.episode_id} [${episode.domain}] ${episode.instruction}`);

  const client: EnvClient = { episodeId: episode.episode_id, baseUrl: input.baseUrl };
  const notices: Notices = {};
  let semanticOperations = 0;
  const semanticTransport = semanticToolTransport(input.provider, input.modelId);
  // A vision-only arm is the unscaffolded screenshot/mouse/keyboard control.
  // Fail closed if a caller accidentally combines it with the full harness's
  // budget or completion interventions: unavailable-tool advice is not a fair
  // base-model baseline, even though it cannot grant additional capabilities.
  const verifyGate = Boolean(input.verifyGate && !input.visionOnly);
  const budgetHints = Boolean(input.budgetHints && !input.visionOnly);
  const selectedTools = strictSemantic
    ? createSemanticRuntimeTools(
      client,
      (count) => { semanticOperations += count; },
      semanticTransport,
    )
    : semanticPlus
    ? createSemanticPlusRuntimeTools(
      client,
      (count) => { semanticOperations += count; },
      semanticTransport,
    )
    : semanticSimple
    ? createSemanticSimpleRuntimeTools(client)
    : input.web
    ? createWebTools(client, input.webTextOnly ?? false, input.webFirst ?? false,
      verifyGate, input.noDesktop ?? false,
      input.compactWeb ?? false, input.som ?? false, notices)
    : input.som ? (
      input.semanticDesktop
        ? createSemanticDesktopTools(client, verifyGate)
        : createSomTools(client, verifyGate)
    )
      : createComputerTools(client, verifyGate);
  const visionToolNames = new Set([
    'screenshot', 'click', 'type_text', 'key', 'scroll', 'drag', 'wait', 'task_complete',
  ]);
  const exposedTools = input.visionOnly
    ? selectedTools.filter((tool) => visionToolNames.has(tool.name))
    : selectedTools;
  if (strictSemantic) {
    const names = exposedTools.map((tool) => tool.name);
    const expected = SEMANTIC_TOOL_DEFINITIONS.map(
      (definition) => transportSemanticToolName(definition.name, semanticTransport),
    );
    if (JSON.stringify(names) !== JSON.stringify(expected)) {
      throw new Error(`semantic-v1 must expose exactly five tools: ${JSON.stringify(names)}`);
    }
  }
  if (semanticPlus) {
    const names = exposedTools.map((tool) => tool.name);
    const expected = semanticPlusExpectedToolNames(semanticTransport);
    if (JSON.stringify(names) !== JSON.stringify(expected)) {
      throw new Error(`semantic-plus-v1 tool surface drift: ${JSON.stringify(names)}`);
    }
  }
  if (semanticSimple) {
    const names = exposedTools.map((tool) => tool.name);
    if (JSON.stringify(names) !== JSON.stringify(SEMANTIC_SIMPLE_TOOL_NAMES)) {
      throw new Error(`semantic-simple-v1 tool surface drift: ${JSON.stringify(names)}`);
    }
  }
  let environmentCalls = 0;
  // Computer/browser actions are stateful and order-dependent. Pi otherwise
  // executes multiple tool calls from one assistant message in parallel,
  // letting "fill, click, inspect" race on the same page. Force the emitted
  // order just as a human or Playwright script would.
  const tools = exposedTools.map((tool) => {
    const execute = tool.execute;
    return {
      ...tool,
      executionMode: 'sequential' as const,
      execute: async (...args: Parameters<typeof execute>) => {
        environmentCalls += 1;
        if (budgetHints) {
          const notice = budgetCheckpoint(environmentCalls, maxToolCalls);
          if (notice) notices.pending = notice;
        }
        const result = await execute(...args);
        // Web tools drain notices while composing their result. Desktop and
        // computer_exec tools do not, so append any still-pending checkpoint
        // here. This makes budget awareness consistent across every surface.
        return appendPendingNotice(result, notices);
      },
    };
  });

  log(`  tools: ${(tools as { name: string }[]).map((t) => t.name).join(', ')}`);

  const modelRuntime = await ModelRuntime.create();
  if (input.modelEndpoint) {
    registerOpenAICompatibleEndpoint(modelRuntime, {
      provider: input.provider,
      modelId: input.modelId,
      ...input.modelEndpoint,
    });
  }
  for (const [provider, key] of Object.entries(input.apiKeys ?? {})) {
    if (key) modelRuntime.setRuntimeApiKey(provider, key);
  }
  // getModel lives on ModelRuntime (pi-ai does not export it).
  const model = modelRuntime.getModel(input.provider, input.modelId);
  if (!model) {
    const available = modelRuntime.getModels(input.provider)
      .map((entry) => entry.id).slice(0, 12).join(', ');
    throw new Error(
      `unknown model ${input.provider}/${input.modelId}. Available: ${available || '(none)'}`,
    );
  }
  const advertisedModelInputs = (model as { input?: Array<'text' | 'image'> }).input ?? [];
  if (semanticRuntime && !advertisedModelInputs.includes('text')) {
    throw new Error(
      `${runtime} model endpoint does not support text input: ${JSON.stringify(advertisedModelInputs)}`,
    );
  }
  // Hosted frontier models often advertise both text and image capability.
  // Strict semantic sessions create a text-only view of that same model so Pi
  // cannot serialize image parts even though the provider could accept them.
  // The provider-payload hook remains the final fail-closed boundary.
  const sessionModel = semanticRuntime
    ? { ...model, input: ['text'] as Array<'text'> }
    : model;

  let toolAttempts = 0;
  let nudges = 0;
  let usageInput = 0;
  let usageOutput = 0;
  let usageTotal = 0;
  let usageCost = 0;
  let stopReason: EpisodeOutcome['stopReason'] = 'agent_end';
  let error: string | undefined;
  let errorCode: EpisodeOutcome['errorCode'];
  let infrastructureFailure: string | undefined;
  let evaluationError: string | undefined;
  let cleanupError: string | undefined;
  const trace: EpisodeTraceEvent[] = [];

  try {
    const now = new Date();
    const clock = episode.browser_clock;
    const dateLine = clock?.text
      ? `Current date and time in the task's browser: ${clock.text}`
        + `${clock.timeZone ? ` (time zone: ${clock.timeZone})` : ''}. `
        + 'Use this browser clock to resolve relative dates like "next Monday" or '
        + '"week after next".'
      : `Current date and time reference: ${now.toString()}. `
        + 'Use this to resolve relative dates like "next Monday" or "week after next".';
    const browserOnly = Boolean(input.web && input.noDesktop);
    const baseSystemPrompt = strictSemantic
      ? STRICT_SEMANTIC_SYSTEM_PROMPT
      : semanticPlus
      ? SEMANTIC_PLUS_SYSTEM_PROMPT
      : semanticSimple
      ? ''
      : input.visionOnly
      ? VISION_ONLY_SYSTEM_PROMPT
      : input.semanticDesktop
      ? SEMANTIC_DESKTOP_SYSTEM_PROMPT
      : input.web && !input.noDesktop
      ? HYBRID_SYSTEM_PROMPT
      : input.browserPrompt ? BROWSER_SYSTEM_PROMPT : SYSTEM_PROMPT;
    const systemPrompt = semanticSimple
      ? buildSemanticSimpleSystemPrompt(dateLine)
      : [
        executionBoundaryPreamble(
          browserOnly, input.visionOnly, strictSemantic, semanticPlus,
        ),
        baseSystemPrompt,
        dateLine,
        input.som && !input.web && !input.semanticDesktop ? SOM_GUIDANCE : '',
        input.web ? WEB_GUIDANCE : '',
        input.codeFirst ? DOM_CODE_GUIDANCE : '',
        input.extraGuidance ?? '',
      ].filter(Boolean).join('\n\n');
    const policyExtensions = [
      createProviderTurnDeadlineExtension(providerTurnTimeoutMs, providerDeadline),
      ...(semanticRuntime ? [createSemanticPolicyExtension(semanticPolicy)] : []),
    ];
    const resourceLoader = await authoritativeResourceLoader(systemPrompt, policyExtensions);
    const settingsManager = createBenchmarkSettings(
      semanticRuntime, providerTurnTimeoutMs,
    );
    const { session } = await createAgentSession({
      // Pi unconditionally appends its session cwd even when a custom system
      // prompt replaces the coding prompt. Make that appended value truthful:
      // it names the remote guest home rather than the harness host checkout.
      cwd: GUEST_HOME,
      model: sessionModel,
      thinkingLevel: input.thinkingLevel ?? 'medium',
      // Only the OSWorld computer tools — no filesystem/bash tools on the host,
      // which would let the agent cheat by editing files outside the VM.
      noTools: 'all',
      customTools: tools,
      tools: tools.map((tool) => tool.name),
      resourceLoader,
      settingsManager,
      sessionManager: SessionManager.inMemory(GUEST_HOME),
      modelRuntime,
    });
    assertAuthoritativeSessionPrompt(session.systemPrompt);

    // Capture what the model actually says. An episode that ends without a
    // tool call is undiagnosable otherwise: we can see that it stopped, not
    // that it stopped to ask a question, refuse, or report a broken page.
    // agent_end carries the turn's messages; stopReason/errorMessage on the
    // assistant message is where a refusal or API failure shows up.
    session.subscribe((event) => {
      const anyEvent = event as {
        type: string;
        toolCallId?: string;
        toolName?: string;
        args?: unknown;
        result?: unknown;
        isError?: boolean;
        willRetry?: boolean;
        messages?: Array<{
          role?: string;
          stopReason?: string;
          errorMessage?: string;
          content?: Array<{ type?: string; text?: string }>;
        }>;
      };
      if (anyEvent.type === 'agent_end') {
        // Token accounting. The whole point of the text-only arm is context
        // cost, and wall-clock is a confounded proxy for it (network variance,
        // provider queueing). Sum usage across the turn's assistant messages so
        // arms can be compared on tokens and dollars directly.
        for (const m of anyEvent.messages ?? []) {
          const u = (m as { usage?: {
            input?: number; output?: number; totalTokens?: number;
            cost?: { total?: number };
          } }).usage;
          if (m.role !== 'assistant' || !u) continue;
          usageInput += u.input ?? 0;
          usageOutput += u.output ?? 0;
          usageTotal += u.totalTokens ?? 0;
          usageCost += u.cost?.total ?? 0;
        }
        const last = [...(anyEvent.messages ?? [])].reverse()
          .find((m) => m.role === 'assistant');
        if (last) {
          const said = (last.content ?? [])
            .filter((c) => c.type === 'text' && c.text)
            .map((c) => c.text)
            .join(' ')
            .replace(/\s+/g, ' ')
            .trim();
          const meta = [
            last.stopReason ? `stop=${last.stopReason}` : '',
            last.errorMessage ? `err=${last.errorMessage.slice(0, 200)}` : '',
            anyEvent.willRetry ? 'willRetry' : '',
          ].filter(Boolean).join(' ');
          if (meta) log(`  turn: ${meta}`);
          if (said) log(`  said: ${said.slice(0, 500)}`);
          else log('  said: (no text)');
          trace.push({
            atMs: Date.now() - startedAt,
            kind: 'assistant',
            text: said.slice(0, 4_000),
            ...(last.stopReason ? { stopReason: last.stopReason } : {}),
            ...(last.errorMessage
              ? { errorMessage: last.errorMessage.slice(0, 1_000) }
              : {}),
          });
          if (
            last.stopReason === 'error'
            && isProviderTurnTimeout(last.errorMessage)
          ) {
            infrastructureFailure = (
              `provider turn timeout after ${providerTurnTimeoutMs}ms: `
              + (last.errorMessage ?? 'provider request timed out')
            );
            error = infrastructureFailure;
            errorCode = 'provider_turn_timeout';
            stopReason = 'error';
            log(`  ${infrastructureFailure}; provider and agent retries are disabled`);
            void session.abort();
          }
        }
        if (providerDeadline.timedOut && errorCode !== 'provider_turn_timeout') {
          infrastructureFailure = (
            `provider turn timeout after ${providerTurnTimeoutMs}ms: `
            + `wall-clock deadline exceeded on provider turn `
            + `${providerDeadline.timedOutTurn ?? 'unknown'}`
          );
          error = infrastructureFailure;
          errorCode = 'provider_turn_timeout';
          stopReason = 'error';
          log(`  ${infrastructureFailure}; provider and agent retries are disabled`);
        }
        return;
      }
      const typed = semanticRuntime && anyEvent.toolName
        ? { ...anyEvent, toolName: canonicalSemanticToolName(anyEvent.toolName) }
        : anyEvent;
      if (typed.type === 'tool_execution_end') {
        const resultText = toolResultText(typed.result);
        trace.push({
          atMs: Date.now() - startedAt,
          kind: 'tool_end',
          ...(typed.toolCallId ? { toolCallId: typed.toolCallId } : {}),
          ...(typed.toolName ? { toolName: typed.toolName } : {}),
          resultText,
          isError: typed.isError ?? false,
        });
        log(
          `  result ${typed.toolName ?? ''}: `
          + resultText.replace(/\s+/g, ' ').slice(0, 500),
        );
        if (isInfrastructureToolTimeout(resultText, typed.isError ?? false)) {
          infrastructureFailure = (
            `infrastructure tool timeout after ${typed.toolName ?? 'unknown tool'}; `
            + 'the episode worker is no longer trustworthy'
          );
          error = infrastructureFailure;
          stopReason = 'error';
          log(`  ${infrastructureFailure}`);
          void session.abort();
          return;
        }
        if (typed.toolName === 'task_complete') {
          const details = (typed.result as {
            details?: { terminal?: boolean; infeasible?: boolean };
          } | undefined)?.details;
          if (details?.terminal) {
            stopReason = details.infeasible ? 'infeasible' : 'task_complete';
            // The completion tool has now executed and recorded DONE/FAIL.
            // Abort only AFTER its result, so the environment mutation cannot
            // be cancelled at tool_execution_start.
            void session.abort();
            return;
          }
        }
        // Let the nominal last action execute, then stop. The previous check
        // lived on tool_execution_start and silently cancelled action N, so a
        // 40-call budget actually delivered only 39 environment actions.
        if (
          (environmentCalls >= maxToolCalls || toolAttempts >= maxToolAttempts)
          && stopReason === 'agent_end'
        ) {
          stopReason = 'step_limit';
          void session.abort();
        }
        return;
      }
      if (typed.type !== 'tool_execution_start') return;
      toolAttempts += 1;
      // Include a short arg digest: tool-name streaks alone cannot distinguish
      // "three clicks on three different things" from "the same click three times".
      const argsRaw = (typed as { args?: unknown }).args;
      const toolDefinition = semanticRuntime
        ? SEMANTIC_TOOL_DEFINITIONS.find((definition) => definition.name === typed.toolName)
        : undefined;
      const normalization = toolDefinition && argsRaw !== undefined
        ? normalizeModelArguments(argsRaw, toolDefinition.inputSchema).evidence
        : null;
      let argSummary = '';
      try {
        if (argsRaw !== undefined) argSummary = ` ${JSON.stringify(argsRaw).slice(0, 90)}`;
      } catch { argSummary = ''; }
      log(
        `  tool attempt ${toolAttempts} (executed ${environmentCalls}/${maxToolCalls}): `
        + `${typed.toolName}${argSummary}`,
      );
      trace.push({
        atMs: Date.now() - startedAt,
        kind: 'tool_start',
        ...(typed.toolCallId ? { toolCallId: typed.toolCallId } : {}),
        ...(typed.toolName ? { toolName: typed.toolName } : {}),
        ...(argsRaw === undefined ? {} : { args: argsRaw }),
        ...(normalization ? {
          argumentNormalization: {
            decodedJsonPaths: [...normalization.decoded_json_paths],
            normalizedArgs: normalization.normalized_args,
          },
        } : {}),
      });
    });

    await session.prompt(`TASK: ${episode.instruction}`);

    // A turn that produces no tool call ends pi's loop. Weaker models do that
    // constantly — they narrate a plan instead of acting — which would end
    // their episode at a fraction of the budget while Opus runs to completion.
    // OSWorld's own convention is to keep stepping until the agent declares
    // DONE/FAIL or the budget runs out, so nudge and continue.
    while (
      !semanticSimple
      &&
      stopReason === 'agent_end'
      && environmentCalls < maxToolCalls
      && toolAttempts < maxToolAttempts
      && nudges < MAX_NUDGES
    ) {
      nudges += 1;
      log(
        `  nudge ${nudges} (stopped at ${environmentCalls} executed calls, `
        + `${toolAttempts} attempts without task_complete)`,
      );
      await session.prompt(semanticRuntime
        ? 'You stopped without calling task_complete. Query system.pending_state and re-query '
          + 'the current surface. Complete only with current passing verification receipts; '
          + 'otherwise continue the unfinished work through the advertised semantic capabilities.'
        : 'You stopped without calling task_complete, and the task is not recorded as '
          + 'finished. Take a screenshot to see the current state. If the task is genuinely '
          + 'complete, call task_complete now. If it is not, keep working on it.');
    }
    if (nudges >= MAX_NUDGES && stopReason === 'agent_end') {
      log(`  gave up after ${nudges} nudges`);
    }
  } catch (sessionError) {
    stopReason = 'error';
    error = sessionError instanceof Error ? sessionError.message : String(sessionError);
    log(`  session error: ${error}`);
  }

  let semanticServerState: {
    screenshots_captured: number;
    semantic_operations: number;
    image_parts_created: number;
    image_parts_in_session: number;
    image_parts_sent: number;
    pixels_sent_to_policy_model: number;
    visual_sidecar_calls: number;
    guest_bundle_hash?: string;
  } | undefined;
  if (semanticRuntime) {
    try {
      const stateResponse = await fetch(
        `${input.baseUrl}/episodes/${episode.episode_id}/semantic/state`,
        { signal: AbortSignal.timeout(30_000) },
      );
      if (!stateResponse.ok) {
        throw new Error(`HTTP ${stateResponse.status}: ${(await stateResponse.text()).slice(0, 500)}`);
      }
      semanticServerState = await stateResponse.json() as typeof semanticServerState;
      const strictCounters = {
        screenshots_captured: semanticServerState?.screenshots_captured ?? -1,
        image_parts_created: semanticServerState?.image_parts_created ?? -1,
        image_parts_in_session: semanticServerState?.image_parts_in_session ?? -1,
        image_parts_sent: semanticServerState?.image_parts_sent ?? -1,
        pixels_sent_to_policy_model: semanticServerState?.pixels_sent_to_policy_model ?? -1,
        visual_sidecar_calls: semanticServerState?.visual_sidecar_calls ?? -1,
      };
      if (Object.values(strictCounters).some((value) => value !== 0)) {
        throw new Error(
          `policy_violation: strict server counters are nonzero: ${JSON.stringify(strictCounters)}`,
        );
      }
    } catch (stateError) {
      const message = stateError instanceof Error ? stateError.message : String(stateError);
      error = `semantic state validation failed: ${message}`;
      stopReason = 'error';
      infrastructureFailure = error;
      log(`  ${error}`);
    }
  }

  // OSWorld grades the VM's final state, independent of what the agent claims.
  // A transport timeout is different: the episode's pinned worker may still be
  // executing the timed-out call, so grading through that same queue can hang
  // for another five minutes and cannot establish a valid score. The CLI will
  // retry this task once from a fresh episode instead.
  let score = 0;
  let steps = 0;
  if (!infrastructureFailure) {
    try {
      const evaluated = await fetch(
        `${input.baseUrl}/episodes/${episode.episode_id}/evaluate`,
        { method: 'POST', signal: AbortSignal.timeout(300_000) },
      );
      if (!evaluated.ok) {
        throw new Error(`HTTP ${evaluated.status}: ${(await evaluated.text()).slice(0, 500)}`);
      }
      const payload = await evaluated.json() as {
        score?: number; steps?: number; error?: string;
      };
      score = payload.score ?? 0;
      steps = payload.steps ?? 0;
      if (payload.error) {
        evaluationError = payload.error;
        log(`  evaluate failed: ${payload.error}`);
      }
    } catch (evaluationFailure) {
      const message = evaluationFailure instanceof Error
        ? evaluationFailure.message : String(evaluationFailure);
      log(`  evaluate failed: ${message}`);
      // A grader transport/runtime failure is not evidence that the model scored
      // zero. Preserve it as an invalid-run condition in the result artifact.
      evaluationError = message;
    }
  } else {
    log('  skipped evaluation because the episode worker timed out');
  }
  try {
    const closed = await fetch(`${input.baseUrl}/episodes/${episode.episode_id}`, {
      method: 'DELETE',
      signal: AbortSignal.timeout(infrastructureFailure ? 15_000 : 180_000),
    });
    if (!closed.ok) {
      throw new Error(`HTTP ${closed.status}: ${(await closed.text()).slice(0, 500)}`);
    }
    const payload = await closed.json() as { errors?: string[] };
    if (payload.errors?.length) cleanupError = payload.errors.join('; ');
  } catch (closeError) {
    cleanupError = closeError instanceof Error ? closeError.message : String(closeError);
  }
  if (cleanupError) log(`  cleanup failed: ${cleanupError}`);

  if (semanticRuntime) assertZeroImageTelemetry(semanticPolicy);

  return {
    taskId: episode.task_id,
    domain: episode.domain,
    instruction: episode.instruction,
    model: `${input.provider}/${input.modelId}`,
    runtime,
    ...(semanticRuntime ? { semanticProtocolVersion: SEMANTIC_PROTOCOL_VERSION } : {}),
    ...(episode.environment_identity ? { environmentIdentity: episode.environment_identity } : {}),
    score,
    steps,
    toolCalls: environmentCalls,
    toolAttempts,
    elapsedMs: Date.now() - startedAt,
    stopReason,
    nudges,
    tokensInput: usageInput,
    tokensOutput: usageOutput,
    tokensTotal: usageTotal,
    costUsd: Number(usageCost.toFixed(6)),
    ...(semanticRuntime ? {
      semanticPolicy: {
        ...semanticPolicy,
        screenshotsCaptured: semanticServerState?.screenshots_captured ?? -1,
        visualSidecarCalls: semanticServerState?.visual_sidecar_calls ?? -1,
        semanticOperations: semanticServerState?.semantic_operations ?? semanticOperations,
      },
    } : {}),
    trace,
    ...(error ? { error } : {}),
    ...(errorCode ? { errorCode } : {}),
    ...(evaluationError ? { evaluationError } : {}),
    ...(cleanupError ? { cleanupError } : {}),
  };
}
