# Contributing

Run `./semantic-os test --full` before submitting a change. Adapter changes
must be generic: no task IDs, known answers, evaluator names, task-specific
selectors, or fixture-only recipes. Every query must be read-only; every action
must report its actual execution route and fail safely on stale/ambiguous
targets.

Changes to the model-facing schema, prompt, guest bundle, OSWorld patch set, or
runtime file manifests require a new runtime hash and benchmark rerun. Update
documentation and add model-free contract tests before agent benchmarks.

Do not commit result artifacts, model/provider keys, guest bearer tokens,
screenshots, private paths, or GCP instance identities.
