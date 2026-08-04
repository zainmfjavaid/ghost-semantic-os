# Known limitations

- This is a modified OSWorld environment, not a drop-in replacement for stock
  OSWorld's screenshot/pyautogui policy interface.
- The strict release intentionally has no visual-inspector sidecar. Tasks that
  require judging freehand composition, visual similarity, or canvas layout
  may return a representation gap.
- Semantic coverage is strongest for browser pages, Chrome state, GNOME,
  dialogs, Files, and LibreOffice. Other adapters are present but have fewer
  end-to-end task trajectories.
- Accessibility quality depends on the exact Ubuntu application versions in
  the OSWorld image. An application update can change exposed roles/actions.
- Native API mutation can satisfy persistent state without reproducing the
  literal mouse path a human would take. Traces disclose that execution route.
- The bundled dependency lock is Linux-specific and intentionally large
  because document, PDF, OCR, audio, and image metadata adapters are included.
- GCP lifecycle automation is provided; other clouds need equivalent KVM,
  Docker, networking, and host identity wiring.
- The environment server is single-trust-domain research infrastructure. Do
  not expose it to the public internet or run hostile tenants together.
- `semantic-v1` and `semantic-plus-v1` internals remain for research and
  conformance. The supported low-context public policy surface is
  `semantic-simple-v1`.
