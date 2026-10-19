#!/usr/bin/env python3
"""Score operator/ before and after a Seedling cycle.

Two measurements, both deterministic and needing no model:

  parse  how many replies in tests/fixtures/operator_replies.jsonl the
         operator's parse_action() (operator/action_parser.py) turns into the
         expected action, or correctly refuses with ValueError. The contract
         is in operator/README.md.
  tests  the smoke suite (scripts/test.sh): how many tests ran, and whether
         it passed.

After a cycle the parser is code a model wrote, so it runs in a separate
`python -I -B` process with a time limit per reply and overall, and only that
process runs it. The child is sent the replies, never the expected answers,
and it reports what the parser did as one JSON document on a private copy of
its stdout; the parser's own prints go to stderr. The parent checks that the
document covers exactly the replies it sent, and does all the comparing. So
printing a fake result line gains a parser nothing: the most it can report
is an answer it could have returned anyway. On the host the parser could
still open the corpus file itself, so this is not a security boundary.
With --container-runtime the child runs in a one-shot container instead
(no network, read-only, the parser mounted read-only, nothing else), which
is how the supervisor scores a quarantined artifact before anyone applies it.
The corpus is under tests/, outside operator/, so an applied artifact cannot
change it.

Usage:
  python3 scripts/measure_cycle.py [--operator-dir DIR] [--corpus FILE]
                                   [--label TEXT] [--skip-tests] [--out FILE]
                                   [--container-runtime PATH] [--image IMAGE]
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_CORPUS = os.path.join(ROOT, "tests", "fixtures", "operator_replies.jsonl")
REQUIRED_KEYS = {"click": ("selector",), "fill": ("selector", "value"), "wait": ("ms",), "done": ()}
CASE_TIMEOUT_SECONDS = 2
PARSER_TIMEOUT_SECONDS = 120
TEST_TIMEOUT_SECONDS = 900
SKIPPED_DIRS = {"venv", ".venv", "__pycache__"}

DEFAULT_IMAGE = "python:3.12-slim"
OUTCOME_KINDS = {"action", "refused", "crash", "timeout"}

# Runs in the child process (on the host or in a container). It reads the
# replies (ids and text only) from stdin, loads parse_action from the given
# file and writes what it did with each reply to a private copy of stdout.
# fd 1 and sys.stdout are pointed at stderr before the parser is loaded, so
# nothing the parser prints is read as a result. The parent does the comparing.
CHILD = r'''
import importlib.util, json, os, signal, sys
results = os.fdopen(os.dup(1), "w", encoding="utf-8")
os.dup2(2, 1)
sys.stdout = sys.stderr
replies = json.load(sys.stdin)["replies"]
spec = importlib.util.spec_from_file_location("action_parser_under_test", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def on_alarm(signum, frame):
    raise TimeoutError("reply took too long")

signal.signal(signal.SIGALRM, on_alarm)
outcomes = {}
for case in replies:
    signal.alarm(CASE_TIMEOUT)
    try:
        value = module.parse_action(case["reply"])
        json.dumps(value)
        outcome = {"kind": "action", "value": value}
    except TimeoutError:
        outcome = {"kind": "timeout"}
    except ValueError as e:
        outcome = {"kind": "refused", "error": type(e).__name__}
    except Exception as e:
        outcome = {"kind": "crash", "error": type(e).__name__}
    finally:
        signal.alarm(0)
    outcomes[case["id"]] = outcome
results.write(json.dumps({"outcomes": outcomes}))
results.close()
'''


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def tree_digest(directory):
    """sha256 over every file's relative path and sha256, skipping venvs and caches."""
    files = {}
    for current, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_DIRS)
        for name in sorted(names):
            if name.endswith((".pyc", ".log")) or name == "current_view.png":
                continue
            path = os.path.join(current, name)
            files[os.path.relpath(path, directory)] = file_sha256(path)
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(f"{rel}\0{files[rel]}\n".encode())
    return digest.hexdigest(), files


def load_corpus(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def matches(expect, outcome):
    """True if the outcome is what the contract asks for this reply."""
    if expect == "refuse":
        return outcome.get("kind") == "refused"
    value = outcome.get("value")
    if outcome.get("kind") != "action" or not isinstance(value, dict):
        return False
    if value.get("action") != expect["action"]:
        return False
    for key in REQUIRED_KEYS[expect["action"]]:
        if key not in value or type(value[key]) is not type(expect[key]) or value[key] != expect[key]:
            return False
    return True


def child_request(cases):
    """What the child is sent: each reply's id and text, never its expected answer."""
    return {"replies": [{"id": case["id"], "reply": case["reply"]} for case in cases]}


def child_command(parser_path, runtime=None, image=DEFAULT_IMAGE, name=None):
    """The command that runs CHILD on parser_path, on the host or in a one-shot container."""
    child = CHILD.replace("CASE_TIMEOUT", str(CASE_TIMEOUT_SECONDS))
    if not runtime:
        return [sys.executable, "-I", "-B", "-c", child, parser_path]
    parser_dir, parser_file = os.path.split(os.path.abspath(parser_path))
    return [
        runtime, "run", "--rm", "-i",
        "--name", name or f"seedling-measure-{secrets.token_hex(6)}",
        "--network", "none",
        "--cpus", "1",
        "--memory", "256m",
        "--pids-limit", "64",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "-v", f"{parser_dir}:/candidate:ro",
        image,
        "python", "-I", "-B", "-c", child, f"/candidate/{parser_file}",
    ]


def read_outcomes(stdout, cases):
    """The child's result document, checked against the replies it was sent. Raises ValueError."""
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        raise ValueError("parser process wrote no readable result") from None
    outcomes = document.get("outcomes") if isinstance(document, dict) else None
    if not isinstance(outcomes, dict) or set(outcomes) != {case["id"] for case in cases}:
        raise ValueError("parser process result does not cover exactly the replies sent")
    for outcome in outcomes.values():
        if not isinstance(outcome, dict) or outcome.get("kind") not in OUTCOME_KINDS:
            raise ValueError("parser process result has an unknown outcome")
    return outcomes


def score_parser(operator_dir, corpus_path, runtime=None, image=DEFAULT_IMAGE):
    """Run parse_action over the corpus in a child process and score each reply.

    With runtime (a Docker or Podman binary) the child runs in a container.
    """
    cases = load_corpus(corpus_path)
    parser_path = os.path.join(operator_dir, "action_parser.py")
    container_name = f"seedling-measure-{secrets.token_hex(6)}"
    command = child_command(parser_path, runtime, image, container_name)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    result = {"passed": 0, "total": len(cases), "error": None, "cases": []}
    try:
        proc = subprocess.run(
            command, input=json.dumps(child_request(cases)),
            capture_output=True, text=True, timeout=PARSER_TIMEOUT_SECONDS, env=env, cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        if runtime:
            # Killing the CLI does not stop the container.
            subprocess.run([runtime, "rm", "-f", container_name], capture_output=True, timeout=30, env=env)
        result["error"] = f"parser run exceeded {PARSER_TIMEOUT_SECONDS}s"
        return result
    except OSError as e:
        result["error"] = f"could not start the parser process: {e}"
        return result
    if proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no output"]
        result["error"] = f"parser process exited {proc.returncode}: {tail[0]}"
        return result
    try:
        outcomes = read_outcomes(proc.stdout, cases)
    except ValueError as e:
        result["error"] = str(e)
        return result
    for case in cases:
        outcome = outcomes[case["id"]]
        result["cases"].append({
            "id": case["id"],
            "class": case.get("class"),
            "source": case.get("source"),
            "pass": matches(case["expect"], outcome),
            "outcome": outcome,
        })
    result["passed"] = sum(1 for case in result["cases"] if case["pass"])
    return result


def compare(baseline, candidate):
    """Compare two score_parser results on the same corpus: totals, fixed and regressed reply ids."""
    if baseline["error"] or candidate["error"]:
        return {"status": "error", "reason": candidate["error"] or f"baseline: {baseline['error']}"}
    before = {case["id"]: case["pass"] for case in baseline["cases"]}
    after = {case["id"]: case["pass"] for case in candidate["cases"]}
    return {
        "status": "done",
        "baseline": {"passed": baseline["passed"], "total": baseline["total"]},
        "candidate": {"passed": candidate["passed"], "total": candidate["total"]},
        "fixed": sorted(i for i, ok in after.items() if ok and not before.get(i)),
        "regressed": sorted(i for i, ok in before.items() if ok and not after.get(i)),
    }


def run_tests():
    """Run scripts/test.sh and report how many tests ran and whether it passed."""
    try:
        proc = subprocess.run(
            ["bash", os.path.join(ROOT, "scripts", "test.sh")],
            capture_output=True, text=True, timeout=TEST_TIMEOUT_SECONDS, cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        return {"ran": 0, "ok": False, "exit": None, "tail": [f"timed out after {TEST_TIMEOUT_SECONDS}s"]}
    output = proc.stdout + proc.stderr
    ran = re.search(r"^Ran (\d+) tests?", output, re.MULTILINE)
    return {
        "ran": int(ran.group(1)) if ran else 0,
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "tail": output.strip().splitlines()[-3:],
    }


def summary_line(report):
    parse = report["parse"]
    percent = 100.0 * parse["passed"] / parse["total"] if parse["total"] else 0.0
    line = f"parse {parse['passed']}/{parse['total']} ({percent:.1f}%)"
    if parse["error"]:
        line += f" [error: {parse['error']}]"
    tests = report["tests"]
    if tests is None:
        line += " | tests skipped"
    else:
        line += f" | tests {tests['ran']} {'OK' if tests['ok'] else 'FAILED'}"
    return line


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score operator/ for a Seedling cycle.")
    parser.add_argument("--operator-dir", default=os.path.join(ROOT, "operator"))
    parser.add_argument("--corpus", default=DEFAULT_CORPUS)
    parser.add_argument("--label", default="")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--out", help="also write the full report as JSON to this file")
    parser.add_argument("--container-runtime", help="score the parser in a one-shot container run by this Docker or Podman binary")
    parser.add_argument("--image", default=DEFAULT_IMAGE, help="container image for --container-runtime")
    args = parser.parse_args(argv)

    operator_dir = os.path.abspath(args.operator_dir)
    tree, files = tree_digest(operator_dir)
    report = {
        "label": args.label,
        "measured_at": utc_now(),
        "python": sys.version.split()[0],
        "operator_dir": os.path.relpath(operator_dir, ROOT),
        "operator_tree_sha256": tree,
        "operator_files": files,
        "corpus": os.path.relpath(os.path.abspath(args.corpus), ROOT),
        "corpus_sha256": file_sha256(args.corpus),
        "parse": score_parser(operator_dir, os.path.abspath(args.corpus), args.container_runtime, args.image),
        "tests": None if args.skip_tests else run_tests(),
    }
    report["summary"] = summary_line(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
    print(report["summary"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
