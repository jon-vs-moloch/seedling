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
`python -I -B` process with a time limit per reply and overall. The corpus is
under tests/, outside operator/, so an applied artifact cannot change it.

Usage:
  python3 scripts/measure_cycle.py [--operator-dir DIR] [--corpus FILE]
                                   [--label TEXT] [--skip-tests] [--out FILE]
"""
import argparse
import hashlib
import json
import os
import re
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

# Runs in the child process. It loads parse_action from the given file and
# reports what it did with each reply; the parent does the comparing.
CHILD = r'''
import importlib.util, json, signal, sys
parser_path, corpus_path = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("action_parser_under_test", parser_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def on_alarm(signum, frame):
    raise TimeoutError("reply took too long")

signal.signal(signal.SIGALRM, on_alarm)
outcomes = {}
with open(corpus_path, encoding="utf-8") as f:
    for line in f:
        if not line.strip():
            continue
        case = json.loads(line)
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
print("SEEDLING-OUTCOMES " + json.dumps(outcomes))
'''
OUTCOME_MARKER = "SEEDLING-OUTCOMES "


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


def score_parser(operator_dir, corpus_path):
    """Run parse_action over the corpus in a child process and score each reply."""
    cases = load_corpus(corpus_path)
    parser_path = os.path.join(operator_dir, "action_parser.py")
    child = CHILD.replace("CASE_TIMEOUT", str(CASE_TIMEOUT_SECONDS))
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    result = {"passed": 0, "total": len(cases), "error": None, "cases": []}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-B", "-c", child, parser_path, corpus_path],
            capture_output=True, text=True, timeout=PARSER_TIMEOUT_SECONDS, env=env, cwd=ROOT,
        )
    except subprocess.TimeoutExpired:
        result["error"] = f"parser run exceeded {PARSER_TIMEOUT_SECONDS}s"
        return result
    lines = [line for line in proc.stdout.splitlines() if line.startswith(OUTCOME_MARKER)]
    if proc.returncode != 0 or not lines:
        tail = proc.stderr.strip().splitlines()[-1:] or ["no output"]
        result["error"] = f"parser process exited {proc.returncode}: {tail[0]}"
        return result
    outcomes = json.loads(lines[-1][len(OUTCOME_MARKER):])
    for case in cases:
        outcome = outcomes.get(case["id"], {"kind": "missing"})
        result["cases"].append({
            "id": case["id"],
            "class": case.get("class"),
            "source": case.get("source"),
            "pass": matches(case["expect"], outcome),
            "outcome": outcome,
        })
    result["passed"] = sum(1 for case in result["cases"] if case["pass"])
    return result


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
        "parse": score_parser(operator_dir, os.path.abspath(args.corpus)),
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
