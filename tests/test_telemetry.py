import json
import unittest

from agent_resume.telemetry import StructuredTelemetry, uses_structured_stdout


class TelemetryTests(unittest.TestCase):
    def test_codex_session_and_typed_failure(self):
        parser = StructuredTelemetry("codex")
        events = parser.consume(
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}).encode()
        )
        self.assertEqual(events[0]["session_id"], "thread-1")
        events = parser.consume(
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "code": "usageLimitExceeded",
                        "resetsAt": 1_900_000_000,
                    },
                }
            ).encode()
        )
        self.assertEqual(events[0]["event"], "quota_failure")
        self.assertEqual(events[0]["session_id"], "thread-1")

    def test_claude_rejected_event(self):
        parser = StructuredTelemetry("claude")
        events = parser.consume(
            json.dumps(
                {
                    "type": "rate_limit_event",
                    "session_id": "session-1",
                    "rate_limit_info": {
                        "rate_limit_type": "five_hour",
                        "utilization": 1,
                        "status": "rejected",
                        "resets_at": 1_900_000_000,
                    },
                }
            ).encode()
        )
        self.assertEqual([event["event"] for event in events], ["quota", "quota_failure"])

    def test_structured_detection(self):
        self.assertTrue(uses_structured_stdout("codex", ["exec", "--json", "x"]))
        self.assertTrue(
            uses_structured_stdout("claude", ["-p", "--output-format=stream-json"])
        )
        self.assertFalse(uses_structured_stdout("claude", ["-p", "hello"]))
