"""Smoke tests for Seedling's sandbox boundaries, quarantine checks and UI assets.

Run with scripts/test.sh (creates a throwaway venv with the server deps).
These tests never start the stack, a container or a model.
"""
import importlib.util
import io
import json
import os
import re
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
