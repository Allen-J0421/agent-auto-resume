import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_resume.runtime import (
    atomic_write_json,
    create_run_dir,
    find_latest_state,
    read_json,
)


class RuntimeTests(unittest.TestCase):
    def test_private_atomic_state_and_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"TMPDIR": directory}):
                run_dir = create_run_dir()
                state_path = run_dir / "state.json"
                atomic_write_json(
                    state_path,
                    {"cwd": os.getcwd(), "updated_at": 1, "state": "running"},
                )
                self.assertEqual(read_json(state_path)["state"], "running")
                self.assertEqual(find_latest_state()["state"], "running")
                self.assertEqual(run_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
