# Semantic-simple browser generalization gate — 2026-08-03

## Result

The generic browser-click repair was frozen before this six-task run. None of
the six tasks is the Ryanair development task that exposed the defect.

| Task | Hosted Qwen | Opus | Notes |
|---|---:|---:|---|
| Chrome password manager | 1 / 11 calls | 1 / 8 | Both reached the correct built-in Chrome surface without revealing a credential. |
| JFK → ORD tomorrow | 0 / 80 | 0 / 30 | Delta's result path was blocked/unstable; Opus substituted Google Flights, while Qwen never made the Delta date selection persist. Neither receives OSWorld credit. |
| Apple three-iPhone comparison | 0 / 33 | 1 / 13 | Opus reached the required Apple comparison URL. Qwen produced the comparison from GSMArena instead of completing Apple's configured state. |
| Manchester monthly weather | 1 / 24 | 1 / 16 | Both reached the required monthly forecast state. |
| United baggage calculator | 1 / 35 | 0 / 18 | Qwen passed. Opus's final semantic state showed the exact evaluator-matching United URL and page, but the post-episode getter returned zero; retain raw zero and label the discrepancy. |
| EVs ≤$50K, 50 miles of 10001 | 0 / 28 | 0 / 21 | Cars.com was Cloudflare-blocked for both; both completed equivalent filters on CarGurus, which does not satisfy the site-specific grader. |

Raw matched result: **Qwen 3/6 (50%); Opus 3/6 (50%)**.

Efficiency:

- Qwen: 211 calls, 35.2 mean calls/task, 5,586,386 cumulative tokens, $1.1824.
- Opus: 106 calls, 17.7 mean calls/task, 1,394,646 cumulative tokens, $1.7224.

Qwen matched Opus's raw outcome score but required 1.99× the calls and 4.01×
the cumulative tokens. This is a small internal generalization gate, not a
frontier-parity claim.

## Integrity

- Parent source: `9b2dc87e2f795856af8b12b890db3b9fce6af28c`
- Nested OSWorld/evaluator: `d3781e929734efdc877fa6bfc5370e669570914c`
- Runtime-files SHA-256: `e6d894f1c1bb4f81fad201537e1766291a61f9f522a43a2302a2d6737880e085`
- Pool SHA-256: `38ba357a3463fc9063a74d5e314264c2d21b70443480532bcb12a117928f12c4`
- Tool limit: 100; thinking: medium; runtime: semantic-simple.
- No-action preflight: 6/6 completed, zero evaluator/setup errors, zero tasks passing initially.
- Across all 12 scored episodes: zero screenshots, image parts, pixels sent to the policy model, and visual-sidecar calls.
- Artifacts: `results_gcp/browser-generalization6-qwen/` and `results_gcp/browser-generalization6-opus/`.

## Conclusion

The browser repair generalizes beyond the exposing task: both models solve
Chrome internal settings, a multi-product Apple state, monthly forecast
navigation, and/or United navigation through the same three public tools. The
remaining raw failures are not one repeated clickability regression. They
separate into target-site blocking/drift, source substitution, one dynamic
controlled-input/date-picker failure, and an evaluator/active-tab discrepancy.
The next browser improvement should target reliable controlled-input typing and
active-tab truth, not add task/site recipes or restore a larger semantic DSL.
