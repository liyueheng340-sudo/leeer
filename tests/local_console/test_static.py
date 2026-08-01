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

    def test_dashboard_exposes_sensor_and_evidence_regions(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")

        self.assertIn('id="tick-status"', page)
        self.assertIn('id="macro-status"', page)
        self.assertIn('id="macro-card"', page)
        self.assertIn('id="layer-evidence"', page)
        self.assertIn('id="decision-state"', page)

    def test_browser_polls_durable_job_state_instead_of_simulating_progress(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn('await jsonRequest(`/api/jobs/${jobId}`)', script)
        self.assertIn("window.setInterval", script)
        self.assertIn("setControlsBusy(!TERMINAL.has(job.stage))", script)
        self.assertIn("pollJob(status.latest_job.id)", script)
        self.assertIn('catch (error) {', script)
        self.assertIn('无法读取历史任务，请确认本机服务正在运行。', script)

    def test_dashboard_boots_from_one_durable_server_state(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn("async function boot()", script)
        self.assertIn("await refreshStatus();", script)
        self.assertIn("await refreshHistory();", script)
        self.assertNotIn("sessionStorage", script)
        self.assertNotIn("restoreActiveJob", script)

    def test_failed_jobs_keep_completed_progress_and_failed_history_state(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn("const terminalFailure =", script)
        self.assertIn("terminalFailure ? STAGES.length - 1", script)
        self.assertIn("jobDisplayState(job)", script)
        self.assertIn('byId("decision-state").textContent = "失败"', script)
        self.assertIn('["FAILED", "REJECTED"].includes(stage)', script)

    def test_dashboard_hides_legacy_english_reports(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn("reportIsChinese", script)
        self.assertIn("历史报告不符合当前中文输出标准", script)
        self.assertIn("事件上下文未核验", script)

    def test_history_escapes_model_controlled_failure_details(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn("function escapeHtml(value)", script)
        self.assertIn("${escapeHtml(detailText(job.detail))}", script)
        self.assertIn('escapeHtml(errorText(error, "无法读取历史任务', script)

    def test_evidence_chips_and_macro_rows_are_escaped(self):
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn("renderSensors(gate)", script)
        self.assertIn("${escapeHtml(field)}", script)
        self.assertIn("escapeHtml(item.label || sid)", script)

    def test_poll_failure_backs_off_instead_of_giving_up(self):
        # 轮询遇瞬时错误必须退避重试而非一次性判死（2026-08-01 服务故障时前端放弃轮询）。
        script = (STATIC / "app.js").read_text(encoding="utf-8")

        self.assertIn("pollFailures", script)
        self.assertIn("状态读取暂时失败", script)
        self.assertIn("pollJob(jobId);", script)
        self.assertIn("window.clearInterval(pollingTimer);", script)
