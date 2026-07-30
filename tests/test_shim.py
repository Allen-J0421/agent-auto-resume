import contextlib
import io
import os
import unittest
from unittest import mock

from agent_resume import shim


ENV = {
    "AGENT_RESUME_SOCKET": "/tmp/agent-resume-test.sock",
    "AGENT_RESUME_REAL_CLAUDE": "/usr/bin/true",
    "AGENT_RESUME_CLAUDE_SETTINGS": "/tmp/settings.json",
    "AGENT_RESUME_CLAUDE_PLUGIN": "/tmp/plugin",
}


class ShimRecoveryTests(unittest.TestCase):
    def _run(self, args, result_responses):
        executed = []
        responses = list(result_responses)

        def fake_request(socket_path, message):
            if message["type"] == "gate":
                return {"action": "proceed"}
            return responses.pop(0)

        def fake_execute(provider, real_binary, current_args, socket_path):
            executed.append(list(current_args))
            return 1

        with mock.patch.dict(os.environ, ENV), mock.patch.object(
            shim, "_request", side_effect=fake_request
        ), mock.patch.object(shim, "_execute", side_effect=fake_execute):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = shim.run("claude", args)
        return code, executed, stderr.getvalue()

    def test_stream_json_refuses_resume_rewrite(self):
        code, executed, output = self._run(
            ["--input-format", "stream-json", "-p"],
            [{"action": "resume_session", "session_id": "session-1"}],
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(executed), 1)
        self.assertIn("pause-until-reset", output)

    def test_stream_json_refuses_retry(self):
        code, executed, output = self._run(
            ["--input-format=stream-json"],
            [{"action": "retry"}],
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(executed), 1)
        self.assertIn("pause-until-reset", output)

    def test_plain_invocation_still_resumes(self):
        code, executed, output = self._run(
            ["-p", "prompt"],
            [
                {"action": "resume_session", "session_id": "session-1"},
                {"action": "exit"},
            ],
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(executed), 2)
        self.assertIn("--resume", executed[1])
        self.assertIn("session-1", executed[1])
        self.assertNotIn("pause-until-reset", output)
