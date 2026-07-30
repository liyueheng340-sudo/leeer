from __future__ import annotations

import unittest

from local_console.brief import validate_report
from local_console.guard import GateResult


class BriefValidationTests(unittest.TestCase):
    def test_report_with_unprovided_source_is_rejected(self):
        payload = {
            "action": "ANALYSE",
            "source_ids": ["mt5_snapshot", "Yahoo Finance"],
            "summary": "Structure is balanced.",
            "invalidation": "A close outside the observed range invalidates the observation.",
            "next_observation": "Wait for a fresh snapshot.",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("ANALYSE", True, True, "ok")
        )

        self.assertFalse(accepted)
        self.assertEqual("report cites an unprovided source: Yahoo Finance", reason)
        self.assertIsNone(report)

    def test_watch_report_cannot_contain_direct_entry_instruction(self):
        payload = {
            "action": "WATCH",
            "source_ids": ["mt5_snapshot"],
            "summary": "立即买入。",
            "invalidation": "Observation is invalid if the spread widens.",
            "next_observation": "Wait for event verification.",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("WATCH", True, False, "events unknown")
        )

        self.assertFalse(accepted)
        self.assertEqual("WATCH report contains a direct entry instruction", reason)
        self.assertIsNone(report)

    def test_valid_watch_report_is_available_to_the_ui(self):
        payload = {
            "action": "WATCH",
            "source_ids": ["mt5_snapshot"],
            "summary": "M1 structure is mixed, so wait for a confirmed close.",
            "invalidation": "The observation expires with the next snapshot.",
            "next_observation": "Refresh after the next closed M1 bar.",
        }

        accepted, reason, report = validate_report(
            payload, GateResult("WATCH", True, False, "events unknown")
        )

        self.assertTrue(accepted)
        self.assertEqual("report accepted", reason)
        self.assertEqual(payload, report)
