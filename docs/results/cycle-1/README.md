# Cycle 1: ZenCode improves the operator's action parser (2026-10-18)

One real, measured Seedling cycle on Lane A: ZenCode A, running a local open-weight model in its container sandbox, rewrote `operator/action_parser.py`. It shipped the result, the supervisor quarantined it, a human applied it through the supervisor's API, and the operator restarted on the new code.

| | Before | After |
|---|---|---|
| Parser score (`scripts/measure_cycle.py`, 43 replies) | **23/43** (53.5%) | **31/43** (72.1%) |
| Smoke tests (`scripts/test.sh`) | 31 OK | 31 OK |
| Per-reply changes | | 8 fixed, 0 regressed |

## What was real and what was scripted

- **Real:** the ZenCode agent loop and its tools, the container sandbox (Docker under Colima, `--network none`, read-only root, all capabilities dropped), Lane A's seeded sandbox, **Ship Update**, the supervisor's outbox polling, archive validation and quarantine, `POST /api/updates/{id}/apply`, and the restart of both operators on the applied code. Every parser line in the applied `action_parser.py` was written by the model.
- **Scripted, not the vision operator:** no vision model was available, so `scripts/run_cycle.py` played Operator A. It sent ZenCode fixed messages through the same HTTP API the UI uses (`messages/`). The operators ran, but they had no browser or VLM, so they stayed paused.
- **Human steps:** a person (the desk agent) chose each follow-up message, restarted ZenCode A twice to clear its chat history (`POST /api/process/zencode_a/restart`; the sandbox stays), and made the apply decision. Every message, the time it was sent and ZenCode's full log are in `attempt-*.json`.

## Model and setup

- **ZenCode's model:** Qwen3-8B (open weights, runs locally), file `Qwen3-8B-Q4_K_M.gguf` from `Qwen/Qwen3-8B-GGUF` at revision `7c41481f`, sha256 `d98cdcbd…5785`. It was served by `llama-server` (llama.cpp, Homebrew build, Metal) on the developer's laptop at `127.0.0.1:18090` with `--jinja`. Attempt 1 ran with thinking on; attempt 2 ran with `enable_thinking=false`. ZenCode sends no temperature, so the server default applied.
- Qwen2.5-Coder-7B-Instruct Q4_K_M was tried first. In 0 of 3 probes with ZenCode's real system prompt did it emit structured tool calls (it wrote pseudo-calls as text), so it was not used.
- **Stack:** the supervisor (`uvicorn main:app` on port 7100, because macOS AirPlay holds 7000), ZenCode A and B on 8000 and 8001, operators on 9000 and 9001. `SEEDLING_RUNTIME_DIR` was under the home directory, because Colima mounts only the home directory (see Findings). Sandbox image `python:3.12-slim` (`sha256:a6e34c59…`). Host Python 3.14.3.

## Timeline (UTC)

| Time | Step |
|---|---|
| 23:07:22 | Baseline measured: 23/43, 31 tests OK (commit `1e1e99df`) |
| 23:09–23:20 | **Attempt 1** (thinking on): 4 messages. ZenCode rewrote the parser but got stuck on quoting errors |
| 23:20:37 | Attempt 1 shipped; quarantined as `76df40fb7529` (sha256 `cdb271b8…`) |
| 23:20:45 | Attempt 1 measured from quarantine, before any apply: **8/43**. **Not applied.** |
| 23:21:09 | `POST /api/reset`: fresh stack, sandbox seeded again |
| 23:21–23:32 | **Attempt 2** (thinking off): 9 messages, 2 ZenCode restarts |
| 23:32:38 | Shipped `zencode_update_341016131697.tar.gz` (2,593 bytes, 5 members) |
| 23:32:40 | Quarantined as `95ec8d06f7bc` (sha256 `341016131697d249c2cc868c9099ba2ea29a48d3e2786d1b1b1217e8147b1154`); validation passed |
| 23:32:49 | Measured from quarantine, before apply: 31/43 |
| 23:32:55 | Apply requested with `operator/` and `zencode/` committed (HEAD `1e1e99df`); HTTP 200, status `applied`; operators restarted |
| 23:33:05 | Measured after apply: 31/43, 31 tests OK |

Supervisor-side timestamps in `receipts.json` (`detected_at`, `approved_at`, `applied_at`) are the host's local time (UTC−5), because the supervisor uses naive `datetime.now()`.

## How it went

- **Attempt 1 failed, and the gate held.** ZenCode spent its 10-tool-turn budget per message fixing string-quoting errors in its own check script. It left a parser that strips every double quote before `json.loads`. Scored from quarantine, it got 8/43, below the baseline, so it was not applied.
- **Attempt 2 improved the parser.** After a quoting hint (put examples in a file instead of in string literals), ZenCode added validation (26/43, then 27/43, then 29/43 in fresh contexts). It then plateaued, rewriting the same file in a loop. A follow-up message (`task-candidates.md`) restated the README's diagnosis: try each balanced `{...}` in turn. With it, ZenCode wrote the parser that shipped: it tries progressively wider brace spans and returns the first that `json.loads` accepts. That fixed 7 of the 9 trailing-prose, multi-object and think-block replies the baseline failed, plus the bare JSON string (8 fixes). But the rewrite dropped the validation the earlier versions had, so unusable actions are still returned as they are, as in the baseline.
- **Still failing (12/43):** single quotes, curly quotes and trailing commas (3); `Click`, string `ms` and missing `ms` (3); click with no selector, empty selector, unknown action and missing `action` (4); an invalid object before a valid one (1); and braces inside a `<think>` block before a valid object (1).
- **ZenCode misreported its own work.** Its final reply said the sandbox held only `README.md` and `action_parser.py`. In fact it still held `check_parser.py`, `examples.txt` and `parse_action.py`, a copy of the new parser. All three were in the artifact and were applied into `operator/`. The desk deleted them before committing. Their hashes are in `receipts.json` (`artifact_members`).
- **Empty replies.** 8 of the 13 messages ended with an empty reply: either ZenCode used up its 10 tool turns, or the model returned neither text nor a tool call. Each needed another message to continue.

## Findings for Seedling

1. **The Colima mount fails silently.** With the runtime dir under `/private/tmp`, Docker mounted an empty `/workspace`, and files written inside the container never reached the host. Nothing reported an error. ZenCode should check, once per sandbox, that a marker file is visible inside the container.
2. **`SEEDLING_CONTAINER_RUNTIME` is not forwarded.** The README lists it as a `run.sh` setting, but the supervisor does not pass it to ZenCode. Docker worked here only because the Docker CLI fell back to the user's own config.
3. **Apply copies whatever is in the sandbox.** Scratch files ship and land in `operator/`. Quarantine shows only the member count.
4. **Pre-apply scoring helps.** Measuring the extracted quarantine artifact (`measure_cycle.py --operator-dir <extracted> --skip-tests`) caught attempt 1. It is not wired into the dashboard.
5. **Ten tool turns per message is tight for an 8B model.** Most messages ended with the turn budget spent, or with an empty reply.

## Files

| File | What it is |
|---|---|
| `measure-before.json`, `measure-after.json` | Full measurements with per-reply outcomes |
| `measure-quarantined.json`, `measure-attempt1-quarantined.json` | The two shipped artifacts, scored from quarantine before any apply |
| `attempt-1.json`, `attempt-2.json` | Messages sent, ZenCode's replies, and ZenCode's tool logs |
| `receipts.json` | Ship result, quarantine record, artifact members with sha256, apply request and result, and the supervisor log |
| `corpus-recording.json` | The 12 recorded replies in the corpus: Qwen3-8B answering the operator's real system prompt, with a text description of the screen in place of a screenshot |
| `messages/` | The exact messages the script sent to ZenCode |

Paths are shortened to `<repo>`, `<runtime>` and `<scratch>`.

The corpus (`tests/fixtures/operator_replies.jsonl`) was written by the desk before the cycle and was never in ZenCode's sandbox. A 50-line reference parser that was never shown to ZenCode scores 43/43, which shows the corpus is consistent with the contract.
