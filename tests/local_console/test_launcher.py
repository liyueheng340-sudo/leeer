from __future__ import annotations

import importlib.util
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
