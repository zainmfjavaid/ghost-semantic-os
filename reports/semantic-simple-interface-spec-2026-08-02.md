# Semantic Simple v1 — locked model-facing interface

## Public tools

The policy model receives exactly three tools:

```text
read_computer(query?, within?, cursor?)
computer_click(element)
computer_type(element, text)
```

There is no completion, verification, code, browser-specific, desktop-specific, screenshot,
coordinate, keyboard, shell, JavaScript, adapter, resource, receipt, or evaluator tool.

The model stops naturally. The system prompt tells it to read the computer after the final
mutation and stop only after that read shows the requested state. OSWorld evaluates the final VM
after the model loop ends.

## Surface and element identity

- Top-level interactive surfaces receive episode-stable letter IDs: `A` through `Z`, then `AA`,
  `AB`, and so on.
- Separate application windows and modal dialogs receive separate surface IDs.
- Browser tabs are elements inside a Chrome-window surface, not separate surfaces.
- Elements receive a surface-qualified number: `A1`, `A2`, `B1`, `B10`, and so on.
- Surface IDs and element IDs are never reassigned to different native identities during an
  episode.
- A stable native identity retains its public ID across reads.
- A replaced or disappeared native identity makes its old ID stale. It never retargets by ordinal
  position, label similarity, or current visual order.
- `computer_click("B")` activates surface `B`.
- `computer_click("B10")` activates the exact current element `B10`.
- IDs are capabilities, never pixels or accessibility object paths.

## `read_computer`

Default output contains:

1. Every current top-level surface, with application/window/document/dialog identity and active,
   modal, modified, or busy state when meaningful.
2. A cleaned hierarchy for the active surface. The active-surface heading always repeats the
   complete application/window/document title and meaningful state; it never says only
   `Active surface [A]` and forces the model to look up the letter elsewhere.
3. Meaningful text, interactive controls, values, non-default states, and click/type affordances.
4. Explicit truncation and a continuation cursor when the bounded response is incomplete.

Optional arguments:

- `query`: case-insensitive search over role, name, text, value, description, and context within
  the selected or active surface.
- `within`: a container ID on the active surface whose semantic descendants should be returned.
- `cursor`: an opaque continuation from the prior compatible read.

The compiler omits geometry, screen coordinates, native object paths, empty containers, duplicate
name/text fields, default false state, hidden implementation nodes, adapter IDs, revisions, and
action schemas. The authoritative trace retains internal provenance.

Example:

```text
COMPUTER

Surfaces
[A] Thunderbird — Bills — active
[B] Chrome — AWS Billing
[C] LibreOffice Calc — expenses.xlsx — modified

Active Surface [A] — Thunderbird — Bills — active
[A1] tree "Folders"
  [A2] treeitem "Inbox" click
  [A3] treeitem "Bills" selected click
[A4] list "Messages"
  [A5] item "AWS Invoice — August 2026" selected click
[A6] document "Message body"
  [A7] text "Your AWS invoice is ready."
  [A8] link "Billing & Cost Management Page" click
```

## `computer_click`

The kernel resolves the current capability and chooses the truthful best route: browser semantic
invocation, Chrome API, application bridge, accessibility action, or guarded private semantic
input. It never guesses among targets, silently retargets a stale ID, or claims a physical click
when a direct API executed.

Clicking a checkbox/switch/toggle invokes its normal activation semantics. Clicking a menu or
combobox exposes its resulting options in the refreshed output. Offscreen targets are privately
scrolled into view when required.

The response includes a concise causal delta, actual execution path in trace metadata, and a fresh
compiled computer view.

When an action changes surfaces, the response names both sides rather than returning bare IDs:

```text
Active surface changed:
[A] Thunderbird — Bills
→ [B] Chrome — AWS Billing
```

## `computer_type`

The kernel focuses and edits the exact target through its native editable interface. The compiled
element advertises the target behavior:

- `type=replace` for scalar inputs, cells, and value fields.
- `type=insert` for documents/editors at their current semantic insertion point.
- `type=send` for terminal/session input.

Tab/newline-separated text sent to a spreadsheet cell or selected range is a bulk rectangular
paste. No per-cell model loop is required.

The response includes a concise causal delta and fresh compiled view.

## Multi-application behavior

Every read and action result includes the surface index. A model changes applications by clicking
the target surface letter. An action that opens a window, dialog, or application assigns it a new
surface ID and reports the active-surface transition immediately. Cross-app data movement remains
ordinary model context plus typing; there is no workflow DSL.

The initial runtime lists every surface but returns element details only for the active surface.
Reading an inactive surface without activating it is deliberately deferred. If focus churn or
cross-surface comparison later proves expensive, add an explicit inactive-surface read as a
measured optimization rather than complicating v1 preemptively.

## Deliberate gaps

Strict v1 has no drag, hover, raw keys, freehand canvas, arbitrary code, or vision. A task that
cannot be expressed with semantic read/click/type returns an explicit representation gap in the
computer view. Add another public primitive only after a generalized task class proves it is
necessary.
