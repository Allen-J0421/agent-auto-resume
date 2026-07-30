from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from agent_resume.providers.codex import read_rate_limits


def run_doctor(timeout: float = 8.0) -> Dict[str, Any]:
    """Check local platform, provider binaries, and available monitoring paths."""
    checks: List[Dict[str, Any]] = []
    supported_platform = sys.platform.startswith(("darwin", "linux"))
    checks.append(
        _check(
            "platform",
            supported_platform,
            "{} {} ({})".format(platform.system(), platform.machine(), sys.platform),
        )
    )
    supported_python = sys.version_info >= (3, 9)
    checks.append(
        _check(
            "python",
            supported_python,
            "{}.{}.{}".format(*sys.version_info[:3]),
        )
    )
    runtime_root = os.path.realpath(
        os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-resume")
    )
    checks.append(_check("runtime", True, runtime_root))

    codex = shutil.which("codex")
    if codex:
        version = _version([codex, "--version"], timeout)
        checks.append(_check("codex executable", True, version or codex))
        try:
            snapshot = read_rate_limits(codex, timeout=timeout)
            detail = "{} subscription window(s) available".format(
                len(snapshot.windows)
            )
            checks.append(_check("codex monitoring", bool(snapshot.windows), detail))
        except Exception as exc:
            checks.append(
                _check(
                    "codex monitoring",
                    False,
                    "degraded: {}".format(exc),
                    fatal=False,
                )
            )
    else:
        checks.append(_check("codex executable", False, "not found", fatal=False))

    claude = shutil.which("claude")
    if claude:
        version = _version([claude, "--version"], timeout)
        checks.append(_check("claude executable", True, version or claude))
        checks.append(
            _check(
                "claude monitoring",
                True,
                "ephemeral status line and StopFailure hook will be tested at runtime",
            )
        )
    else:
        checks.append(_check("claude executable", False, "not found", fatal=False))

    fatal_failures = [
        item for item in checks if not item["ok"] and bool(item.get("fatal"))
    ]
    available_provider = codex is not None or claude is not None
    return {
        "ok": not fatal_failures and available_provider,
        "checks": checks,
        "limitations": [
            "absolute provider paths, SDK/API calls, remote processes, and replaced PATH values bypass interception",
            "pre-limit stopping is best effort because providers report usage after work",
            "Claude managed policy may disable temporary hooks or status-line telemetry",
        ],
    }


def _version(command: List[str], timeout: float) -> Optional[str]:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
            universal_newlines=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first_line = completed.stdout.strip().splitlines()
    return first_line[0] if first_line else None


def _check(name: str, ok: bool, detail: str, fatal: bool = True) -> Dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "fatal": fatal}
