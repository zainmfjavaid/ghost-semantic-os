# semantic-simple-v1 real-VM trajectory pack

This pack is a model-free breadth gate for the three-operation computer facade.
Every fixture points at an existing OSWorld task JSON, so OSWorld alone owns VM
setup. The canary runner creates a clean `semantic-simple-v1` episode, replays
the listed operations, audits the exact model-visible text, evaluates the final
VM state, and cleans up.

No fixture calls a model, shell, semantic-kernel endpoint, adapter, resource,
selector, coordinate, key, or evaluator during the trajectory. The only
trajectory operations are `read`, `click`, and `type`. The pack is deliberately
model-free: public actions are resolved uniquely from the immediately preceding
model-visible render rather than invented from source inspection. Fixture
steps use only the public tool schema: `read` may carry `query`, `within`, or
`cursor`; `click` carries a prior-render match; and `type` carries a
prior-render match plus text. In particular, fixtures never use the HTTP-only
`limit` argument.

The action canaries cover a Chrome destination field and Search button, Writer
paragraph replacement and document-end insertion, Calc scalar/formula/rectangular input, VLC play and
volume, a real GTK dialog and file chooser, and exact Calc → Writer → Files
surface switching. Writer insertion uses a stable `Document end` capability
backed by native UNO paragraph insertion.

Each fixture carries the same article gate:

- 1–10,000 exact rendered characters per call;
- no duplicated non-empty rendered line;
- no adapter/resource/ref/revision/receipt/handle/kernel jargon;
- all screenshot/image/pixel/visual-sidecar counters exactly zero;
- the intended app in the exact active-surface header and at least one useful
  element on the unfiltered read;
- estimated text budget at most 2,500 tokens per call.

The current runner enforces these limits globally and preserves every exact
render in the human-review bundle. Fixture constraints are repeated as data so
the intended acceptance boundary remains explicit if runner defaults change.

## Chosen OSWorld setups

| Fixture | Surface | Existing task setup |
|---|---|---|
| `01-chrome-webpage.json` | Chrome webpage | `chrome/0d8b7de3…` opens drugs.com |
| `02-chrome-form.json` | Web form/text fields | `chrome/f79439ad…` opens Ryanair flight search |
| `03-chrome-select.json` | Web combobox/select | `chrome/1704f00f…` opens Rentalcars |
| `04-chrome-iframe.json` | Frame-rich web app | `multi_apps/dd60633f…` opens a Google Colab notebook |
| `05-chrome-settings.json` | Chrome settings/chrome surface | `chrome/030eeff7…` launches Chrome for Do Not Track |
| `06-gnome-settings.json` | GNOME settings state | `os/a4d98375…` seeds the auto-lock setting |
| `07-gnome-dialog.json` | Real GTK question dialog | Generic setup launches `zenity --question`; public read resolves and clicks Continue |
| `08-gnome-file-chooser.json` | Real GTK file chooser | Generic setup stages one file and launches `zenity --file-selection`; public read resolves the exact-path input and types the staged path |
| `09-writer.json` | LibreOffice Writer | Opens a DOCX; replaces one uniquely rendered heading, then appends and verifies three paragraphs through the stable document-end insertion point |
| `10-calc.json` | LibreOffice Calc | Opens an XLSX; types scalar, formula, and 2×2 rectangular values through one queried cell ID |
| `11-impress.json` | LibreOffice Impress | `libreoffice_impress/04578141…` opens a PPTX |
| `12-thunderbird.json` | Thunderbird | `thunderbird/3f49d2cc…` loads a profile and launches mail |
| `13-vscode.json` | VS Code | `vs_code/0ed39f63…` opens a text buffer |
| `14-vlc.json` | VLC | Stages media, clicks the rendered Play capability, and types the rendered volume capability |
| `15-pdf-evince.json` | PDF / Evince | `multi_apps/5df7b33a…` opens a book PDF |
| `16-terminal.json` | GNOME Terminal | `multi_apps/f7dfbef3…` opens a maximized terminal |
| `17-gimp.json` | GIMP | `multi_apps/e8172110…` opens a PNG in GIMP |
| `18-multi-app.json` | Writer + Calc + Files | Opens all three and activates exact rendered surface rows in Calc → Writer → Files order |
| `19-public-cross-app-e2e.json` | Chrome + Writer + Desktop + Settings + chooser | Staged local page covers DOM type/click/result, exact surface switches, installed Settings launch, and a real Chrome file chooser |

The two GTK fixtures and cross-app journey are synthetic conformance tasks
rather than scored OSWorld examples. They are intentionally
evaluator-independent (`infeasible` with no expected or result state). The
cross-app setup only stages its local HTML/upload files and launches Writer,
Chrome, and Chrome's debug-port bridge; every meaningful interaction remains a
public trajectory step bound to a unique capability line in the immediately
prior render. The Chrome and GNOME settings fixtures remain read-only
representation canaries.

Run later from the synced repository on a warm host:

```bash
python3 infra/gcp_semantic_simple_canary.py \
  --base-url http://127.0.0.1:8079 \
  $(for f in infra/semantic_simple_trajectories/[0-9][0-9]-*.json; do printf -- '--trajectory %q ' "$f"; done) \
  --output results_vm/semantic-simple-canaries
```
