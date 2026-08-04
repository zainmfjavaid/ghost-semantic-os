# Semantic Simple application/surface matrix — 2026-08-02

## Question

Can the current `read_computer` / `computer_click` / `computer_type` facade truthfully and
compactly represent every Linux OSWorld application family, using stable lettered surfaces and
surface-qualified element IDs, without making the model rediscover adapter protocols?

## Short answer

Not yet. The ID and active-surface presentation are directionally right, but the current facade is
an **AT-SPI + active-browser-AX compiler**, not a facade over the whole semantic kernel. It reads
only `ui.elements`, `os.windows`, `os.applications`, `browser.tabs`, and `browser.elements`
(`envserver/semantic/simple_facade.py:540-544`). It does not read or route through LibreOffice UNO,
Chrome-chrome, Thunderbird, VS Code, VLC/MPRIS, PDF/Poppler, the terminal worker, GIMP, Picard, or
the media parser.

That boundary matters. Several of those adapters expose more accurate, smaller records than the
full accessibility tree, and several actions cannot be truthfully reduced to a generic
`invoke`/`set_text`. The facade is currently useful for basic semantic navigation and ordinary web
forms, but it is not ready for broad app testing.

## What is real today

### Identity and presentation that are already sound

- Surface letters and per-surface element numbers are retained for the episode and never rebound
  (`simple_facade.py:183-200`).
- The surface label includes application, title, modal/modified/busy/active state, and the active
  heading repeats the full label (`simple_facade.py:87-104`, `618-625`). This satisfies the
  requested `Active Surface [A] — Thunderbird — Bills — active` shape.
- AT-SPI records have a real cross-query native identity. The guest hashes the private D-Bus proxy
  identity and reuses its entity ref (`guest_agent/semantic_agent.py:121-143`); the facade then
  keys a surface by `adapter_id:native_ref` independent of resource
  (`simple_facade.py:140-147`). This is how a window from `ui.elements` can match the same window
  from `os.windows`.
- Browser AX elements have stable episode-local refs derived from page/frame/backend DOM node, and
  their records contain role, name, value, description, states, ignored flag, parent/children, and
  actions (`envserver/semantic/browser_adapter.py:420-480`).

### The universal AT-SPI record shape

All native UI currently reaches the facade through this one shape:

```text
ref, kind="ui.element", role, name, description, text,
value={current,minimum,maximum,increment}|null,
state={active,busy,checked,editable,enabled,expanded,focusable,focused,
       invalid,multiselectable,pressed,read_only,required,selected,showing,visible},
advertised_actions=[native AT-SPI action names],
parent_ref, child_refs, child_count
```

The record is built at `guest_agent/semantic_agent.py:1033-1092`. A full `ui.elements` read walks
every application up to depth 32 and 5,000 records. The facade separately performs two more
shallow AT-SPI walks for windows and applications (`semantic_agent.py:1693-1716`), then performs
the two browser reads.

## Application matrix

“Should route” below describes the correct internal implementation while preserving exactly the
three public operations. It is not what the current facade does unless explicitly stated.

| App/family | Likely surface identity | Real semantic records available | What the simple elements should look like | How click/type should route | Volume and duplicate risks | Explicit gaps / current status |
|---|---|---|---|---|---|---|
| **Chrome — webpage** | One AT-SPI Chrome frame/window, titled from the active `browser.tab`; tabs remain elements inside that surface. A second Chrome window must be a separate surface. | `browser.tab`: `index,url,title,active,actions` (`browser_adapter.py:302-325`). `browser.element`: `role,name,value,description,states,ignored,frame_url,frame_name,parent_ref,child_refs,actions` (`420-480`). AT-SPI simultaneously exposes Chrome toolbar and frequently the page accessibility tree. | Tabs with title and active state; page headings/text/links/buttons/inputs; Chrome toolbar/address bar only from AT-SPI. Default read should strongly prefer interactive nodes plus compact readable text, with static-detail expansion via `query`/`within`. | Tab click -> `browser.tabs.switch_tab`; DOM button/link -> `browser.elements.invoke`; checkbox -> `toggle`; textbox -> `set_text`. Toolbar and browser-native dialogs -> AT-SPI. | Full AX trees include generic/static nodes and every node advertises `scroll_into_view`. AT-SPI page nodes duplicate CDP AX nodes. Multiple tabs and frames can be large. Current dedupe is too weak across sources and too aggressive within one source. | Current tab rows lose `title` and `url`, so every tab renders as `browser.tab click`. HTML combobox/listbox exposes `select_option`, which the facade maps to neither click nor type. Multiple Chrome windows are collapsed onto the first Chrome surface. |
| **Chrome — chrome/profile/settings/bookmarks/history/extensions/downloads** | Same Chrome window surface when a visible `chrome://` page is active; persistent profile state is not itself a visible surface. | `chrome.bookmark`, `chrome.setting`, `chrome.history_entry`, `chrome.extension`, `chrome.download`, plus profile/internal-page records. Resources/actions are declared at `chrome_adapter.py:28-38`; representative bookmark fields are at `207-230`, and the dispatcher is at `601-641`. | Visible chrome-page controls should still be DOM/AT-SPI elements. Persistent objects may need concise virtual rows only when the active chrome page or a query asks for them: bookmark title/URL, setting key/value, extension name/enabled, download filename/state. | Ordinary visible controls -> browser/AT-SPI. Persistent mutations -> exact `chrome.semantic@1` action, not page clicking, after mapping a virtual click/type target to its typed arguments. | Bookmark/history/settings stores can be thousands of records. Showing them by default would recreate the token problem. AT-SPI + browser AX + native store records can describe the same visible item three times. | Current facade never observes `chrome.*`, so reliable bookmark/history/extension/settings operations and save-PDF/create-shortcut routes disappear. Many typed Chrome actions need more than empty click arguments or `{value:text}` and require virtual action metadata. |
| **GNOME shell, Settings, Files, ordinary dialogs** | Each showing AT-SPI frame/window/dialog/alert is a surface. App name comes from the application ancestor and title from the frame/dialog. Modal dialogs should supersede their owner only when actually active/modal. | Universal AT-SPI record above. `guest-os@1` also exposes settings, clipboard, desktop entries, audio/display/network/power/session/package state, but the facade does not read it (`semantic_agent.py:1501-1537`). | Menus, navigation rows, switches, radio/check controls, editable entries, dialog message, confirmation buttons. Omit shell implementation windows and blank panels. | Visible UI -> AT-SPI direct action or guarded semantic input. Stable OS setting/value changes can use `guest-os@1` internally when a visible virtual control is bound to the exact setting. | GNOME shell contributes blank/implementation windows. Settings pages contain repeated labels and panel wrappers. Current full-tree read scans other apps even though it renders only the active one. | Any showing `dialog`/`alert` is treated as modal and selected ahead of focused windows, even if it is a nonmodal utility. If no window reports active/focused, the first showing surface (often a blank GNOME shell window) wins. Current surface activation is likely unreliable for windows whose AT-SPI action list is empty. |
| **Native file chooser** | A chooser dialog must be its own modal surface, owned by the launching app. GTK may report role `dialog`, `file chooser`, or `file chooser dialog`. | AT-SPI tree plus a dedicated `os.file_choosers` resource and `choose_path(path)` action. Chooser recognition and path traversal are implemented at `semantic_agent.py:1659-1673` and `3690-3794`. | Current folder/path, file/folder rows, filename entry, cancel/open/save/select buttons. Prefer the dedicated path state over dumping the GTK implementation tree. | File/folder/button click -> AT-SPI. Exact path choice -> dedicated `choose_path`, provided the facade has a truthful way to bind typed path text to that action. | GTK replaces most of the subtree while navigating, so element refs churn normally. The full tree is large and full of repeated file rows/icons. | Current facade never queries `os.file_choosers`; role `file chooser` is not a surface role; `computer_type` cannot invoke `choose_path`. Paths outside guest home and choosing home itself are explicit bridge gaps (`semantic_agent.py:3724-3743`). |
| **LibreOffice Writer** | AT-SPI Writer frame/window joined to the matching UNO `document.writer` session by title/URL/window ownership. Surface title should be document title; modified/read-only state should come from UNO. | Document: `document_type,title,url,modified,actions` (`semantic_agent.py:221-242`). Paragraphs: `index,text,style,alignment,actions` (`379-405`). Runs: paragraph/run index, text, font/style/bold/italic/color (`410-439`). Tables and styles have their own compact records (`444-490`). | Menus/toolbars via AT-SPI; document body as compact paragraphs/headings/runs only as needed; selected text and insertion context; tables as bounded containers/cells. Avoid emitting the same text from document root, paragraph, line, and glyph descendants. | Button/menu clicks -> AT-SPI. Body type must choose UNO `insert_text`, `replace_text`, `replace_with_paragraphs`, or table-cell action based on the selected virtual target and advertised behavior. Save/undo/redo -> UNO document action. | Writer AT-SPI trees are commonly thousands of nodes, with text repeated at document/paragraph/run levels. UNO runs can also be numerous, but are queryable by exact resource and can be summarized. | Current facade ignores UNO entirely. It labels every role `document` as `type=insert` but calls AT-SPI `set_text`, which replaces contents rather than inserting. Structured formatting actions cannot be represented by the current generic click/type mapping. |
| **LibreOffice Calc** | AT-SPI Calc frame joined to UNO `document.calc`; one surface per workbook window. Sheet tabs and ranges are elements, not surfaces. | Sheets: `name,index,active,visible,actions` (`semantic_agent.py:493-515`). Cells: `sheet,column,row,address,value,display,formula,type,number_format,style,background_color,actions` (`553-586`). Ranges carry rectangular `data`/`formulas` (`589-602`); charts and selection/frozen-pane resources also exist. | Workbook state; sheet tabs; a bounded used/visible range with address + displayed value/formula; current selection; named tables/charts. `query="A37"` should find a cell without dumping the sheet. | Sheet click -> activate/select via AT-SPI or UNO controller binding. Cell type -> UNO `set_text`, `set_value`, or `set_formula` chosen from input. Tab/newline rectangular input -> `set_range_values`/`set_range_formulas`, not per-cell loops. | A full sheet can exceed 5,000 cells; AT-SPI repeats row/column headers and empty cells. Current same-semantics dedupe can delete repeated empty cells. The UI walk still scans the entire office tree before query filtering. | Current facade does not observe any spreadsheet resource. Its advertised rectangular-paste behavior has no implementation: it sends one AT-SPI `set_text` with `{value:text}`. Empty/default cells and formulas are therefore neither compactly readable nor reliably editable. |
| **LibreOffice Impress** | AT-SPI Impress frame joined to UNO `document.impress`; slide pane items and canvas objects are elements inside the document surface. Slide show windows, if interactive, are separate surfaces. | Slides: `index,name,layout,shape_count,actions`; shapes: `slide_index,shape_index,name,shape_type,text,position,size,actions` (`semantic_agent.py:781-815`); notes: `slide_index,text` (`818-845`). | Bounded slide list; active slide; text-bearing shapes; notes; menu/toolbar controls. Do not dump every canvas implementation object by default. | Slide/toolbar navigation -> AT-SPI. Shape text -> UNO `replace_text`; notes -> `add_text_shape`; structural click targets may bind to create/delete slide/shape actions only when the action and target are explicit. | Slides can contain many shapes, and AT-SPI may duplicate shape text in canvas/document descendants. Geometry is useful provenance but should not be model-visible by default. | Current facade ignores UNO and has no shape/slide identity. Visual layout/similarity remains an explicit semantic gap. Separate adapter defect: `presentation.styles` rewrites records to `presentation.style` and then filters for `presentation.shape`, so it always returns empty (`semantic_agent.py:848-856`, `935-937`). |
| **Thunderbird** | AT-SPI Thunderbird frame; mail folder/message/composer tabs are elements. Message compose or standalone message windows may be separate frame surfaces; modal account/file dialogs are separate dialog surfaces. | The declared bridge model has accounts, folders, messages, threads, search results, drafts, composer, attachments, tags, filters, settings and typed actions (`thunderbird_adapter.py:20-30`). No installed guest bridge produces those records: the native dispatcher does not register Thunderbird (`native_app_bridges.py:1628-1637`). AT-SPI is therefore the only real live shape. | Folder tree, message list with sender/subject/date/read/selected, message body text/links, composer recipient/subject/body fields and attachment rows. | Folder/message/link/button -> AT-SPI. Composer scalar fields -> AT-SPI editable text initially. Reliable search/open/move/tag/send should route to the MailExtension bridge once installed, with send retaining idempotency and external-action safeguards. | Mail trees and message bodies are large and heavily nested; subject/body text is often duplicated across table cells, document text, and descendants. Repeated blank action cells are common. | Native integration is absent, so search/mailbox semantics and safe send cannot be guaranteed. The facade silently suppresses representation-gap/timeout observations rather than telling the model what is missing. |
| **VS Code** | One surface per VS Code frame/window/workspace. Editor tabs, Explorer, search, problems, terminals, and dialogs are elements. | Declared records cover workspaces, files, editors, buffers, selections, symbols, diagnostics, search results, settings, extensions, terminals, tasks, save state; actions are typed edits/rename/save/command/task/terminal/extension mutations (`vscode_adapter.py:20-30`). No VS Code guest bridge is registered (`native_app_bridges.py:1628-1637`), so only AT-SPI is real today. | Workspace/title, editor tabs, active file, bounded buffer/selection, Explorer/search/diagnostics rows, status bar and terminal summary. Monaco should not be emitted as thousands of line fragments. | Navigation controls -> AT-SPI. Editor typing should route to extension buffer edits with a live buffer hash; save/rename/task actions should use the extension. Terminal input requires a distinct terminal-session route. | Monaco may expose the visible buffer plus repeated line numbers/text and can change on cursor blink/selection. Explorer and command-palette items repeat labels. | Native extension integration is absent. Generic AT-SPI `setTextContents` can replace an editor buffer without the required optimistic-concurrency hash and is not an adequate substitute. Rendered extension webviews remain an explicit gap. |
| **VLC / generic media player** | VLC AT-SPI window surface; current media/playback is state inside it. A headless MPRIS player has no visible surface unless represented as an explicitly named virtual player surface. | `media.player`: identity, playback status, position/duration, volume, loop, shuffle, title/artists/album/url, capability flags, actions (`native_app_bridges.py:488-518`). Playlist entries: position/title/artists/url (`542-571`). | Now-playing title/artist, play state, time/duration, volume, loop/shuffle, playlist rows, ordinary UI buttons. | Visible play/pause/stop/next controls -> AT-SPI or direct MPRIS action. Volume/seek editable virtual fields -> parse text and call `set_volume`/`seek`. Playlist row click may activate; removal must bind explicitly to remove. | AT-SPI exposes unlabeled icon buttons and sliders; native player record is much smaller and more reliable. Playlist may be long. VLC and generic MPRIS adapters can duplicate the same player. | Current facade ignores both MPRIS adapters. If their entity records were naively appended, any nonempty action list would become generic `invoke`, which the adapter does not support. Audio/subtitle tracks, preferences, equalizer, reorder and several other declared VLC operations are explicit MPRIS gaps (`native_app_bridges.py:572-575`, `638-642`). |
| **PDF / Evince** | Evince AT-SPI window titled by PDF filename; document parser state joins that surface by active path/title. | Document: `name,path,page_count,size,sha256` (`native_app_bridges.py:811-826`). Per-page text: `page,text` (`895-906`). Forms: `name,value,field_type`; links/annotations: `page,subtype,uri,contents` (`828-865`). | Filename/page count/current page, bounded text by page, outline if available, links and form fields, toolbar controls. A query should target page/text without rendering the entire PDF. | Toolbar navigation -> AT-SPI. Form type -> `fill_form_field`. Open/save/export -> PDF adapter when a virtual target has the necessary path. Link click only when the adapter or visible AT-SPI link has a truthful follow route. | Whole-document text can be huge; text should page by PDF page and query. Evince may expose the same page text through AT-SPI, duplicating Poppler. | Current facade ignores PDF records. `go_to_page`, `follow_link`, annotation, and print are declared but the native bridge explicitly reports gaps; logical sections and live selection are also gaps (`native_app_bridges.py:916-920`, `979-980`). Visual similarity/freehand ink remain out of scope. |
| **Terminal** | Visible terminal emulator frame as a surface. The sandbox process manager/session is not the same thing and should not masquerade as the visible terminal unless the product explicitly creates that session. | Session manager/session records contain name/cwd/created/last run/actions; process/output/status records contain argv/cwd/exit/duration/stdout/stderr/hashes/truncation (`native_app_bridges.py:1500-1531`, `1575-1582`). | Prompt/session title, bounded recent output, current working directory, running/completed state. Never dump unbounded scrollback. | If operating a visible terminal, type must use a truthful terminal input path that can submit text/newline. If operating an owned sandbox session, a command requires a parsed/explicit argv+cwd contract; `send_stdin` is not available because the worker is not an interactive PTY. | Terminal scrollback is high volume and changes rapidly. AT-SPI often exposes the terminal as one enormous text node. Process stdout is already bounded to head+tail with a truncation marker. | Current facade labels role `terminal` as `type=send` but executes AT-SPI `set_text`, which calls `EditableText.setTextContents`; that is not terminal submission. The native worker is ignored and explicitly rejects interactive stdin (`native_app_bridges.py:1598-1600`). |
| **GIMP** | GIMP main frame plus any independent image windows/dialogs. Tool docks should remain containers/elements unless they are true top-level windows. | The outer descriptor declares images, canvas, layers, channels, paths, selections, guides, text layers, undo, exports, filters and typed actions (`gimp_adapter.py:21-32`). No GIMP bridge is registered in the guest dispatcher (`native_app_bridges.py:1628-1637`), so there are no real PDB records today; only AT-SPI menus/toolbox/layer widgets. | Active image name/modified state, layer list/visibility/opacity, text layers, selection/path summaries, menus/tools. Canvas pixels must not appear as fake semantics. | Menu/button/list operations -> AT-SPI. Layer/text/filter/export mutations should route to a real GIMP PDB plugin once installed. | Tool docks contain many repeated icon buttons, blank panels, sliders, and labels. Canvas descendants provide little semantic value. Multiple image/dialog windows complicate active-surface selection. | Native integration is absent. Visual composition and freehand painting are explicit gaps (`gimp_adapter.py:66-72`). The three primitives cannot express drag/brush/path geometry without a future generalized capability. |
| **Picard / deterministic media** | Picard frame as a surface. A media artifact parser is data associated with a selected file, not a top-level UI surface. | Picard live bridge is an explicit gap (`native_app_bridges.py:1334-1349`). Media parser records include file name/path/size/MIME/hash/actions (`1169-1179`), stream `fields`, metadata/EXIF, OCR `text`, palette, and histogram (`1194-1237`). | Picard file/cluster/album/track tree, selected tags as editable name/value fields, save state. For media files, show concise metadata only after selection/query; OCR/palette/histogram must be opt-in. | Picard rows/fields -> AT-SPI until plugin exists; reliable tag/cluster/lookup/save -> future Picard bridge. Media metadata edits/conversion/resize/crop/export require typed native arguments and cannot be generic click/type without virtual parameter fields. | Tag tables repeat values across file/track/album nodes. Histograms are 768 bins and must never be default output. OCR can be long. AT-SPI and parser may duplicate filenames/metadata. | Picard plugin is not installed. Current facade ignores the deterministic media adapter too. Visual judgment is an explicit gap even when OCR/palette/histogram are available. |

## Concrete facade bugs and failure modes

### P0 — false or dangerous affordances

1. **The facade advertises `type=insert` but performs replacement.** `_action_shape` always sets
   `type_action="set_text"`; it merely labels a `document` as `insert`
   (`simple_facade.py:374-382`). `type_text` then sends that action with `{value:text}`
   (`734-748`). The AT-SPI implementation of `set_text` calls `setTextContents`, replacing the
   target (`semantic_agent.py:3806-3812`). A Writer document can therefore be presented as an
   insertion target while the action replaces all accessible text.

2. **Terminal `type=send` is fictional.** Role `terminal` gets the same `set_text` action and only a
   different display label (`simple_facade.py:376-382`). There is no submit/newline or owned PTY
   route. The real terminal bridge says `send_stdin` is unavailable
   (`native_app_bridges.py:1598-1600`).

3. **Read-only/static text can be advertised as editable.** `_TYPE_ROLES` contains the generic
   AT-SPI role `text`, and the compiler does not exclude `read_only` or require the editable-text
   interface (`simple_facade.py:36-39`, `374-378`). Clicking such a false affordance fails only
   after the model spends a call.

4. **Specialized records cannot be appended naively.** For every non-browser record, any nonempty
   action list becomes generic `invoke` (`simple_facade.py:371-372`). A VLC player advertising
   `play,pause,seek,...`, a Writer paragraph advertising `replace_text`, or a media artifact
   advertising `resize` would therefore become one `invoke` target even though none of those
   adapters supports `invoke`. `computer_click` also always supplies empty arguments
   (`691-717`), and `computer_type` always supplies only `value` (`743-748`). A truthful facade needs
   per-resource/action virtual elements and argument translation.

### P0 — missing application coverage

5. **The compiler bypasses nearly every native adapter.** The five fixed observations at
   `simple_facade.py:540-544` are the complete read source. Consequently modified document state,
   Calc cells/formulas, Writer paragraphs, Impress shapes, MPRIS playback, PDF pages/forms,
   terminal output, Chrome profile objects, and media metadata cannot appear and their actions
   cannot be selected.

6. **Thunderbird, VS Code, and GIMP have descriptors but no guest implementations.** The default
   native dispatcher registers only VLC/MPRIS, PDF, terminal, Picard-gap, and media
   (`native_app_bridges.py:1628-1637`). These three families necessarily fall back to AT-SPI.

7. **Adapter failure is hidden as an empty computer.** `_try_observe` swallows unknown,
   unsupported, not-found, unavailable, timeout, and representation-gap errors and returns `[]`
   (`simple_facade.py:168-181`). The model cannot distinguish “no elements” from “the only truthful
   adapter timed out/is missing.” This contradicts the spec's explicit representation-gap rule.
   `budget_exhausted` is not swallowed, so a >5,000-node UI instead fails the entire read.

### P1 — duplicate loss and browser routing

8. **Deduplication can remove legitimate same-looking siblings.** The semantic key contains only
   surface, role, name, text, value, and boolean click/type affordances; it omits native identity,
   source, parent, sibling index, and description (`simple_facade.py:459-472`). Two “Edit” buttons,
   blank cells, repeated list items, or identical paragraphs on the same surface collapse into one.
   Deduplication should target cross-source equivalence, never same-source siblings by label alone.

9. **The implementation does the reverse of its browser-preference comment.** AT-SPI candidates
   are appended first, browser tabs/elements second (`simple_facade.py:416-434`), and the first
   semantic key wins (`459-472`). When an AT-SPI page control exactly matches a browser AX control,
   the browser record is discarded and clicking routes through AT-SPI rather than CDP.

10. **Browser tabs are indistinguishable.** Browser tabs store `title`, `url`, and `active`
    (`browser_adapter.py:313-324`), but the facade extracts only role/kind, name, text,
    description, and value (`simple_facade.py:444-448`). A tab therefore renders as only
    `[A#] browser.tab click`.

11. **Browser comboboxes/listboxes are dead.** Browser AX assigns `select_option` to those roles
    (`browser_adapter.py:442-443`), but `_action_shape` recognizes only `invoke` or toggle actions
    for browser elements (`simple_facade.py:366-370`). Neither click nor type is emitted.

12. **Multiple Chrome windows are not represented correctly.** The compiler finds only the first
    surface whose app/title contains Chrome/Chromium and attaches every browser tab and the active
    page AX tree to it (`simple_facade.py:304-328`, `411-434`). Browser tab records have a per-page
    `surface_id`, but no Chrome-window ownership; the facade ignores that field.

### P1 — token/latency and hierarchy correctness

13. **A compact response is backed by an expensive full-computer scan.** `read()` makes five
    sequential observations. The guest proxy overrides the requested limit to 100 per private page
    and assembles the entire collection up to 5,000 records before returning
    (`runtime.py:443-534`). `ui.elements` itself is a depth-32 walk of every app, while windows and
    applications trigger two more independent walks. Query filtering happens only after all of
    this. This controls model tokens but not latency, churn, or transport work.

14. **Every AT-SPI element prints irrelevant false state.** The guest materializes every known
    AT-SPI state as true or false on every record (`semantic_agent.py:969-1001`). The renderer prints
    `checked=false` and `expanded=false` whenever false (`simple_facade.py:508-516`). Thus buttons,
    labels, text, documents, rows, and panels all waste tokens on two inapplicable state strings,
    contrary to the default-false omission requirement.

15. **Character truncation can silently lose elements without a usable continuation.** Pagination
    is computed by element count first, then the final rendered text is sliced at 10,000 characters
    (`simple_facade.py:600-647`). If fewer than `limit` elements contain long text, `next_cursor` is
    null even though the character slice removed some rendered elements. The slice can also cut an
    element line or quoted value in half, while `returned_elements` still reports the unsliced page
    size.

16. **`within` and indentation break across omitted containers and page boundaries.** Parent
    traversal uses only compiled meaningful elements for `within` (`simple_facade.py:556-572`) and
    only the current page for indentation (`625-637`). An omitted low-signal panel between a root
    and descendant severs containment; a parent on the prior page makes its children look
    top-level.

17. **Text normalization destroys useful structure.** `_clean_text` collapses every whitespace run
    (`simple_facade.py:55-59`). That erases line breaks in documents, code, messages, PDF text, and
    terminal output, and tabs/newlines in spreadsheet-shaped content. Compactness should preserve
    bounded structural newlines rather than flattening all app content into one sentence.

### P1 — surface selection and activation

18. **Every showing dialog/alert is forced modal and takes focus.** The surface compiler sets
    `modal` purely from the role, then selects the last modal before considering active/focused
    state (`simple_facade.py:264-275`, `330-338`). Nonmodal utility dialogs can hijack the active
    surface.

19. **Surface activation is not guaranteed by the actual AT-SPI shape.** An observed OSWorld
    window can have no advertised actions. Yet clicking a surface always calls
    `activate_window` (`simple_facade.py:691-703`). The guest's action implementation requires a
    unique matching direct Action; unlike `invoke`, `activate_window` has no guarded semantic-input
    fallback when the action interface is absent (`semantic_agent.py:3844-3865`). Cross-app surface
    switching will therefore fail for such windows.

20. **Modified/busy/title truth is incomplete.** AT-SPI state collection does not include a
    modified flag (`semantic_agent.py:975-992`), and the facade never joins the UNO document record
    that does. For non-browser apps, modified is therefore normally false even when the document is
    dirty. Similar title quality depends entirely on AT-SPI application/window naming.

## Recommended implementation order before any model run

1. **Fix the compiler invariants first:** relevant-only state rendering; line-safe character
   pagination; source-aware dedupe; tab title/URL; combobox routing; truthful type behaviors;
   explicit observation gaps.
2. **Build one shared surface snapshot:** perform one AT-SPI walk, derive applications/windows/
   dialogs/file choosers from it, and scope the full-depth walk to the active surface. Preserve the
   current “all surface labels, active elements only” public contract.
3. **Add an internal surface join layer:** AT-SPI window identity remains the surface, while UNO,
   Chrome, MPRIS, PDF, terminal, and other adapter records attach to it by app/process/title/path/
   document ownership. Non-visual artifact adapters stay query-only children of an active file/app,
   not fake windows.
4. **Use virtual semantic elements with exact action bindings:** one entity may expose several
   click/type targets. Each target must retain `{resource,ref,action,argument mapping,behavior}`.
   Do not infer `invoke` from “has actions.”
5. **Implement app compilers in risk order:** Chrome page + chrome toolbar, GNOME/dialog/file
   chooser, Writer, Calc, Impress, Thunderbird, VS Code, VLC, PDF, Terminal, GIMP, Picard/media.
   Writer/Calc/Impress should use UNO before broad AT-SPI text; VLC should use MPRIS state; PDF
   should use page-bounded Poppler text; terminal must not claim send until a real input route
   exists.
6. **Install or explicitly gate missing bridges:** Thunderbird, VS Code, and GIMP are not merely
   facade work. Their guest integrations do not exist in the default dispatcher. Picard is an
   intentional live-model gap.
7. **Then run model-free real-app trajectories:** measure full read latency, raw record counts,
   rendered characters/tokens, duplicate ratios, stable-ID retention, click/type success, and
   post-action refresh for each app/state. Only after those pass should an agent see the tools.

## Minimum model-free scenario set

- Chrome: simple article; dense app; long form; native `<select>`; iframe; 10+ tabs; two Chrome
  windows; download; `chrome://settings`; permission/file-picker dialog.
- GNOME: Settings list/detail; Files with repeated rows; modal confirm; nonmodal dialog; native open
  and save choosers; two applications with one unfocusable window.
- Writer: short document; 50-page document; table; selection; dirty state; insert vs replace;
  toolbar formatting; save dialog.
- Calc: sparse sheet; dense 5,000-cell edge; formulas; repeated blank cells; multi-sheet; rectangular
  paste; chart; active selection.
- Impress: 1 slide; 50 slides; many repeated text boxes; notes; slideshow window; modal export.
- Thunderbird: folder tree; 1,000-message inbox; HTML message with link; search; composer; attachment
  chooser; send confirmation/idempotency.
- VS Code: small file; 10k-line file; multi-root Explorer; search results; diagnostics; command
  palette; integrated terminal; unsaved buffer.
- VLC: stopped/playing/paused; no media; long playlist; unlabeled toolbar; volume/seek; subtitle menu.
- PDF/Evince: text PDF; 200-page PDF; scanned/no-text PDF; form; links; password/error dialog.
- Terminal: empty prompt; long scrollback; running command; multiline input; full-screen TUI gap.
- GIMP: one/multiple images; layers; text layer; export dialog; tool dock; explicit canvas gap.
- Picard/media: many tracks/tags; duplicate titles; no plugin; metadata/EXIF/OCR/palette/histogram
  query bounds.

## Confidence

**High** on the current routing boundary and the listed code defects: they follow the complete
facade read/action path, the actual AT-SPI/browser/UNO/native record builders, and the default guest
bridge registry. **Medium** on exact per-app AT-SPI role names and tree volume because those vary by
application build, accessibility mode, document, and current desktop state; they must be confirmed
by the model-free live scenario pass above.

## Open gaps

- No focused test currently exercises `SimpleComputerFacade` with real or fixture AT-SPI/browser
  records. The existing semantic tests cover the kernel/adapters, not this compiler.
- This audit did not call a model, mutate GCP, or run an agent. It also did not claim live UI success.
  Real-app trajectory validation is the next distinct gate.
- A three-operation public surface can remain viable, but only if the internal element capability
  is richer than the public ID: it must bind the exact adapter, entity, action, arguments, type
  behavior, and surface ownership. The current `_Element` stores only one generic click action and
  one generic type action, which is insufficient for the real adapter shapes above.
