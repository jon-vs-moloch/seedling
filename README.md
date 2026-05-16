# Seedling

Seedling is an Owl & Kestrel experiment built from the Zen/ZenCode lineage: a small local supervisor that runs paired coding sandboxes and visual operators.

This version is being cleaned for a public experiment download. It is not a general-purpose agent runtime yet. The safe path requires Docker or Podman so agent shell commands run in an ephemeral container rather than on the host.

## Safety Posture

- ZenCode file tools are confined to each runtime sandbox directory.
- `run_bash` defaults to `SEEDLING_SANDBOX_MODE=container`.
- Container commands run with no network by default, a read-only container filesystem, dropped capabilities, resource limits, and only the sandbox directory mounted at `/workspace`.
- Runtime sandboxes live under `.seedling_runtime/` by default so Docker/Colima can mount them reliably.
- Supervisor child processes receive an allowlisted environment instead of inheriting host secrets.
- Update artifacts are quarantined and validated before any manual apply.
- The dashboard exposes a reset action that destroys runtime sandboxes and restarts clean.
- The desktop shell is a thin viewer with no Tauri shell or HTTP plugin and `withGlobalTauri` disabled.

## Requirements

- Python 3.12+
- Docker or Podman for the safe command sandbox
- Rust/Tauri toolchain only when building the desktop app

Run:

```sh
scripts/preflight.sh
```

## Local Launch

```sh
cd supervisor
./run.sh
```

Then open `http://127.0.0.1:7000`.

If Docker or Podman is missing, the app starts but agent shell commands are blocked.

## Configuration

Useful environment variables:

- `SEEDLING_SANDBOX_MODE=container` keeps command execution inside containers.
- `SEEDLING_CONTAINER_RUNTIME=/path/to/docker` overrides runtime detection.
- `SEEDLING_SANDBOX_NETWORK=none` keeps command containers offline.
- `SEEDLING_SANDBOX_IMAGE=python:3.12-slim` chooses the command image.
- `SEEDLING_SANDBOX_CPUS=1`, `SEEDLING_SANDBOX_MEMORY=512m`, and `SEEDLING_SANDBOX_PIDS=128` set resource caps.
- `SEEDLING_RUNTIME_DIR=.seedling_runtime` can move runtime sandboxes, but the path must be shareable with Docker/Podman.

`SEEDLING_SANDBOX_MODE=local` exists only for development and should not be used for a public download.

## Release Boundary

For an Owl & Kestrel experiment page, the honest download posture is:

Seedling is a downloadable local experiment for watching a self-improvement loop under supervision. It requires Docker or Podman for safe command execution. It does not ship with a hosted model, a production update system, or a guarantee that generated artifacts are useful.
