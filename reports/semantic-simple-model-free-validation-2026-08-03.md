# Semantic Simple v1 — model-free validation

Date: 2026-08-03

Runtime: `semantic-simple-v1`

Runtime SHA-256: `6ec9c0f76e9d584a229984de855427cec87d11b074ee2efc3d6e4f933b309509`

## Result

- 19/19 trajectories passed on the same runtime hash across six warm GCP hosts.
- 91 public operations completed with 0 model calls.
- All zero-image audits passed: no screenshots, image parts, policy-model pixels, or visual-sidecar calls.
- 0 public-ID stability mismatches.
- 0 duplicate rendered lines and 0 forbidden protocol-jargon findings.
- 159,550 rendered characters / 39,919 estimated tokens total.
- 1,753 characters / 439 estimated tokens per public operation on average.
- Maximum single result: 9,812 characters / 2,453 estimated tokens.
- Maximum returned rows: 60. Larger semantic states remained queryable through filters/cursors.

The model-free canary deliberately does not run the original task evaluator by default. These trajectories validate the public computer interface and are not attempts to solve the source OSWorld instructions. Evaluator execution remains an explicit `--evaluate` option.

## Public interface

- `read_computer(query?, within?, cursor?)`
- `computer_click(element)`
- `computer_type(element, text)`

Surface IDs use letters (`A`, `B`, `C`); element IDs are surface-qualified (`A1`, `B9`, `C10`). Every active-surface header includes the friendly application name, title, and state. Only active-surface elements are rendered.

## Covered trajectories

Chrome webpage, form, select/combobox, iframe traversal, and Chrome settings; GNOME Settings, question dialog, and file chooser; Writer, Calc, and Impress; Thunderbird; VS Code; VLC; PDF/Evince; Terminal; GIMP; a Writer/Calc/Files multi-app flow; and the full Chrome → Writer → Desktop → Settings → Chrome → native portal file-chooser journey.

The Zenity file-selection fixture records an honest read-only `representation_gap`: its current accessibility state does not prove a safe exact-path action. Exact-path chooser control is separately proven by the real Chrome portal chooser in trajectory 19.

## Release gates

- Python: 320 tests and 89 subtests passed.
- TypeScript compile passed.
- Semantic schema drift check passed.
- All semantic harness test programs passed.
- `git diff --check` passed.

## Evidence

Raw review bundles, literal rendered text for every public operation, zero-image counters, per-step audits, and summaries are stored under:

`results_vm/model-free-review/release-6ec9c0f7/`
