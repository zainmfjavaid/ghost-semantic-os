# Installation

## Supported host

The acceptance target is Ubuntu 22.04 LTS on x86_64 with hardware
virtualization exposed as `/dev/kvm`. GCE instances must be created with nested
virtualization. AWS, Azure, and bare-metal hosts can work if they expose KVM,
but this release's automated host lifecycle is GCP-specific.

Recommended capacity for one concurrent episode is 16 vCPU, 32 GB RAM, and
150 GB free disk. Use 32 vCPU, 64–128 GB RAM, and a 400 GB SSD for several
parallel nested desktops. The first image warm-up downloads roughly 11 GB and
expands it substantially.

## Source bootstrap

```bash
git clone https://github.com/zainmfjavaid/ghost-semantic-os.git
cd ghost-semantic-os
./semantic-os bootstrap --source-only
./semantic-os doctor --source-only
```

This extracts the checksummed OSWorld base snapshot, verifies its Git tree,
applies the ordered patch set, verifies the final Git tree, and installs the
exact Node lockfile. `OSWORLD_REPOSITORY` controls only the informational
upstream remote recorded in the reconstructed checkout.

## Full Ubuntu host bootstrap

```bash
./semantic-os bootstrap
```

The installer:

1. Fails unless Linux, `apt-get`, and `/dev/kvm` are present.
2. Installs Docker, Python venv support, Node.js 22, Poppler, bubblewrap, jq,
   and build prerequisites.
3. Creates `.venv` from the checked-in complete Linux dependency lock.
4. Runs `npm ci` from `harness/package-lock.json`.
5. Pulls the pinned OSWorld Docker digest and tags that exact image locally.
6. Writes a secret-free `.environment_state` dependency fingerprint.

The script may add the current user to the `docker` group. Log out and back in
once before `./semantic-os doctor` if Docker access fails.

To use another audited image, set `GHOST_OSWORLD_DOCKER_IMAGE` to an immutable
digest. Do not use a mutable tag for a reported benchmark.

## GCP creation

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT
export GHOST_OSWORLD_GCP_PROJECT=YOUR_PROJECT
export GHOST_OSWORLD_GCP_FLEET=semantic-os-1

./semantic-os vm create
./semantic-os vm sync
./semantic-os vm setup
./semantic-os vm status
```

Defaults are `us-central1-a`, `n2-standard-32`, Ubuntu 22.04, and a 400 GB
pd-ssd. Override them with `GHOST_OSWORLD_GCP_ZONE`,
`GHOST_OSWORLD_GCP_MACHINE`, and `GHOST_OSWORLD_GCP_DISK_SIZE`.

Normal fleet commands do not stop or delete outer VMs. `sync` refuses to
replace code while a scored harness process is active. It installs the new
snapshot atomically and retains the prior directory as a timestamped backup.

## Verify

```bash
./semantic-os doctor
./semantic-os test --full
```

The doctor checks the exact patched OSWorld tree, Node, the Python imports,
Docker access, KVM, and the image. Do not begin scored episodes with a red
doctor or model-free canary.

`test --full` runs every local contract in an isolated Python process. Live
CDP/desktop scripts are explicitly marked deferred and are covered by the
real-VM canary instead of being faked or counted as local passes.
