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
        cls.client = TestClient(cls.zen.app)

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
