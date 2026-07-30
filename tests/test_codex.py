import stat
import tempfile
import unittest
from pathlib import Path

from agent_resume.providers.codex import read_rate_limits


FAKE_SERVER = r"""#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    value = json.loads(line)
    if value.get("method") == "initialize":
        print(json.dumps({"id": value["id"], "result": {}}), flush=True)
    elif value.get("method") == "account/rateLimits/read":
        result = {
            "rateLimitsByLimitId": {
                "chatgpt": {
                    "limitId": "chatgpt",
                    "primary": {
                        "usedPercent": 12,
                        "windowDurationMins": 10080,
                        "resetsAt": 1900000000
                    },
                    "secondary": {
                        "usedPercent": 98,
                        "windowDurationMins": 300,
                        "resetsAt": 1800000000
                    }
                }
            }
        }
        print(json.dumps({"id": value["id"], "result": result}), flush=True)
"""


class CodexTests(unittest.TestCase):
    def test_app_server_normalizes_by_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex"
            path.write_text(FAKE_SERVER, encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            snapshot = read_rate_limits(str(path), timeout=2)
        self.assertEqual(snapshot.provider, "codex")
        self.assertEqual(
            [(window.kind, window.used_percent) for window in snapshot.windows],
            [("five_hour", 98), ("weekly", 12)],
        )
