from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_console.jobs import JobStore


class JobStoreTests(unittest.TestCase):
    def test_persists_transition_across_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            first = JobStore(Path(directory))
            created = first.create("brief")
            first.transition(created.id, "SNAPSHOT", "Reading MT5 snapshot")

            restored = JobStore(Path(directory)).get(created.id)

        self.assertEqual("SNAPSHOT", restored.stage)
        self.assertEqual("Reading MT5 snapshot", restored.detail)
        self.assertGreaterEqual(restored.elapsed_seconds, 0)

    def test_rejects_transition_from_terminal_job(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JobStore(Path(directory))
            job = store.create("brief")
            store.transition(job.id, "COMPLETE", "Done")

            with self.assertRaisesRegex(ValueError, "terminal"):
                store.transition(job.id, "MODEL", "Must not restart")
