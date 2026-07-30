import tempfile
import unittest
from pathlib import Path

from agent_resume.providers.claude import (
    augment_args,
    is_session_invocation,
    is_stream_json_invocation,
    make_resume_args,
    window_from_rate_event,
    windows_from_status_payload,
)


class ClaudeTests(unittest.TestCase):
    def test_status_payload_normalization(self):
        windows = windows_from_status_payload(
            {
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 98,
                        "resets_at": 1_900_000_000,
                    },
                    "seven_day": {
                        "utilization": 0.5,
                        "resets_at": 1_900_001_000,
                    },
                }
            }
        )
        self.assertEqual([window.used_percent for window in windows], [98, 50])

    def test_rate_limit_event(self):
        window = window_from_rate_event(
            {
                "rate_limit_info": {
                    "rate_limit_type": "seven_day_opus",
                    "utilization": 1,
                    "status": "rejected",
                    "resets_at": 1_900_000_000,
                }
            }
        )
        self.assertIsNotNone(window)
        self.assertEqual(window.kind, "model")
        self.assertEqual(window.used_percent, 100)
        self.assertEqual(window.status, "rejected")

    def test_only_session_invocations_are_augmented(self):
        self.assertFalse(is_session_invocation(["--version"]))
        self.assertFalse(is_session_invocation(["mcp", "list"]))
        self.assertTrue(is_session_invocation(["-p", "hello"]))
        augmented = augment_args(["-p", "hello"], "/s", "/p")
        self.assertEqual(augmented[-4:], ["--settings", "/s", "--plugin-dir", "/p"])

    def test_stream_json_invocations_are_detected(self):
        self.assertTrue(
            is_stream_json_invocation(["--input-format", "stream-json", "-p"])
        )
        self.assertTrue(is_stream_json_invocation(["--input-format=stream-json"]))
        self.assertFalse(is_stream_json_invocation(["-p", "hello"]))
        self.assertFalse(
            is_stream_json_invocation(["--output-format", "stream-json"])
        )
        self.assertFalse(is_stream_json_invocation(["--input-format", "text"]))

    def test_resume_replaces_prompt_and_keeps_print_mode(self):
        args = make_resume_args(
            ["-p", "--output-format", "json", "old prompt"], "session-1", "/s", "/p"
        )
        self.assertIn("-p", args)
        self.assertIn("json", args)
        self.assertNotIn("old prompt", args)
        self.assertIn("session-1", args)
