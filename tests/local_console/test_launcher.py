from __future__ import annotations

import importlib.util
from unittest.mock import patch
import unittest
from pathlib import Path


def load_launcher():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_xau_analysis_console.py"
    spec = importlib.util.spec_from_file_location("run_xau_analysis_console", path)
    if spec is None or spec.loader is None:
        raise AssertionError("launcher module cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LauncherTests(unittest.TestCase):
    def test_launcher_only_accepts_localhost(self):
        launcher = load_launcher()

        self.assertEqual(("127.0.0.1", 8767), launcher.launch_arguments([]))
        with self.assertRaises(SystemExit):
            launcher.launch_arguments(["--host", "0.0.0.0"])

    def test_launcher_detects_an_already_running_console(self):
        launcher = load_launcher()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"service":"ready","host":"127.0.0.1"}'

        with patch.object(launcher, "urlopen", return_value=Response()):
            self.assertEqual("http://127.0.0.1:8767", launcher.find_existing_console_url("127.0.0.1", 8767))

    def test_launcher_refuses_environment_missing_llm_dependencies(self):
        launcher = load_launcher()

        with patch.object(launcher.importlib.util, "find_spec", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                launcher.check_llm_dependencies()

        self.assertEqual(1, ctx.exception.code)

    def test_launcher_accepts_environment_with_llm_dependencies(self):
        launcher = load_launcher()

        with patch.object(launcher.importlib.util, "find_spec", return_value=object()):
            launcher.check_llm_dependencies()  # 不抛即通过
