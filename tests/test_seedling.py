"""Smoke tests for Seedling's sandbox boundaries, quarantine checks and UI assets.

Run with scripts/test.sh (creates a throwaway venv with the server deps).
These tests never start the stack, a container or a model.
"""
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path, cwd):
    previous = os.getcwd()
    os.chdir(cwd)  # zencode/app.py mounts "static" relative to the working directory
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous)


class ZenCodeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        cls.tmp = tempfile.TemporaryDirectory()
        cls.sandbox = Path(cls.tmp.name) / "sandbox"
        cls.sandbox.mkdir()
        os.environ["ZENCODE_SANDBOX_DIR"] = str(cls.sandbox)
        cls.zen = load_module("zencode_app", ROOT / "zencode" / "app.py", ROOT / "zencode")
        cls.client = TestClient(cls.zen.app, base_url="http://127.0.0.1:8000")

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("ZENCODE_SANDBOX_DIR", None)
        cls.tmp.cleanup()

    def test_files_lists_sandbox_and_skips_venvs_and_links(self):
        (self.sandbox / "src").mkdir(exist_ok=True)
        (self.sandbox / "src" / "main.py").write_text("print('hi')\n")
        (self.sandbox / "venv").mkdir(exist_ok=True)
        (self.sandbox / "venv" / "ignored.py").write_text("x")
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("secret")
        link = self.sandbox / "escape.txt"
        if not link.exists():
            link.symlink_to(outside)

        payload = self.client.get("/api/files").json()
        paths = [entry["path"] for entry in payload["files"]]
        self.assertIn(os.path.join("src", "main.py"), paths)
        self.assertNotIn("escape.txt", paths)
        self.assertFalse(any(p.startswith("venv") for p in paths))
        self.assertFalse(payload["truncated"])

    def test_status_reports_container_mode_by_default(self):
        status = self.client.get("/api/status").json()
        self.assertEqual(status["sandbox_mode"], os.environ.get("SEEDLING_SANDBOX_MODE", "container"))
        self.assertIn("network", status)

    def test_chat_before_init_refuses_without_touching_history(self):
        self.zen.sandbox_state["base_url"] = None
        self.zen.sandbox_state["model_id"] = None
        before = len(self.zen.sandbox_state["messages"])
        reply = self.client.post("/api/chat", json={"message": "hello"}).json()
        self.assertIn("Not initialized", reply["response"])
        self.assertEqual(len(self.zen.sandbox_state["messages"]), before)

    def test_path_boundary_rejects_absolute_and_traversal(self):
        with self.assertRaises(ValueError):
            self.zen.resolve_sandbox_path("/etc/passwd")
        with self.assertRaises(ValueError):
            self.zen.resolve_sandbox_path("../outside.txt")
        self.assertTrue(self.zen.resolve_sandbox_path("ok.txt").startswith(os.path.realpath(self.sandbox)))

    def test_status_hides_userinfo_in_the_endpoint_url(self):
        previous = self.zen.sandbox_state["base_url"]
        self.zen.sandbox_state["base_url"] = "http://user:hunter2@127.0.0.1:1234/v1"
        try:
            status = self.client.get("/api/status").json()
            self.assertEqual(status["base_url"], "http://127.0.0.1:1234/v1")
            self.assertNotIn("hunter2", json.dumps(status))
        finally:
            self.zen.sandbox_state["base_url"] = previous

    def test_chat_errors_hide_the_endpoint_url_and_host(self):
        import httpx

        state = self.zen.sandbox_state
        previous = (state["base_url"], state["model_id"], list(state["messages"]))
        state["base_url"] = "http://user:hunter2@inference.internal.example:4321"
        state["model_id"] = "m"
        error = httpx.ConnectError(
            "Failed to reach http://user:hunter2@inference.internal.example:4321/v1/chat/completions "
            "(host INFERENCE.internal.example refused; redirected to https://redirect.example/x?key=s3cret)"
        )
        try:
            logs_before = len(state["logs"])
            with mock.patch.object(self.zen.httpx.AsyncClient, "post", side_effect=error):
                reply = self.client.post("/api/chat", json={"message": "hi"}).json()
            logged = json.dumps(state["logs"][logs_before:])
            for text in (reply["response"], logged):
                self.assertIn("ConnectError", text)
                for secret in ("hunter2", "inference.internal.example", "4321", "user:", "redirect.example", "s3cret"):
                    self.assertNotIn(secret, text.lower())
        finally:
            state["base_url"], state["model_id"], state["messages"][:] = previous

    def test_files_does_not_create_a_missing_sandbox(self):
        missing = Path(self.tmp.name) / "not-created"
        previous = self.zen.sandbox_state["sandbox_dir"]
        self.zen.sandbox_state["sandbox_dir"] = None
        try:
            with mock.patch.dict(os.environ, {"ZENCODE_SANDBOX_DIR": str(missing)}):
                payload = self.client.get("/api/files").json()
            self.assertEqual(payload, {"files": [], "truncated": False})
            self.assertFalse(missing.exists())
        finally:
            self.zen.sandbox_state["sandbox_dir"] = previous

    def test_files_skips_a_file_removed_during_the_listing(self):
        (self.sandbox / "gone.txt").write_text("x")
        with mock.patch.object(self.zen.os.path, "getsize", side_effect=FileNotFoundError):
            response = self.client.get("/api/files")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["files"], [])

    def test_cross_site_and_rebound_requests_are_refused(self):
        self.zen.sandbox_state["base_url"] = None
        self.zen.sandbox_state["model_id"] = None
        evil = self.client.post("/api/chat", json={"message": "x"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(evil.status_code, 403)
        stripped = self.client.post("/api/chat", json={"message": "x"}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(stripped.status_code, 403)
        other_port = self.client.post("/api/chat", json={"message": "x"}, headers={"Origin": "http://127.0.0.1:9999"})
        self.assertEqual(other_port.status_code, 403)
        rebound = self.client.get("/api/status", headers={"Host": "rebind.attacker.example:8000"})
        self.assertEqual(rebound.status_code, 403)
        # The page's own origin and Origin-less scripts are allowed.
        own = self.client.post("/api/chat", json={"message": "x"}, headers={"Origin": "http://127.0.0.1:8000"})
        self.assertEqual(own.status_code, 200)
        self.assertEqual(self.client.post("/api/chat", json={"message": "x"}).status_code, 200)


class OperatorGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        cls.tmp = tempfile.TemporaryDirectory()
        # Playwright is not installed in the test venv; block it anyway so the
        # test proves operator/main.py imports without it. Only these two
        # sys.modules entries are touched (patch.dict would also drop every
        # module first imported during the load).
        blocked = ("playwright", "playwright.async_api")
        saved = {name: sys.modules[name] for name in blocked if name in sys.modules}
        sys.modules.update(dict.fromkeys(blocked))
        try:
            with mock.patch.dict(os.environ, {"OPERATOR_WORK_DIR": cls.tmp.name}):
                cls.op_module = load_module("operator_main_guard", ROOT / "operator" / "main.py", ROOT / "operator")
        finally:
            for name in blocked:
                sys.modules.pop(name, None)
            sys.modules.update(saved)
        # No `with`: the startup hook (which launches Playwright and the loop) does not run.
        cls.client = TestClient(cls.op_module.app, base_url="http://127.0.0.1:9000")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_cross_site_posts_are_refused(self):
        # An unknown route, so a guard regression gets a harmless 404.
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Origin": "http://127.0.0.1:9001"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            response = self.client.post("/api/nope", headers=headers)
            self.assertEqual(response.status_code, 403, headers)

    def test_rebound_host_is_refused(self):
        self.assertEqual(self.client.get("/api/state", headers={"Host": "rebind.attacker.example:9000"}).status_code, 403)
        self.assertEqual(self.client.get("/api/state", headers={"Host": "127.0.0.1:9001"}).status_code, 403)
        self.assertEqual(self.client.get("/api/state").status_code, 200)

    def test_own_page_and_scripts_pass_the_guard(self):
        own = self.client.post("/api/nope", headers={"Origin": "http://127.0.0.1:9000"})
        self.assertEqual(own.status_code, 404)
        self.assertEqual(self.client.post("/api/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/control", json={}).json(), {"ok": True})


class SupervisorQuarantineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sup_module = load_module("supervisor_main", ROOT / "supervisor" / "main.py", ROOT / "supervisor")

    def make_tar(self, members):
        handle = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        with tarfile.open(fileobj=handle, mode="w") as archive:
            for name, kind in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    data = b"content"
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    archive.addfile(info)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_accepts_plain_files(self):
        result = self.sup_module.Supervisor()._validate_archive(self.make_tar([("app.py", "file")]))
        self.assertEqual(result["members"], 1)

    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            self.sup_module.Supervisor()._validate_archive(self.make_tar([("../evil.py", "file")]))

    def test_rejects_symlinks(self):
        with self.assertRaises(ValueError):
            self.sup_module.Supervisor()._validate_archive(self.make_tar([("link", "symlink")]))

    def test_rejects_non_tar(self):
        handle = tempfile.NamedTemporaryFile(suffix=".tar", delete=False)
        handle.write(b"not a tar")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        with self.assertRaises(ValueError):
            self.sup_module.Supervisor()._validate_archive(handle.name)


class SupervisorGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        cls.sup_module = load_module("supervisor_main_guard", ROOT / "supervisor" / "main.py", ROOT / "supervisor")
        # No `with`: the startup hook (which launches the stack) does not run.
        cls.client = TestClient(cls.sup_module.app, base_url="http://127.0.0.1:7000")

    def test_cross_site_posts_are_refused(self):
        # An unknown process name, so a guard regression gets a harmless 404
        # instead of resetting (and launching) a real stack.
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}):
            response = self.client.post("/api/process/nope/start", headers=headers)
            self.assertEqual(response.status_code, 403, headers)

    def test_rebound_host_is_refused(self):
        self.assertEqual(self.client.get("/api/safety", headers={"Host": "rebind.attacker.example:7000"}).status_code, 403)
        self.assertEqual(self.client.get("/api/safety", headers={"Host": "127.0.0.1:7001"}).status_code, 403)
        self.assertEqual(self.client.get("/api/safety").status_code, 200)

    def test_own_page_and_scripts_pass_the_guard(self):
        # An unknown process name gets past the guard and is answered 404 by the route.
        own = self.client.post("/api/process/nope/start", headers={"Origin": "http://127.0.0.1:7000"})
        self.assertEqual(own.status_code, 404)
        self.assertEqual(self.client.post("/api/process/nope/start").status_code, 404)


class SupervisorProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sup_module = load_module("supervisor_main_proc", ROOT / "supervisor" / "main.py", ROOT / "supervisor")

    def make_supervisor(self, cmd, base_dir):
        sup = self.sup_module.Supervisor()
        sup.base_dir = base_dir
        sup.processes["child"] = self.sup_module.ManagedProcess(
            name="child", role="zencode", port=0, cwd=str(ROOT), cmd=cmd,
        )
        self.addCleanup(sup.stop_process, "child")
        return sup

    def test_failed_start_closes_the_log_file(self):
        with tempfile.TemporaryDirectory() as base_dir:
            sup = self.make_supervisor([os.path.join(base_dir, "no-such-binary")], base_dir)
            # Keep a reference to every file start_process opens, so garbage
            # collection cannot close a leaked handle on the code's behalf.
            opened = []

            def tracking_open(*args, **kwargs):
                handle = open(*args, **kwargs)
                opened.append(handle)
                return handle

            with mock.patch.object(self.sup_module, "open", tracking_open, create=True):
                with self.assertRaises(OSError):
                    sup.start_process("child")
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].closed)
            self.assertIsNone(sup.processes["child"].log_file)
            self.assertIsNone(sup.processes["child"].process)

    def test_starting_a_running_process_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as base_dir:
            sup = self.make_supervisor([sys.executable, "-c", "import time; time.sleep(30)"], base_dir)
            sup.start_process("child")
            first = sup.processes["child"].process
            first_log = sup.processes["child"].log_file
            sup.start_process("child")
            self.assertIs(sup.processes["child"].process, first)
            self.assertIs(sup.processes["child"].log_file, first_log)
            sup.stop_process("child")
            self.assertIsNone(sup.processes["child"].process)
            self.assertTrue(first_log.closed)

    def test_fallback_log_path_is_per_supervisor(self):
        sup = self.make_supervisor([sys.executable, "-c", "pass"], None)
        sup.start_process("child")
        log_name = os.path.basename(sup.processes["child"].log_file.name)
        sup.stop_process("child")
        self.addCleanup(os.unlink, os.path.join(tempfile.gettempdir(), log_name))
        self.assertEqual(log_name, f"seedling-{os.getpid()}-child.log")


class SupervisorSeedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sup_module = load_module("supervisor_main_seed", ROOT / "supervisor" / "main.py", ROOT / "supervisor")

    def test_lane_a_seed_is_the_operator_docs_and_parser(self):
        source_dir, names = self.sup_module.SANDBOX_A_SEED
        self.assertEqual(Path(source_dir), ROOT / "operator")
        self.assertEqual(tuple(names), ("README.md", "action_parser.py"))
        for name in names:
            self.assertTrue((ROOT / "operator" / name).is_file(), name)

    def test_seed_copies_named_regular_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "src"
            sandbox = Path(tmp) / "sandbox"
            source.mkdir()
            sandbox.mkdir()
            (source / "README.md").write_text("docs")
            (source / "other.py").write_text("not named")
            (Path(tmp) / "secret.txt").write_text("secret")
            (source / "link.py").symlink_to(Path(tmp) / "secret.txt")
            seeded = self.sup_module.Supervisor()._seed_sandbox(
                str(sandbox), str(source), ("README.md", "link.py", "missing.py", "../secret.txt"),
            )
            self.assertEqual(seeded, ["README.md"])
            self.assertEqual(sorted(p.name for p in sandbox.iterdir()), ["README.md"])
            self.assertEqual((sandbox / "README.md").read_text(), "docs")


class MeasureCycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.measure = load_module("measure_cycle", ROOT / "scripts" / "measure_cycle.py", ROOT)

    def write_parser(self, tmp, source):
        operator_dir = Path(tmp) / "operator"
        operator_dir.mkdir()
        (operator_dir / "action_parser.py").write_text(source)
        return str(operator_dir)

    def write_corpus(self, tmp, cases):
        corpus = Path(tmp) / "corpus.jsonl"
        corpus.write_text("".join(json.dumps(case) + "\n" for case in cases), encoding="utf-8")
        return str(corpus)

    def test_corpus_is_well_formed(self):
        cases = self.measure.load_corpus(self.measure.DEFAULT_CORPUS)
        self.assertGreaterEqual(len(cases), 20)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        for case in cases:
            self.assertIsInstance(case["reply"], str, case["id"])
            self.assertIn("class", case)
            self.assertIn("source", case)
            expect = case["expect"]
            if expect == "refuse":
                continue
            self.assertIn(expect["action"], self.measure.REQUIRED_KEYS, case["id"])
            self.assertEqual(set(expect), {"action", *self.measure.REQUIRED_KEYS[expect["action"]]}, case["id"])

    def test_scores_each_reply_against_the_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator_dir = self.write_parser(tmp, (
                "import json\n"
                "def parse_action(reply):\n"
                "    if reply == 'boom':\n"
                "        raise KeyError('boom')\n"
                "    return json.loads(reply)\n"
            ))
            corpus = self.write_corpus(tmp, [
                {"id": "ok", "class": "t", "source": "t", "reply": '{"action": "wait", "ms": 5, "why": "x"}', "expect": {"action": "wait", "ms": 5}},
                {"id": "wrong-type", "class": "t", "source": "t", "reply": '{"action": "wait", "ms": "5"}', "expect": {"action": "wait", "ms": 5}},
                {"id": "refused", "class": "t", "source": "t", "reply": "nope", "expect": "refuse"},
                {"id": "crash", "class": "t", "source": "t", "reply": "boom", "expect": "refuse"},
                {"id": "accepted-junk", "class": "t", "source": "t", "reply": '{"action": "click"}', "expect": "refuse"},
            ])
            result = self.measure.score_parser(operator_dir, corpus)
        self.assertIsNone(result["error"])
        verdicts = {case["id"]: case["pass"] for case in result["cases"]}
        self.assertEqual(verdicts, {"ok": True, "wrong-type": False, "refused": True, "crash": False, "accepted-junk": False})
        self.assertEqual((result["passed"], result["total"]), (2, 5))

    def test_a_hanging_reply_is_cut_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator_dir = self.write_parser(tmp, (
                "def parse_action(reply):\n"
                "    while reply == 'hang':\n"
                "        pass\n"
                "    return {'action': 'done'}\n"
            ))
            corpus = self.write_corpus(tmp, [
                {"id": "hang", "class": "t", "source": "t", "reply": "hang", "expect": {"action": "done"}},
                {"id": "fine", "class": "t", "source": "t", "reply": "x", "expect": {"action": "done"}},
            ])
            result = self.measure.score_parser(operator_dir, corpus)
        outcomes = {case["id"]: case["outcome"]["kind"] for case in result["cases"]}
        self.assertEqual(outcomes, {"hang": "timeout", "fine": "action"})
        self.assertEqual(result["passed"], 1)

    def test_a_parser_that_fails_to_import_scores_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            operator_dir = self.write_parser(tmp, "this is not python\n")
            corpus = self.write_corpus(tmp, [
                {"id": "a", "class": "t", "source": "t", "reply": "{}", "expect": "refuse"},
            ])
            result = self.measure.score_parser(operator_dir, corpus)
        self.assertEqual(result["passed"], 0)
        self.assertIn("exited", result["error"])


class SupervisorEnvTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sup_module = load_module("supervisor_main_env", ROOT / "supervisor" / "main.py", ROOT / "supervisor")

    def test_container_runtime_and_caps_reach_zencode(self):
        settings = {
            "SEEDLING_CONTAINER_RUNTIME": "/opt/example/bin/docker",
            "SEEDLING_SANDBOX_CPUS": "2",
            "SEEDLING_SANDBOX_MEMORY": "1g",
            "SEEDLING_SANDBOX_PIDS": "64",
            "SEEDLING_SANDBOX_TIMEOUT": "45",
        }
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"SEEDLING_RUNTIME_DIR": tmp, **settings}):
                sup = self.sup_module.Supervisor()
                with mock.patch.object(sup, "_ensure_venv"):
                    sup.initialize()
                for name in ("zencode_a", "zencode_b"):
                    env = sup._process_env(sup.processes[name])
                    for key, value in settings.items():
                        self.assertEqual(env.get(key), value, (name, key))
                    self.assertEqual(env["ZENCODE_SANDBOX_DIR"], sup.processes[name].sandbox_dir)
                # Operators run no containers and get none of these.
                self.assertNotIn("SEEDLING_CONTAINER_RUNTIME", sup._process_env(sup.processes["operator_a"]))

    def test_unset_settings_are_not_forwarded(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in self.sup_module.ZENCODE_FORWARDED_ENV:
                os.environ.pop(name, None)
            env = self.sup_module.zencode_env("/sandbox")
        for name in self.sup_module.ZENCODE_FORWARDED_ENV:
            self.assertNotIn(name, env)
        self.assertEqual(env["ZENCODE_SANDBOX_DIR"], "/sandbox")


class SupervisorTimeTests(unittest.TestCase):
    UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    @classmethod
    def setUpClass(cls):
        cls.sup_module = load_module("supervisor_main_time", ROOT / "supervisor" / "main.py", ROOT / "supervisor")

    def test_receipts_and_logs_are_utc(self):
        sup = self.sup_module.Supervisor()
        with tempfile.TemporaryDirectory() as tmp:
            sup.quarantine_dir = tmp
            artifact = Path(tmp) / "update.tar"
            with tarfile.open(artifact, "w") as archive:
                info = tarfile.TarInfo("app.py")
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
            update = sup.quarantine_artifact("zencode_b", "zencode", str(artifact))
        self.assertRegex(update["detected_at"], self.UTC)
        self.assertTrue(sup.logs)
        for entry in sup.logs:
            self.assertRegex(entry["ts"], self.UTC)

    def test_no_naive_local_time_in_the_supervisor(self):
        source = (ROOT / "supervisor" / "main.py").read_text()
        self.assertNotIn("datetime.now()", source)


class RunCycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.run_cycle = load_module("run_cycle", ROOT / "scripts" / "run_cycle.py", ROOT)

    def test_a_stopped_server_fails_clearly(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # Nothing listens on the port now, so the connection is refused.
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                code = self.run_cycle.main(["--zencode", f"http://127.0.0.1:{port}", "ship", "--out", tmp])
        self.assertEqual(code, 3)
        self.assertIn(f"cannot reach http://127.0.0.1:{port}/api/ship_update", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


# A stand-in for `docker run`, so container paths can be tested without a
# container runtime. It maps each `-v HOST:CONTAINER` mount, runs the command
# after the image on the host, and records its arguments. Modes: "ok" (the
# mount works), "empty" (the runtime can't see the host path, so the mount is
# an empty directory: what Colima does outside $HOME) and "copy" (the
# container sees the files, but its writes never reach the host).
FAKE_RUNTIME = r'''#!{python}
import json, os, shutil, subprocess, sys, tempfile
MODE, LOG, IMAGE = {mode!r}, {log!r}, "fake-image"
args = sys.argv[1:]
with open(LOG, "a") as f:
    f.write(json.dumps(args) + "\n")
if args[0] != "run":
    sys.exit(0)
at = args.index(IMAGE)
options, command = args[:at], args[at + 1:]
mounts, workdir, scratch = {{}}, None, []
for i, arg in enumerate(options):
    if arg == "-v":
        host, inside = options[i + 1].split(":")[:2]
        if MODE != "ok":
            fake = tempfile.mkdtemp()
            scratch.append(fake)
            if MODE == "copy":
                shutil.copytree(host, fake, dirs_exist_ok=True)
            host = fake
        mounts[inside] = host
    if arg == "-w":
        workdir = options[i + 1]

def mapped(arg):
    for inside, host in mounts.items():
        if arg == inside or arg.startswith(inside + "/"):
            return host + arg[len(inside):]
    return arg

command = [sys.executable if a == "python" else "-c" if a == "-lc" else mapped(a) for a in command]
try:
    code = subprocess.run(command, cwd=mapped(workdir) if workdir else None).returncode
finally:
    for path in scratch:
        shutil.rmtree(path, ignore_errors=True)
sys.exit(code)
'''


def make_fake_runtime(directory, mode):
    """Write a fake container runtime; return (its path, the file it logs its calls to)."""
    path = Path(directory) / f"fake-runtime-{mode}"
    log = Path(directory) / f"fake-runtime-{mode}.log"
    path.write_text(FAKE_RUNTIME.format(python=sys.executable, mode=mode, log=str(log)))
    path.chmod(0o755)
    return str(path), log


def runtime_calls(log):
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


class ZenCodeContainerTests(unittest.TestCase):
    """The mount check (defect 1), the runtime setting (defect 2) and the ship policy (defect 3)."""

    @classmethod
    def setUpClass(cls):
        cls.zen = load_module("zencode_app_container", ROOT / "zencode" / "app.py", ROOT / "zencode")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.sandbox = self.tmp / "sandbox"
        self.sandbox.mkdir()
        state = self.zen.sandbox_state
        saved = dict(state)
        self.addCleanup(state.update, saved)
        state["sandbox_dir"] = str(self.sandbox)
        self.zen.verified_mounts.clear()
        self.addCleanup(self.zen.verified_mounts.clear)

    def use_runtime(self, mode):
        runtime, log = make_fake_runtime(self.tmp, mode)
        for name, value in (("SANDBOX_MODE", "container"), ("SANDBOX_RUNTIME", runtime), ("SANDBOX_IMAGE", "fake-image")):
            patcher = mock.patch.object(self.zen, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return log

    def assert_no_probe_files(self):
        self.assertEqual([p.name for p in self.sandbox.iterdir() if p.name.startswith(".seedling-")], [])

    def test_an_unmounted_sandbox_fails_closed(self):
        log = self.use_runtime("empty")
        result = self.zen.run_bash("echo hi > made.txt")
        self.assertIn("Container mount check failed", result["error"])
        self.assertIn("SEEDLING_RUNTIME_DIR", result["error"])
        # Only the probe ran; the agent's command never did.
        self.assertEqual(len(runtime_calls(log)), 1)
        self.assertFalse((self.sandbox / "made.txt").exists())
        self.assert_no_probe_files()
        # It is not remembered as checked: the next command checks again.
        self.zen.run_bash("true")
        self.assertEqual(len(runtime_calls(log)), 2)

    def test_container_writes_that_never_reach_the_host_fail_closed(self):
        (self.sandbox / "action_parser.py").write_text("x = 1\n")
        log = self.use_runtime("copy")
        result = self.zen.run_bash("ls")
        self.assertIn("Container mount check failed", result["error"])
        self.assertEqual(len(runtime_calls(log)), 1)
        self.assert_no_probe_files()

    def test_a_working_mount_is_checked_once_then_commands_run(self):
        log = self.use_runtime("ok")
        result = self.zen.run_bash("echo hi > made.txt")
        self.assertEqual(result.get("returncode"), 0, result)
        self.assertEqual((self.sandbox / "made.txt").read_text(), "hi\n")
        self.zen.run_bash("true")
        calls = runtime_calls(log)
        self.assertEqual(len(calls), 3)  # probe, command, command
        self.assertIn(".seedling-mount-probe-", calls[0][-1])
        for call in calls:
            self.assertEqual(call[call.index("--network") + 1], self.zen.SANDBOX_NETWORK)
            self.assertIn("--read-only", call)
        self.assert_no_probe_files()

    def test_init_reports_a_broken_mount(self):
        from fastapi.testclient import TestClient

        self.use_runtime("empty")
        client = TestClient(self.zen.app, base_url="http://127.0.0.1:8000")
        response = client.post("/api/init", json={"base_url": "http://127.0.0.1:1", "model_id": "m"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("Container mount check failed", response.json()["error"])
        self.assert_no_probe_files()

    def test_container_runtime_setting_is_used(self):
        runtime, _ = make_fake_runtime(self.tmp, "ok")
        with mock.patch.dict(os.environ, {"SEEDLING_CONTAINER_RUNTIME": runtime}):
            fresh = load_module("zencode_app_runtime", ROOT / "zencode" / "app.py", ROOT / "zencode")
        self.assertEqual(fresh.container_runtime(), runtime)

    def test_ship_leaves_out_scratch_and_reports_what_shipped(self):
        files = {
            "action_parser.py": "x = 1\n",
            "README.md": "docs\n",
            "scratch/check_parser.py": "print(1)\n",
            "scratch/examples.txt": "{}\n",
            "notes.tmp": "x",
            "lib/__pycache__/a.cpython-312.pyc": "x",
            "lib/helper.py": "y = 2\n",
            "run.log": "x",
            ".seedling-mount-probe-0": "x",
        }
        for rel, text in files.items():
            (self.sandbox / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.sandbox / rel).write_text(text)
        from fastapi.testclient import TestClient

        client = TestClient(self.zen.app, base_url="http://127.0.0.1:8000")
        body = client.post("/api/ship_update").json()
        self.assertEqual(body["files"], ["README.md", "action_parser.py", "lib/helper.py"])
        excluded = {e["path"] for e in body["excluded"]}
        self.assertEqual(excluded, {".seedling-mount-probe-0", "notes.tmp", "run.log", "scratch/", "lib/__pycache__/"})
        with tarfile.open(self.sandbox / "outbox" / body["artifact"]) as archive:
            self.assertEqual(sorted(archive.getnames()), body["files"])

    def test_system_prompt_names_the_scratch_dir(self):
        self.assertIn("scratch/", self.zen.SYSTEM_PROMPT)


class ArtifactPolicyTests(unittest.TestCase):
    """Quarantine refuses scratch paths and reports files new to the target (defect 3)."""

    @classmethod
    def setUpClass(cls):
        cls.sup_module = load_module("supervisor_main_policy", ROOT / "supervisor" / "main.py", ROOT / "supervisor")
        cls.zen = load_module("zencode_app_policy", ROOT / "zencode" / "app.py", ROOT / "zencode")

    def make_tar(self, names):
        handle = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        with tarfile.open(handle.name, "w:gz") as archive:
            for name in names:
                info = tarfile.TarInfo(name)
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
        return handle.name

    def test_zencode_and_supervisor_share_one_policy(self):
        self.assertEqual(self.zen.ARTIFACT_EXCLUDED_DIRS, self.sup_module.ARTIFACT_EXCLUDED_DIRS)
        self.assertEqual(self.zen.ARTIFACT_EXCLUDED_FILES, self.sup_module.ARTIFACT_EXCLUDED_FILES)

    def test_quarantine_refuses_paths_that_never_ship(self):
        sup = self.sup_module.Supervisor()
        for name in ("scratch/check_parser.py", "notes.tmp", "deep/tmp/x.py", ".seedling-mount-probe-1", "a/__pycache__/m.pyc"):
            with self.assertRaises(ValueError, msg=name):
                sup._validate_archive(self.make_tar(["action_parser.py", name]))

    def test_cycle_one_scratch_files_are_reported(self):
        # The artifact cycle 1 applied: the parser plus three scratch files.
        names = ["README.md", "action_parser.py", "check_parser.py", "examples.txt", "parse_action.py"]
        result = self.sup_module.Supervisor()._validate_archive(self.make_tar(names), str(ROOT / "operator"))
        self.assertEqual(result["files"], sorted(names))
        self.assertEqual(
            [u["path"] for u in result["unexpected_files"]],
            ["check_parser.py", "examples.txt", "parse_action.py"],
        )
        self.assertTrue(all("new file" in u["reason"] for u in result["unexpected_files"]))

    def test_a_refused_artifact_shows_in_the_quarantine_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self.sup_module.Supervisor()
            sup.quarantine_dir = os.path.join(tmp, "quarantine")
            os.makedirs(sup.quarantine_dir)
            for name in ("zencode_a", "zencode_b"):
                sandbox = os.path.join(tmp, name)
                os.makedirs(os.path.join(sandbox, "outbox"))
                sup.processes[name] = self.sup_module.ManagedProcess(name, "zencode", 0, tmp, [], sandbox_dir=sandbox)
            bad = self.make_tar(["action_parser.py", "scratch/x.py"])
            shutil.copyfile(bad, os.path.join(tmp, "zencode_a", "outbox", "update.tar.gz"))
            updates = sup.poll_outboxes()
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["status"], "rejected")
        self.assertIn("never ships", updates[0]["error"])
        # A refused artifact has no Apply.
        sup.update_history.extend(updates)
        with self.assertRaises(ValueError):
            sup.apply_update(updates[0]["id"])


class CandidateScoringTests(unittest.TestCase):
    """Pre-apply scoring in a container (defect 4) and the hardened measurement (F-03)."""

    @classmethod
    def setUpClass(cls):
        cls.sup_module = load_module("supervisor_main_score", ROOT / "supervisor" / "main.py", ROOT / "supervisor")
        cls.measure = cls.sup_module.measure

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def quarantined(self, parser_source, extra=()):
        sup = self.sup_module.Supervisor()
        sup.base_dir = str(self.tmp / "stack")
        sup.quarantine_dir = os.path.join(sup.base_dir, "quarantine")
        os.makedirs(sup.quarantine_dir)
        artifact = self.tmp / "update.tar.gz"
        with tarfile.open(artifact, "w:gz") as archive:
            for name, text in (("action_parser.py", parser_source), *extra):
                data = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        with mock.patch.object(sup, "_start_scoring") as start:
            update = sup.quarantine_artifact("zencode_a", "operator", str(artifact))
        start.assert_called_once_with(update)
        self.assertEqual(update["score"], {"status": "scoring"})
        return sup, update

    def score(self, sup, update, runtime):
        with mock.patch.object(self.sup_module, "sandbox_runtime", return_value=runtime), \
                mock.patch.dict(os.environ, {"SEEDLING_SANDBOX_IMAGE": "fake-image"}):
            sup._score_candidate(update)
        return update["score"]

    def test_scores_the_candidate_against_operator_now_without_applying(self):
        operator_parser = (ROOT / "operator" / "action_parser.py").read_bytes()
        sup, update = self.quarantined("def parse_action(reply):\n    raise ValueError('no')\n")
        runtime, log = make_fake_runtime(self.tmp, "ok")
        score = self.score(sup, update, runtime)
        self.assertEqual(score["status"], "done", score)
        corpus = self.measure.load_corpus(self.measure.DEFAULT_CORPUS)
        refusals = sum(1 for case in corpus if case["expect"] == "refuse")
        now = self.measure.score_parser(str(ROOT / "operator"), self.measure.DEFAULT_CORPUS)
        self.assertEqual(score["baseline"], {"passed": now["passed"], "total": len(corpus)})
        self.assertEqual(score["candidate"], {"passed": refusals, "total": len(corpus)})
        self.assertTrue(score["regressed"])
        self.assertTrue(score["parser_shipped"])
        self.assertRegex(score["measured_at"], r"Z$")
        # Read-only: still quarantined, operator/ untouched, scratch removed.
        self.assertEqual(update["status"], "quarantined")
        self.assertEqual((ROOT / "operator" / "action_parser.py").read_bytes(), operator_parser)
        self.assertEqual(sorted(os.listdir(sup.base_dir)), ["quarantine"])
        # Both parsers ran in containers: no network, read-only, parser mounted read-only.
        calls = runtime_calls(log)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertEqual(call[call.index("--network") + 1], "none")
            self.assertIn("--read-only", call)
            self.assertTrue(call[call.index("-v") + 1].endswith(":/candidate:ro"))

    def test_no_container_runtime_means_no_score(self):
        sup, update = self.quarantined("def parse_action(reply):\n    return {}\n")
        score = self.score(sup, update, None)
        self.assertEqual(score["status"], "unavailable")
        self.assertIn("container", score["reason"])

    def test_an_unmounted_scoring_dir_gives_an_error_not_a_score(self):
        sup, update = self.quarantined("def parse_action(reply):\n    return {}\n")
        runtime, _ = make_fake_runtime(self.tmp, "empty")
        score = self.score(sup, update, runtime)
        self.assertEqual(score["status"], "error")
        self.assertNotIn("candidate", score)

    def test_a_forged_result_line_changes_nothing(self):
        operator_dir = self.tmp / "operator"
        operator_dir.mkdir()
        forged = json.dumps({"outcomes": {"a": {"kind": "action", "value": {"action": "done"}}}})
        (operator_dir / "action_parser.py").write_text(
            "import sys\n"
            f"print({forged!r})\n"
            f"print('SEEDLING-OUTCOMES ' + {forged!r})\n"
            "def parse_action(reply):\n"
            f"    print({forged!r})\n"
            "    raise KeyError('wrong')\n"
        )
        corpus = self.tmp / "corpus.jsonl"
        corpus.write_text(json.dumps({"id": "a", "reply": "x", "expect": {"action": "done"}}) + "\n")
        result = self.measure.score_parser(str(operator_dir), str(corpus))
        self.assertIsNone(result["error"])
        self.assertEqual(result["passed"], 0)
        self.assertEqual(result["cases"][0]["outcome"]["kind"], "crash")

    def test_the_parser_is_never_shown_the_expected_answers(self):
        operator_dir = self.tmp / "operator"
        operator_dir.mkdir()
        (operator_dir / "action_parser.py").write_text(
            "import sys\n"
            "def parse_action(reply):\n"
            "    return {'action': 'done', 'argv': sys.argv[1:], 'stdin': sys.stdin.read()}\n"
        )
        corpus = self.tmp / "corpus.jsonl"
        corpus.write_text(json.dumps({"id": "a", "reply": "x", "expect": {"action": "done"}}) + "\n")
        result = self.measure.score_parser(str(operator_dir), str(corpus))
        value = result["cases"][0]["outcome"]["value"]
        self.assertEqual(value["argv"], [str(operator_dir / "action_parser.py")])
        self.assertEqual(value["stdin"], "")
        self.assertEqual(self.measure.child_request([{"id": "a", "reply": "x", "expect": "refuse"}]), {"replies": [{"id": "a", "reply": "x"}]})

    def test_a_result_that_does_not_cover_the_replies_is_refused(self):
        cases = [{"id": "a"}, {"id": "b"}]
        for stdout in ("", "nonsense", json.dumps({"outcomes": {"a": {"kind": "action"}}}),
                       json.dumps({"outcomes": {"a": {"kind": "action"}, "b": {"kind": "pass"}}})):
            with self.assertRaises(ValueError, msg=stdout):
                self.measure.read_outcomes(stdout, cases)

    def test_dashboard_shows_the_score_read_only(self):
        app_js = (ROOT / "supervisor" / "static" / "app.js").read_text()
        self.assertIn("scoreLine(u)", app_js)
        self.assertIn("unexpectedLine(u)", app_js)
        # Apply is only ever sent from the Apply button's click handler.
        self.assertEqual(app_js.count("/apply`"), 1)
        fixture = json.loads((ROOT / "supervisor" / "static" / "demo" / "fixture.json").read_text())
        scored = [u for u in fixture["updates"] if u.get("score", {}).get("status") == "done"]
        self.assertTrue(scored)


class UiAssetTests(unittest.TestCase):
    UI_FILES = [
        "supervisor/static/index.html", "supervisor/static/app.js", "supervisor/static/style.css",
        "zencode/static/index.html", "zencode/static/app.js", "zencode/static/style.css",
        "operator/static/index.html", "operator/static/app.js", "operator/static/style.css",
        "desktop/ui/index.html",
    ]

    def test_ui_makes_no_external_requests(self):
        for rel in self.UI_FILES:
            text = (ROOT / rel).read_text()
            urls = re.findall(r"https?://[^\s\"'`)]+", text)
            # XML namespace identifiers (inline SVG) are not requests.
            external = [u for u in urls if not u.startswith(("http://127.0.0.1", "http://www.w3.org/"))]
            self.assertEqual(external, [], f"{rel} references {external}")

    def test_demo_fixtures_are_well_formed(self):
        fixture = json.loads((ROOT / "supervisor/static/demo/fixture.json").read_text())
        self.assertEqual(set(fixture["topology"]["processes"]), {"zencode_a", "zencode_b", "operator_a", "operator_b"})
        self.assertTrue(all(u["status"] == "quarantined" for u in fixture["updates"]))
        for name in ("operator_a", "operator_b"):
            self.assertTrue((ROOT / f"supervisor/static/demo/{name}.png").is_file())
        demo = json.loads((ROOT / "zencode/static/demo.json").read_text())
        self.assertTrue(demo["messages"] and demo["logs"] and demo["files"])

    def test_operator_facing_labels_are_kept(self):
        # The operator prompt teaches these selectors; renaming them breaks the loop.
        html = (ROOT / "zencode/static/index.html").read_text()
        for needle in ("Connect &amp; Probe", 'type="url"', "<textarea", "Ship Update", "Initialize Workspace"):
            self.assertIn(needle, html)


if __name__ == "__main__":
    unittest.main()
