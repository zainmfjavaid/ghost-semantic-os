# Qwen vision-v15 exact 24-task control — August 3, 2026

## Result

Hosted `qwen/qwen3.6-27b`, medium thinking, frozen `vision-v15`, 100 executed-tool-call limit:

| Slice | Solved | Rate |
|---|---:|---:|
| Browser | 1/6 | 16.7% |
| Other applications / OS | 3/18 | 16.7% |
| Overall | **4/24** | **16.7%** |

The four passes were Chrome password-manager navigation plus all three GNOME/Files tasks: Do Not Disturb, remove Vim from favorites, and rename the directory.

## Efficiency

- Tool calls: **2,052** (mean 85.5; median 100)
- Tool attempts: **2,093**
- Cumulative tokens: **211,798,993** (8,824,958 per episode)
- Hosted cost: **$63.4532**
- Stops: 12 `step_limit`, 8 `agent_end`, 4 `task_complete`
- Public tool mix: 1,619 clicks, 348 key actions, 48 text-entry actions, 34 explicit screenshots, 16 waits, 16 scrolls, 7 drags, and 5 completion calls

## Same-pool comparison

| System | Score | Calls | Cumulative tokens | Hosted cost |
|---|---:|---:|---:|---:|
| Qwen 3.6 27B, conventional vision | **4/24 (16.7%)** | 2,052 | 211,798,993 | $63.4532 |
| Qwen 3.6 27B, semantic-simple | **10/24 (41.7%)** | 1,316 | 37,555,988 | $7.5450 |
| Opus 5, semantic-simple | **12/24 (50.0%)** | 1,084 | 28,635,589 | $26.1479 |
| Opus 4.8, conventional vision | **22/24 (91.7%)** | — | — | — |

On this pool, the Ghost harness raised Qwen by **six solved tasks / 25.0 percentage points / 2.5x the raw success rate**. It used 35.9% fewer tool calls, 82.3% fewer cumulative tokens, and 88.1% less hosted spend. The harness did not approach the conventional Opus vision ceiling: 10/24 versus 22/24.

The aggregate hides an important inversion. Conventional-vision Qwen solved all three GNOME/Files tasks while semantic-simple Qwen solved none. The semantic gain came from browser and application tasks; the current OS accessibility facade is a regression for this model.

## Validity audit

- Exact pool: `pools/semantic-simple-singleapp24.json`
- Pool SHA-256: `a3602e946e2d745d21f9056fccb1cbd757ca004e8e570ac220ebec98c90bf1fa`
- All 24 task IDs are unique and all 24 produced evaluator scores.
- Setup-invalid episodes: **0**
- Evaluator-invalid episodes: **0**
- Cleanup-invalid episodes: **0**
- Terminal run errors recorded in result artifacts: **0**

The arm is provider-noisy, not pristine. **20/24 episodes** contain at least one transient provider-turn error in the run logs, totaling 83 error turns. These include stream termination, exhausted provider targets, external-process response exhaustion, HTTP 524, one request-body-too-large failure, and upstream engine/content failures. Retries allowed every episode to produce a final evaluator score, so none was removed from the raw denominator. This noise likely makes 4/24 a conservative estimate of clean base-vision capability and must be disclosed in any chart.

Collection also correctly rejected the arm as one homogeneous manifest because the hosts split across two Python dependency fingerprints: dev2/dev3 used `c1d07f…`, while dev6/dev7 used `702a6f…`. Runtime code, frozen parent/nested commits, Docker image, evaluator hash, pool hash, model, variant, and task IDs match. Each fingerprint half independently scored 2/12. Preserve the raw comparison, but do not call the arm a single immutable environment.

This is hosted Qwen, not the exact local Ghost Box endpoint, and the 24 tasks are a small exposed single-application diagnostic rather than the full OSWorld benchmark.
