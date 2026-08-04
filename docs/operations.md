# Operations

## Start and inspect the server

```bash
./semantic-os serve
curl -fsS http://127.0.0.1:8079/health | jq
```

The server binds only to loopback by default. A healthy response reports the
runtime protocol, runtime identity, and registered episode IDs. An episode
owns its nested desktop and is deleted after evaluation/cleanup.

Before model runs, replay a model-free trajectory against the warm server:

```bash
./semantic-os canary \
  --base-url http://127.0.0.1:8079 \
  --trajectory infra/semantic_simple_trajectories/01-chrome-webpage.json \
  --output results/canary-chrome
```

The canary records the literal model-visible text and image-policy counters,
performs real reads/actions in the nested desktop, and skips the task evaluator
unless `--evaluate` is explicitly supplied.

## Run a pool

```bash
export OPENROUTER_API_KEY='...'
./semantic-os run \
  --tasks-file pools/semantic-simple-singleapp3.json \
  --provider openrouter \
  --model qwen/qwen3.6-27b \
  --thinking medium \
  --max-tool-calls 60 \
  --concurrency 1 \
  --label smoke
```

Task-pool entries are resolved relative to the pool JSON. The included pools
therefore work in any clone path. Use one nested desktop per concurrent worker
and size host RAM accordingly.

## Warm GCP hosts

The sequence is `create`, `sync`, `setup`, `warm`, then scored work. Keep outer
hosts running between rounds; only nested episode desktops reset. When runtime
code changes, `sync` copies a new immutable snapshot. The remote launcher
drains registered idle episodes and restarts only the benchmark server when
the semantic runtime hash changes.

```bash
./semantic-os vm status
./semantic-os vm sync
./semantic-os vm setup
./semantic-os vm warm
```

The launcher refuses to sync or start another scored run when the existing run
PID is live. It queues no destructive cleanup of unrelated Docker state.
Expired cleanup is limited to containers using the exact OSWorld image and
only when the semantic server reports no registered episodes.

## Collect

After `./semantic-os vm status` shows every shard complete:

```bash
infra/gcp_collect.sh results_gcp/my-run
```

Collection verifies task IDs against each shard, result counts, pool hashes,
runtime manifests, duplicate tasks, and absence of recorded setup/evaluation/
cleanup errors before aggregating traces.

## Local model endpoints

Pass `--model-base-url` to register an OpenAI-compatible endpoint in the same
policy loop. The endpoint must expose a text-input model. Record its weights,
quantization, context length, output limit, thinking format, and serving
revision outside the API secret and include that identity with published
results.

## Logs and secrets

Results, traces, provider usage, manifests, and logs are ignored by Git.
OpenRouter keys are copied to a remote file with mode 0600 and removed as soon
as the harness process inherits the environment. Guest bearer tokens and
image bytes are never written to policy traces.
