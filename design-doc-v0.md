# ZenCode Sandboxed Self-Improvement System
*Design Doc v0*

## Motivations
The goal of the ZenCode project is to build a minimalist coding agent that can genuinely bootstrap its own potential. Rather than giving the agent direct, unsafe access to our host systems, we want to construct an isolated ecosystem where it can safely run, test, and ship physical updates to its own codebase. By restricting interactions between the outer supervisory layer and the inner agent solely to UI affordances (vision and browser automation), we force the agent to prioritize building human-compatible, highly-legible interfaces while simultaneously preventing it from "cheating" via direct backend integration.

## Architecture

The system operates across three strictly delineated layers:

1. **ZenCode Sandbox (Inner Layer)**
   - Contains the core ZenCode agent, its UI, local server harnesses, and a writable workspace.
   - Operates inside an isolated container format (e.g., Docker).
   - ZenCode has full read/write access to anything within this sandbox and may update or replace any of its own internal processes (such as its backend API or its static asset folders).

2. **Operator Layer (Middle Layer)**
   - Facilitates the "interaction boundary."
   - Operates entirely outside the sandbox.
   - Presents ZenCode to an Operator Agent (powered by a VLM, like Gemma).
   - Interacts with ZenCode strictly through visual inspection (screenshots) and UI actions (click, type, scroll, keyboard input via Playwright/Puppeteer).
   - Features no direct shell or backend hooks into the ZenCode sandbox.

3. **Supervisor (Outer Layer)**
   - The master control plane for the operation.
   - Responsible for environment enforcement: sandbox creation, container destruction, managing resource caps (CPU, RAM, disk limits), and enforcing networking rules (disabled by default).
   - Manages state observability (file trees, transcript logs, exit codes).
   - Handles the actual teardown and application of validated Self-Update Artifacts.

## Trust Boundaries & Invariants

*   **Operator Sovereignty:** The Supervisor and Operator layers live exclusively outside the ZenCode sandbox. Under no circumstances can ZenCode patch, query, or execute commands against the external host or the Operator layer.
*   **Encapsulated Artifacts:** Any code or build output leaving the ZenCode sandbox is treated as fully unverified and untrusted. ZenCode cannot deploy its own updates directly into the production container.
*   **Visual-Only Affordance:** The Operator model interacts exclusively via standard user inputs.

## The Update Flow (Self-Evolution)

1. **Generation:** ZenCode performs a task and produces a candidate self-update artifact (e.g., an archive of its own source codebase) inside a designated volume (e.g., `/outbox`).
2. **Detection & Signaling:** ZenCode signals via the UI that an update is ready for review.
3. **Inspection:** The Operator layer captures screenshots of the ZenCode UI to visually validate the success of the new build or read any release notes the AI provides inside its interface.
4. **Deployment:** If the Operator issues a standard UI command to proceed (or a supervisor script triggers), the artifact is considered authorized. The Supervisor stops the sandbox, applies the untrusted update payload over a fresh ZenCode container instance, and restarts the environment.

## Open Questions & Future Considerations

*   **Artifact Structuring:** Should the entire ZenCode root directory be packaged into a single tarball on every update loop, or should the system rely on differential diff patches?
*   **Rollback Mechanics:** If a deployed artifact breaks the ZenCode UI completely (preventing the Operator from visually interacting with it), how many fallback loops should the Supervisor attempt before rolling back to the last known-good image?
*   **VLM Prompting:** What is the optimal system prompt and state representation context format for the Operator (Gemma VLM) so that it understands the boundaries of its Playwright capabilities while viewing ZenCode's UI screenshots?

## Success Criteria
The project is successful once ZenCode completes a bounded coding task to improve its own internal logic or UI, successfully outputs an artifact, and the Supervisor deploys that artifact into a usable state without breaching the isolation boundary.
