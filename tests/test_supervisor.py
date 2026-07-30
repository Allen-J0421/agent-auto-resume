import os
import json
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_resume.supervisor import Supervisor


FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json
import math
import os
import sys
import time
from pathlib import Path
from agent_resume.protocol import request

Path(os.environ["AGENT_RESUME_RUN_DIR"]).joinpath("received.json").write_text(
    json.dumps(sys.argv[1:])
)
request(
    os.environ["AGENT_RESUME_SOCKET"],
    {
        "type": "event",
        "event": "quota",
        "provider": "claude",
        "session_id": "fake-session",
        "windows": [{
            "kind": "five_hour",
            "used_percent": 99,
            "resets_at": math.ceil(time.time()) + 1,
            "status": "warning",
            "observed_at": int(time.time())
        }]
    },
    timeout=10,
)
print("fake claude completed")
"""


class SupervisorTests(unittest.TestCase):
    def test_path_shim_pauses_and_resumes_same_group(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as directory:
            root = Path(directory)
            binary = root / "claude"
            binary.write_text(FAKE_CLAUDE, encoding="utf-8")
            binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            path = str(root) + os.pathsep + os.environ.get("PATH", "")
            with mock.patch.dict(os.environ, {"PATH": path, "TMPDIR": str(root)}):
                supervisor = Supervisor(
                    ["claude", "-p", "hello"],
                    os.getcwd(),
                    provider_mode="claude",
                    threshold=98,
                    reset_grace=0,
                    direct_provider="claude",
                )
                # A workspace-local socket avoids CI sandbox restrictions while
                # exercising the same Unix socket protocol.
                supervisor.socket_path = str(root / "supervisor.sock")
                started = time.monotonic()
                result = supervisor.run()
                elapsed = time.monotonic() - started
                received = json.loads(
                    (supervisor.run_dir / "received.json").read_text(encoding="utf-8")
                )
        self.assertEqual(result, 0)
        self.assertGreaterEqual(elapsed, 0.7)
        self.assertEqual(supervisor.state, "completed")
        self.assertEqual(supervisor.session_ids["claude"], "fake-session")
        self.assertEqual(received[:2], ["-p", "hello"])
        self.assertIn("--settings", received)
        self.assertIn("--plugin-dir", received)

    def test_wrapped_exit_code_is_preserved(self):
        with tempfile.TemporaryDirectory(dir=os.getcwd()) as directory:
            with mock.patch.dict(os.environ, {"TMPDIR": directory}):
                supervisor = Supervisor(
                    [sys.executable, "-c", "raise SystemExit(23)"],
                    os.getcwd(),
                )
                supervisor.socket_path = str(Path(directory) / "exit.sock")
                self.assertEqual(supervisor.run(), 23)

    def test_watchdog_resumes_stopped_process_group(self):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os,signal,sys;"
                    "print('ready',flush=True);"
                    "os.kill(os.getpid(),signal.SIGSTOP);"
                    "print('resumed',flush=True)"
                ),
            ],
            stdout=subprocess.PIPE,
            universal_newlines=True,
            start_new_session=True,
        )
        assert child.stdout is not None
        self.assertEqual(child.stdout.readline().strip(), "ready")
        deadline = time.time() + 2
        while time.time() < deadline:
            pid, status = os.waitpid(child.pid, os.WUNTRACED | os.WNOHANG)
            if pid and os.WIFSTOPPED(status):
                break
            time.sleep(0.01)
        else:
            self.fail("child did not stop")
        read_fd, write_fd = os.pipe()
        watchdog = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_resume.watchdog",
                str(read_fd),
                str(child.pid),
            ],
            pass_fds=(read_fd,),
        )
        os.close(read_fd)
        os.close(write_fd)
        self.assertEqual(child.stdout.readline().strip(), "resumed")
        self.assertEqual(child.wait(timeout=2), 0)
        self.assertEqual(watchdog.wait(timeout=2), 0)
        child.stdout.close()
