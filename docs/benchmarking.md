# Benchmarking and disclosure

This repository measures whether a compact semantic OS interface can improve a
small policy model's computer-use mechanics. It does not claim stock OSWorld
performance because the observation and action space is modified.

Use this disclosure with score tables:

> Ghost Semantic OS result on a frozen OSWorld task pool using a modified
> text-only semantic environment. The policy model received no screenshots or
> pixels and acted through `read_computer`, `computer_click`, and
> `computer_type`. OSWorld's evaluator ran after the policy loop. This is not a
> stock screenshot/pyautogui OSWorld score.

## Required result identity

Record and preserve:

- Repository commit and semantic runtime hash.
- Upstream OSWorld commit and patched Git tree.
- OSWorld Docker digest.
- Complete Python and Node lock hashes.
- Exact task-pool bytes and SHA-256.
- Evaluator source hash.
- Model name, endpoint, weights/quantization when local, context/output limits,
  thinking format, and provider revision.
- Tool-call limit, concurrency, tokens, cost, wall time, and every stop reason.
- Setup, evaluator, cleanup, provider, and transport-invalid episodes.
- All strict image-policy counters.

Never convert invalid setup or evaluator episodes into model failures. Never
exclude provider-noisy episodes silently. Report both the raw denominator and
the valid denominator when they differ.

## Included evidence

`reports/semantic-simple-model-free-validation-2026-08-03.md` documents manual
model-free trajectories across browser, Chrome chrome, GNOME, dialogs, and
LibreOffice before agent scoring. The exact single-application diagnostic pool
is `pools/semantic-simple-singleapp24.json`.

These are exposed development artifacts, not an untouched final benchmark.
Use a separately frozen holdout for new performance claims.
