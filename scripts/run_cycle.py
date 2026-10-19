#!/usr/bin/env python3
"""Drive one Seedling cycle through the real HTTP APIs, without a vision operator.

This stands in for Operator A: it gives ZenCode A a fixed task, asks it to
ship, waits for the supervisor to quarantine the artifact, and applies it
only when asked and only while operator/ and zencode/ have no uncommitted
changes (Apply copies over them with no backup). It calls the same endpoints
the UIs call; it never touches a sandbox or the source tree itself.

  run_cycle.py chat  --base-url URL --model ID --message-file F [--message-file F ...] --out DIR
  run_cycle.py ship  --out DIR
  run_cycle.py apply --id ID --sha256 HEX --out DIR

ZenCode A is http://127.0.0.1:8000 and the supervisor http://127.0.0.1:7000
unless --zencode or --supervisor say otherwise. Each step writes what it
sent and got back, with UTC times, as JSON under --out.
If a server does not answer, it exits 3 with a one-line error.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APPLY_TARGETS = ("operator", "zencode")


class Unreachable(Exception):
    """A server did not answer (not running, wrong port, or timed out)."""


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def call(method, url, body=None, timeout=60):
    """Send one JSON request; return (status, decoded body or raw text)."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read().decode()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read().decode()
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise Unreachable(f"cannot reach {url}: {reason}") from None
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw


def save(out_dir, name, payload):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    return path


def find_update(supervisor, key, value):
    _, body = call("GET", f"{supervisor}/api/updates")
    updates = body.get("updates", []) if isinstance(body, dict) else []
    return next((u for u in updates if u.get(key) == value), None)


def cmd_chat(args):
    record = {"zencode": args.zencode, "model": args.model, "messages": []}
    status, body = call("POST", f"{args.zencode}/api/init", {"base_url": args.base_url, "model_id": args.model})
    record["init"] = {"at": utc_now(), "status": status, "body": body}
    if status != 200:
        save(args.out, "chat.json", record)
        print(f"init failed: {status} {body}", file=sys.stderr)
        return 1
    for path in args.message_file:
        with open(path, encoding="utf-8") as f:
            message = f.read()
        sent_at = utc_now()
        status, body = call("POST", f"{args.zencode}/api/chat", {"message": message}, timeout=args.timeout)
        record["messages"].append({
            "file": os.path.basename(path), "sent_at": sent_at, "replied_at": utc_now(),
            "status": status, "reply": body,
        })
        save(args.out, "chat.json", record)
        print(f"{os.path.basename(path)}: HTTP {status}")
    _, record["logs"] = call("GET", f"{args.zencode}/api/logs?offset=0")
    _, record["files"] = call("GET", f"{args.zencode}/api/files")
    save(args.out, "chat.json", record)
    return 0


def cmd_ship(args):
    status, body = call("POST", f"{args.zencode}/api/ship_update", timeout=120)
    record = {"shipped_at": utc_now(), "status": status, "ship": body, "quarantine": None}
    if status != 200 or not isinstance(body, dict):
        save(args.out, "ship.json", record)
        print(f"ship failed: {status} {body}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        update = find_update(args.supervisor, "sha256", body.get("sha256"))
        if update:
            record["quarantine"] = update
            record["quarantine_seen_at"] = utc_now()
            break
        time.sleep(2)
    save(args.out, "ship.json", record)
    update = record["quarantine"]
    if not update:
        print(f"artifact {body.get('sha256')} not quarantined within {args.wait}s", file=sys.stderr)
        return 1
    print(f"quarantined: id {update['id']} sha256 {update['sha256']} status {update['status']} target {update['target_codebase']}/")
    return 0 if update.get("status") == "quarantined" else 1


def uncommitted(paths):
    out = subprocess.run(
        ["git", "-C", ROOT, "status", "--porcelain", "--", *paths],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.strip()


def cmd_apply(args):
    dirty = uncommitted(APPLY_TARGETS)
    if dirty:
        print("Refusing to apply: commit operator/ and zencode/ first (Apply has no backup).\n" + dirty, file=sys.stderr)
        return 2
    update = find_update(args.supervisor, "id", args.id)
    if not update or update.get("sha256") != args.sha256 or update.get("status") != "quarantined":
        print(f"Refusing to apply: no quarantined update {args.id} with sha256 {args.sha256}", file=sys.stderr)
        return 2
    head = subprocess.run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    record = {"head_before_apply": head, "requested_at": utc_now(), "update_before": update}
    status, body = call("POST", f"{args.supervisor}/api/updates/{args.id}/apply", timeout=300)
    record["apply"] = {"at": utc_now(), "status": status, "body": body}
    record["update_after"] = find_update(args.supervisor, "id", args.id)
    save(args.out, "apply.json", record)
    after = record["update_after"] or {}
    print(f"apply: HTTP {status}, update status {after.get('status')}")
    return 0 if status == 200 and after.get("status") == "applied" else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Drive one Seedling cycle through the HTTP APIs.")
    parser.add_argument("--zencode", default="http://127.0.0.1:8000")
    parser.add_argument("--supervisor", default="http://127.0.0.1:7000")
    commands = parser.add_subparsers(dest="command", required=True)

    chat = commands.add_parser("chat", help="initialize ZenCode and send it the task")
    chat.add_argument("--base-url", required=True, help="OpenAI-compatible endpoint for ZenCode")
    chat.add_argument("--model", required=True)
    chat.add_argument("--message-file", action="append", required=True)
    chat.add_argument("--timeout", type=int, default=1800, help="seconds to wait for each chat reply")
    chat.add_argument("--out", required=True)

    ship = commands.add_parser("ship", help="press Ship Update and wait for quarantine")
    ship.add_argument("--wait", type=int, default=60)
    ship.add_argument("--out", required=True)

    apply = commands.add_parser("apply", help="apply one quarantined update")
    apply.add_argument("--id", required=True)
    apply.add_argument("--sha256", required=True)
    apply.add_argument("--out", required=True)

    args = parser.parse_args(argv)
    args.zencode = args.zencode.rstrip("/")
    args.supervisor = args.supervisor.rstrip("/")
    try:
        return {"chat": cmd_chat, "ship": cmd_ship, "apply": cmd_apply}[args.command](args)
    except Unreachable as e:
        print(f"{e}. Is the Seedling stack running?", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
