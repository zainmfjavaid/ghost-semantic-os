# Ghost Semantic OS

[![CI](https://github.com/zainmfjavaid/ghost-semantic-os/actions/workflows/ci.yml/badge.svg)](https://github.com/zainmfjavaid/ghost-semantic-os/actions/workflows/ci.yml)

Ghost Semantic OS is a text-only computer-use runtime for a modified OSWorld
Linux environment. Instead of giving a policy model screenshots, mouse
coordinates, keyboard strings, browser JavaScript, or a shell, it exposes the
current computer as a compact accessibility-oriented scene:

```text
Surfaces
[A] Chrome - United Airlines - Active
[B] Files - Downloads

Active Surface [A] - Chrome - United Airlines - Active
[A1] link "Baggage fee calculator" click
[A2] textbox "Search" type=replace value=""
```

The model receives exactly three computer tools:

```text
read_computer(query?, within?, cursor?)
computer_click(element)               # A, A1, B10, ...
computer_type(element, text)
```

Surface IDs are letters; elements are surface-qualified numbers. The model
stops naturally after re-reading and verifying the requested state. There is
no model-facing screenshot, raw coordinate, keyboard, JavaScript, Python,
shell, critic, task card, hidden evaluator feedback, or completion tool in
`semantic-simple-v1`.

> **Benchmark disclosure:** this is not stock OSWorld's screenshot/pyautogui
> action space. It is a modified Ghost semantic OS environment evaluated by
> OSWorld after the policy loop ends. Scores from it must be labeled that way.

## What is packaged

- A reproducible six-patch series over a pinned OSWorld commit.
- An environment server that owns nested OSWorld desktop VMs.
- A versioned guest semantic agent using CDP, AT-SPI, UNO, native app bridges,
  document parsers, and safe fallbacks.
- The compact `read_computer` / `computer_click` / `computer_type` facade.
- A text-only Pi policy harness supporting hosted or OpenAI-compatible local
  models.
- Runtime hashing, image-policy counters, trace capture, result manifests,
  canaries, GCP warm-host orchestration, and evaluator-isolation tests.
- Exact task pools and the model-free validation reports used during
  development.

The exact Apache-licensed OSWorld base tree is included as a checksummed source
archive and patched locally. Upstream no longer advertises the frozen commit,
so this snapshot makes new installations reproducible rather than depending on
mutable remote history.

## Requirements

The full runtime is tested on:

- Ubuntu 22.04 x86_64.
- A VM or bare-metal host exposing `/dev/kvm`; nested virtualization is
  mandatory when the host is itself a VM.
- Node.js 22+, Python 3.10+, Docker, and at least 150 GB free disk.
- 16 vCPU / 32 GB RAM minimum; 32 vCPU / 64–128 GB RAM and a 400 GB SSD are
  recommended for parallel episodes.
- Public internet access for the first OSWorld image download and task setup.

The repository can be bootstrapped and tested on macOS with `--source-only`,
but real OSWorld episodes require Linux KVM.

## Quick start on an existing Ubuntu VM

```bash
git clone https://github.com/zainmfjavaid/ghost-semantic-os.git
cd ghost-semantic-os
./semantic-os bootstrap
# Log out/in once if the installer added you to the docker group.
./semantic-os doctor
```

Start the environment server:

```bash
./semantic-os serve
```

In a second shell, run a pool with a hosted model:

```bash
export OPENROUTER_API_KEY='...'
./semantic-os run \
  --tasks-file pools/semantic-simple-singleapp3.json \
  --provider openrouter \
  --model qwen/qwen3.6-27b \
  --thinking medium \
  --max-tool-calls 60 \
  --concurrency 1 \
  --label first-semantic-run
```

Results land under `results/<timestamp>_<label>/results.json`. The wrapper
adds `--runtime semantic-simple-v1`, `--model-input text`, and the local env
server URL unless explicitly supplied.

For a local OpenAI-compatible model server:

```bash
./semantic-os run \
  --tasks-file pools/semantic-simple-singleapp3.json \
  --provider local-qwen \
  --model qwen3.6-27b \
  --model-base-url http://127.0.0.1:8000/v1 \
  --model-context-window 131072 \
  --model-max-tokens 32768 \
  --thinking medium
```

## One-command GCP host path

Install the Google Cloud CLI locally, authenticate, and select a project. The
launcher creates warm outer hosts but never stops or deletes them:

```bash
export GHOST_OSWORLD_GCP_PROJECT=your-project-id
export GHOST_OSWORLD_GCP_ZONE=us-central1-a
export GHOST_OSWORLD_GCP_FLEET=semantic-os-1

./semantic-os bootstrap --source-only
./semantic-os vm create
./semantic-os vm sync
./semantic-os vm setup
./semantic-os vm status
# Once setup reports complete:
./semantic-os vm warm
```

Then launch a sharded run without tearing down the warm hosts:

```bash
export OPENROUTER_API_KEY='...'
./semantic-os gcp-run \
  pools/semantic-simple-singleapp3.json smoke-3 60 \
  qwen/qwen3.6-27b medium semantic_simple_v1
```

Use `infra/gcp_collect.sh` after all episodes finish. Each episode still gets a
clean nested desktop; only the expensive outer GCE hosts remain warm.

## Reproducibility and validation

```bash
./semantic-os test --fast     # packaging, schema, TypeScript, protocol tests
./semantic-os test --full     # plus all local Python contracts
```

Tests that require a live nested desktop are reported as deferred by the local
suite and must pass through the model-free VM canary before a release is
accepted. They are not silently counted as local passes.

`scripts/bootstrap-osworld.sh` verifies the patched OSWorld Git tree, and
`scripts/package-audit.py` verifies the patch hashes, runtime file manifest,
executable bits, private path removal, and common secret patterns. A runtime
manifest records both source commits, dependency fingerprints, Docker image
identity, task-pool hash, evaluator hash, model endpoint metadata, and the
semantic runtime hash.

See:

- [Installation](docs/installation.md)
- [Architecture](docs/architecture.md)
- [Operating and benchmarking](docs/operations.md)
- [Security model](docs/security-model.md)
- [Benchmark disclosure](docs/benchmarking.md)
- [Known limitations](docs/limitations.md)

## License

Apache-2.0. The included pinned OSWorld source snapshot is also Apache-2.0.
See [LICENSE](LICENSE), [NOTICE](NOTICE), and the
[OSWorld patch manifest](patches/osworld/manifest.json).
