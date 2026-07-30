from __future__ import annotations

import unittest
from pathlib import Path


STATIC = Path(__file__).resolve().parents[2] / "local_console" / "static"


class StaticConsoleTests(unittest.TestCase):
    def test_dashboard_has_task_buttons_and_accessible_progress_region(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="start-brief"', page)
        self.assertIn('id="start-deep-review"', page)
        self.assertIn('id="job-progress"', page)
        self.assertIn('aria-live="polite"', page)

    def test_browser_polls_durable_job_state_instead_of_simulating_progress(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn("fetch(`/api/jobs/${jobId}`)", script)
        self.assertIn("window.setInterval", script)
        self.assertIn("sessionStorage.setItem('xau-analysis-job-id'", script)
        self.assertIn("setControlsBusy(!TERMINAL.has(job.stage))", script)
        self.assertIn("pollJob(status.latest_job.id)", script)

    def test_dashboard_hides_legacy_english_reports(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn("reportIsChinese", script)
        self.assertIn("历史报告不符合当前中文输出标准", script)
        self.assertIn("事件上下文未核验", script)
