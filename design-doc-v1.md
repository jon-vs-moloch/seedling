# ZenCode Sandboxed Self-Improvement System
*Design Doc v1 — Dual-Operator Architecture*

## Motivations
The goal of the ZenCode project is to build a minimalist coding agent that can genuinely bootstrap its own potential. Rather than giving the agent direct, unsafe access to our host systems, we want to construct an isolated ecosystem where it can safely run, test, and ship physical updates to its own codebase. By restricting interactions between the outer supervisory layer and the inner agent solely to UI affordances (vision and browser automation), we force the agent to prioritize building human-compatible, highly-legible interfaces while simultaneously preventing it from "cheating" via direct backend integration.

## Architecture

The system operates across four process types orchestrated by a central Supervisor:

### 1. ZenCode Sandbox (Inner Layer)
- A sandboxed coding agent with its own UI, local server, and writable workspace.
- Communicates with an inference endpoint via OpenAI-compatible function calling (native tool calls).
- Has three tools: `run_bash`, `write_file`, `read_file` — all constrained to its sandbox directory.
- Can produce self-update artifacts and ship them to an `/outbox` boundary.

### 2. Operator Layer (Middle Layer)
- A VLM-driven autonomous agent that interacts with ZenCode exclusively through visual inspection (screenshots) and browser automation (Playwright).
- No direct shell or backend hooks into the ZenCode sandbox.
- Two instances run simultaneously:

| Instance | Role | Goal | Target Codebase |
|---|---|---|---|
| **Operator A** (Meta) | Improve the Operator itself | Drives ZenCode-A to patch `operator/` | `operator/` |
| **Operator B** (Ground) | Improve ZenCode | Drives ZenCode-B to patch `zencode/` | `zencode/` |

### 3. Supervisor (Outer Layer)
- A FastAPI application that serves as the master control plane.
- Manages all four processes with full lifecycle control (start, stop, restart, health checks).
- Polls ZenCode outboxes for update artifacts and routes them to the correct target codebase.
- Exposes a REST API consumed by its own web dashboard (and future Tauri desktop shell).
- Serves on port 7000.

### Process Topology

```
Supervisor (:7000)
├── Operator A (:9000) ──Playwright──→ ZenCode-A (:8000)
│   Goal: improve operator/          Sandbox: /tmp/zen_stack_*/zencode_a_workspace
│
└── Operator B (:9001) ──Playwright──→ ZenCode-B (:8001)
    Goal: improve zencode/           Sandbox: /tmp/zen_stack_*/zencode_b_workspace
```

### Convergence Property
Operators A and B are instances of the same codebase, differentiated only by environment variables. When Operator A drives ZenCode to improve the operator code, both A and B receive the improvement on the next deploy cycle. This creates a fixed-point iteration — not unbounded recursion — because:
- There are exactly 2 operator instances (no mechanism to spawn a third)
- The improvement target is always *source code*, never the running instance
- Both operators benefit equally from operator-code improvements

## Trust Boundaries & Invariants

*   **Operator Sovereignty:** The Supervisor and Operator layers live exclusively outside the ZenCode sandbox. ZenCode cannot patch, query, or execute commands against the host or any Operator instance.
*   **Encapsulated Artifacts:** Any code leaving a ZenCode sandbox is treated as fully unverified. ZenCode cannot deploy its own updates directly.
*   **Visual-Only Affordance:** Operators interact exclusively via screenshots and standard UI inputs. No shell access, no IPC, no shared state.
*   **Prompt-Based Sandboxing (Dev):** ZenCode is instructed via system prompt to confine itself to its working directory. Container-level isolation is planned for production hardening.

## The Update Flow

1. **Generation:** ZenCode performs a task and produces a candidate artifact inside `/outbox`.
2. **Detection:** The Supervisor polls each ZenCode instance's outbox directory.
3. **Routing:** Artifacts from ZenCode-A target `operator/`; artifacts from ZenCode-B target `zencode/`.
4. **Inspection:** The Supervisor logs the detection and (in future) triggers validation.
5. **Deployment:** The Supervisor tears down affected processes, applies the artifact, and restarts.

## Configuration

All processes are configured via environment variables:

**ZenCode:**
- `ZENCODE_SANDBOX_DIR` — Override the tempdir sandbox location

**Operator:**
- `OPERATOR_TARGET` — ZenCode UI URL to interact with
- `VLM_ENDPOINT` — VLM inference API URL
- `OPERATOR_GOAL` — High-level goal description
- `OPERATOR_ROLE` — Display name (e.g., "Meta Operator (A)")
- `OPERATOR_WORK_DIR` — Directory for screenshots and telemetry logs

## Port Allocation

| Process | Port |
|---|---|
| Supervisor Dashboard | 7000 |
| ZenCode A | 8000 |
| ZenCode B | 8001 |
| Operator A | 9000 |
| Operator B | 9001 |

## Future Considerations

*   **Tauri Desktop Shell:** Wrap the Supervisor dashboard in a native desktop app for a polished human-facing surface.
*   **Container Isolation:** Replace prompt-based sandboxing with Docker for production.
*   **Artifact Validation:** Automated test suites that run against new artifacts before deployment.
*   **Rollback Mechanics:** If a deployed artifact breaks the ZenCode UI, automatic rollback to the last known-good version.
*   **Differential Updates:** Move from full-snapshot tarballs to incremental diff patches.

## Success Criteria
The project is successful once:
1. Operator B drives ZenCode to improve ZenCode's own codebase and ships a valid artifact.
2. Operator A drives ZenCode to improve the Operator codebase and ships a valid artifact.
3. The Supervisor applies both artifacts without breaching isolation boundaries.
4. The improved Operator code demonstrably performs better in subsequent cycles.
