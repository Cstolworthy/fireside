"""The container entrypoint: create the data dir, then hand off to the app.

The bug it exists for (issue #78): a host bind mount owned by root, an app that
runs as uid 10001, and SQLite failing to create its file. The test cannot chown
as a non-root runner, but it can prove the entrypoint creates the data dir and
execs the command, which is the part that must never be wrong.
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRYPOINT = os.path.join(ROOT, "docker-entrypoint.py")


def _load():
    spec = importlib.util.spec_from_file_location("fs_entrypoint", ENTRYPOINT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestEntrypoint(unittest.TestCase):
    def test_creates_data_dir_and_then_execs(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "nested", "data")
            env = dict(os.environ, FIRESIDE_DATA=data)
            out = subprocess.run(
                [sys.executable, ENTRYPOINT, sys.executable, "-c", "print('handoff-ok')"],
                env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("handoff-ok", out.stdout)
            self.assertTrue(os.path.isdir(data), "data dir should be created")

    def test_defaults_when_env_is_garbage(self):
        mod = _load()
        os.environ["PUID"] = "1234"
        self.assertEqual(mod._int_env("PUID", 1), 1234)
        os.environ["PUID"] = "not-a-number"
        self.assertEqual(mod._int_env("PUID", 7), 7)
        os.environ["PUID"] = ""
        self.assertEqual(mod._int_env("PUID", 9), 9)
        os.environ.pop("PUID", None)

    def test_missing_command_is_a_clear_error(self):
        out = subprocess.run([sys.executable, ENTRYPOINT],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 2)
        self.assertIn("usage", out.stderr.lower())


if __name__ == "__main__":
    unittest.main()
