# Seedling Safety Notes

This document tracks the minimum safety bar for running Seedling on your own machine.

## 1. Real Sandboxing

ZenCode's `run_bash` tool defaults to container mode. Each command is executed through Docker or Podman in a one-shot container with the active sandbox mounted at `/workspace`.

If no container runtime is available, `run_bash` fails closed.

Before the first command in a sandbox (and at `/api/init`), ZenCode checks the bind mount in both directions: it writes a marker file with a random value on the host, a container copies it to a reply file, and the host reads the reply back. A runtime inside a VM (Colima, Docker Desktop) mounts paths it does not share as an empty directory, with no error, and writes there are lost. If the check fails, no command runs and the error says to move `SEEDLING_RUNTIME_DIR` under a shared path. A failed check is not remembered, so the next command checks again.

## 2. No Host Secrets

The supervisor no longer forwards `os.environ` to child processes. It builds a small allowlisted environment with only process basics and explicit per-role settings. ZenCode gets its sandbox settings (`SEEDLING_SANDBOX_*` and `SEEDLING_CONTAINER_RUNTIME`) forwarded by name; unset ones are left out.

## 3. Filesystem Boundary

`read_file` and `write_file` resolve paths through a realpath/commonpath check and reject absolute paths or traversal outside the sandbox. Container commands receive only the sandbox directory as a bind mount.

Runtime sandboxes default to `.seedling_runtime/` under the project so Docker Desktop and Colima can mount them reliably.

## 4. Network Policy

Command containers default to `--network none`. The server processes still bind to localhost so the UI can function, but generated shell commands do not receive outbound network access by default. Each server also refuses requests whose `Host` is not a loopback name on its own port, and state-changing requests from other origins, so web pages in the operator's browser cannot drive the local APIs. ZenCode logs a failed inference request without the endpoint's URL, host or credentials.

## 5. Resource Caps

Command containers are launched with CPU, memory, pid, timeout, read-only filesystem, tmpfs, no-new-privileges, and dropped Linux capabilities. Defaults are intentionally small and configurable through environment variables.

## 6. Artifact Quarantine

ZenCode ships update artifacts as bounded tarballs. The supervisor copies each detected artifact into a quarantine directory, validates size, member count, expanded size, and member paths, then records a digest. Artifacts are not auto-applied.

What ships is a denylist, kept identical in `zencode/app.py` and `supervisor/main.py` (a test checks): the directories `scratch/`, `tmp/`, `outbox/`, `.git/`, virtualenvs, `node_modules/` and caches at any depth, and files named `*.tmp`, `*.pyc`, `*.pyo`, `*.log`, `*.swp`, `*~`, `.DS_Store` and `.seedling-*`. ZenCode's system prompt tells the agent to keep scratch work under `scratch/`. Ship Update leaves these out and returns, and logs, the list of files the archive actually holds and the list left out, so the operator sees what shipped regardless of what the agent says. The supervisor refuses an artifact that still holds one of these paths (it did not come from Ship Update), and shows the refusal in the Quarantine card. Every other file that does not already exist in the target codebase is listed in the card as new, so a human sees it before pressing Apply.

For a Lane A artifact (target `operator/`), the supervisor scores the candidate parser with `scripts/measure_cycle.py` against `operator/` as it is now, and shows both numbers, and the replies fixed and regressed, in the Quarantine card. The candidate is code a model wrote, so it is scored only in a one-shot container (no network, read-only, the parser mounted read-only); without a container runtime there is no score. Scoring is read-only and never applies anything; Apply stays a separate human action.

Supervisor timestamps (logs, `started_at`, `detected_at`, `approved_at`, `applied_at`) are UTC, ISO-8601 with a `Z`.

## 7. Reset Flow

The supervisor exposes `/api/reset` and a dashboard Reset button. Reset stops managed processes, removes the runtime workspace, recreates fresh sandboxes, and restarts the stack.

## Known Gaps

- Applying an artifact copies it over the checked-out `zencode/` or `operator/` source in place. There is no backup and no automatic rollback; use git to undo.
- Artifacts are validated for shape (size, members, paths). Only Lane A's parser is scored before apply; nothing else in an artifact is tested, and no score gates Apply.
- A file the agent leaves outside `scratch/` still ships. It is listed as new in the Quarantine card, but Apply copies it if a human applies the artifact.
- ZenCode B's sandbox starts empty, so a ZenCode improvement has to be rebuilt inside the sandbox. ZenCode A's sandbox starts with copies of `operator/README.md` and `operator/action_parser.py`, its improvement target. Nothing else from the checkout is copied in, and nothing is mounted.
- `scripts/measure_cycle.py` scores `operator/` before and after an apply. It measures; it does not gate anything. Run on the host, the scored parser could read the corpus's expected answers from disk; the measurement is not a security boundary. The child is sent only the replies and reports on a private channel, so printing a fake result gains a parser nothing.

## Remaining Release Notes

- Docker or Podman is a hard requirement for the safe public posture.
- The VLM/operator loop is still experimental and should be described as observational, not dependable automation.
- Public packaging should avoid bundling generated venvs, build outputs, logs, screenshots, or runtime sandboxes.
