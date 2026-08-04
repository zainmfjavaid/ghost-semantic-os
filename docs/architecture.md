# Architecture

```text
text-only policy model
        |
        | read_computer / computer_click / computer_type
        v
Pi harness + zero-image provider guard
        |
        v
semantic-simple facade (surface letters + element numbers)
        |
        v
semantic kernel (refs, revisions, typed errors, receipts)
        |
        +-- browser/CDP and Chrome profile routes
        +-- live AT-SPI and GNOME routes
        +-- LibreOffice UNO and artifact parsers
        +-- app bridges: Thunderbird, VS Code, VLC, PDF, GIMP, media
        +-- bounded public research and process routes
        |
        v
versioned guest agent inside an isolated OSWorld desktop
        |
        v
OSWorld evaluator, invoked only after the policy loop
```

## Model-facing contract

`read_computer` lists all current application/window surfaces and the active
surface's compact scene. A surface has a stable letter for the episode; an
element combines that letter with a number. Every element line describes its
role, accessible name or content, relevant state, and advertised action.

`computer_click(A)` activates a surface. `computer_click(A12)` resolves the
current semantic capability represented by `A12`, activates its surface if
needed, and executes its truthful native route. `computer_type(A4, text)`
enters literal text using the element's declared replace, insert, send, or grid
paste behavior. Stale and ambiguous IDs fail rather than retargeting.

Every click or type result includes a fresh concise scene, so a low-context
model can observe the consequence immediately. Earlier results remain in the
conversation. Large page text and container contents are queryable and
paginated rather than dumped into every observation.

## Execution routes

The kernel prefers, in order:

1. Native protocol or application APIs.
2. A versioned guest bridge.
3. Accessibility Action, Text, Value, or Selection interfaces.
4. A private semantic input fallback whose target is uniquely resolved and
   hit-tested. Bounds are never returned to the model.

The trace records the real execution path. A direct Chrome preference write or
UNO edit is not described as a click.

## Isolation

The outer environment server creates one nested OSWorld desktop per episode.
The model cannot access host paths, KVM, Docker, display sockets, CDP ports,
AT-SPI object paths, evaluator sources, task gold state, or arbitrary Python.
The evaluator is a post-episode operation and its result is not fed back into
the policy loop.

## Upstream boundary

The repository includes a compressed Apache-licensed snapshot of the exact
OSWorld base tree because upstream no longer advertises the frozen commit. The
bootstrap script verifies that archive and tree before applying the six audited
patches in `patches/osworld/`. The expected final Git tree is verified
independently of clone path and commit metadata.
