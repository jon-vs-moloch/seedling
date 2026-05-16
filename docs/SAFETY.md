# Seedling Safety Notes

This document tracks the minimum safety bar for presenting Seedling as an Owl & Kestrel experiment download.

## 1. Real Sandboxing

ZenCode's `run_bash` tool defaults to container mode. Each command is executed through Docker or Podman in a one-shot container with the active sandbox mounted at `/workspace`.

If no container runtime is available, `run_bash` fails closed.

## 2. No Host Secrets

The supervisor no longer forwards `os.environ` to child processes. It builds a small allowlisted environment with only process basics and explicit per-role settings.

## 3. Filesystem Boundary

`read_file` and `write_file` resolve paths through a realpath/commonpath check and reject absolute paths or traversal outside the sandbox. Container commands receive only the sandbox directory as a bind mount.

Runtime sandboxes default to `.seedling_runtime/` under the project so Docker Desktop and Colima can mount them reliably.

## 4. Network Policy

Command containers default to `--network none`. The server processes still bind to localhost so the UI can function, but generated shell commands do not receive outbound network access by default.

## 5. Resource Caps

Command containers are launched with CPU, memory, pid, timeout, read-only filesystem, tmpfs, no-new-privileges, and dropped Linux capabilities. Defaults are intentionally small and configurable through environment variables.

## 6. Artifact Quarantine

ZenCode ships update artifacts as bounded tarballs. The supervisor copies each detected artifact into a quarantine directory, validates size, member count, expanded size, and member paths, then records a digest. Artifacts are not auto-applied.

## 7. Reset Flow

The supervisor exposes `/api/reset` and a dashboard Reset button. Reset stops managed processes, removes the runtime workspace, recreates fresh sandboxes, and restarts the stack.

## Remaining Release Notes

- Docker or Podman is a hard requirement for the safe public posture.
- The VLM/operator loop is still experimental and should be described as observational, not dependable automation.
- Public packaging should avoid bundling generated venvs, build outputs, logs, screenshots, or runtime sandboxes.
