from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_resume.models import (
    QuotaWindow,
    STATUS_ALLOWED,
    STATUS_REJECTED,
    STATUS_WARNING,
    WINDOW_FIVE_HOUR,
    WINDOW_MODEL,
    WINDOW_WEEKLY,
    WINDOW_UNKNOWN,
)
from agent_resume.runtime import atomic_write_json, read_json


CLAUDE_SUBCOMMANDS = {
    "agents",
    "attach",
    "auth",
    "auto-mode",
    "daemon",
    "doctor",
    "install",
    "logs",
    "mcp",
    "plugin",
    "plugins",
    "project",
    "update",
}


def prepare_integration(
    run_dir: Path, cwd: str, python_executable: Optional[str] = None
) -> Tuple[Path, Path]:
    """Create per-run Claude settings and hook plugin without touching user config."""
    executable = python_executable or sys.executable
    original = discover_status_line(cwd)
    original_path = run_dir / "original-claude-statusline.json"
    atomic_write_json(original_path, original or {})

    settings_path = run_dir / "claude-settings.json"
    status_command = "{} -m agent_resume.hooks statusline".format(
        shlex.quote(executable)
    )
    atomic_write_json(
        settings_path,
        {
            "statusLine": {
                "type": "command",
                "command": status_command,
                "refreshInterval": 5,
            }
        },
    )

    plugin_dir = run_dir / "claude-plugin"
    manifest_dir = plugin_dir / ".claude-plugin"
    hooks_dir = plugin_dir / "hooks"
    manifest_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    hooks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write_json(
        manifest_dir / "plugin.json",
        {
            "name": "agent-resume-guard",
            "version": "0.1.0",
            "description": "Reports Claude subscription limit failures to Agent Resume.",
        },
    )
    hook_command = "{} -m agent_resume.hooks stop-failure".format(
        shlex.quote(executable)
    )
    atomic_write_json(
        hooks_dir / "hooks.json",
        {
            "hooks": {
                "StopFailure": [
                    {
                        "matcher": "rate_limit",
                        "hooks": [
                            {
                                "type": "command",
                                "command": hook_command,
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        },
    )
    return settings_path, plugin_dir


def augment_args(args: Sequence[str], settings_path: str, plugin_dir: str) -> List[str]:
    """Attach ephemeral Claude telemetry only to interactive/session invocations."""
    values = list(args)
    if not is_session_invocation(values):
        return values
    if "--safe-mode" in values:
        return values
    values.extend(["--settings", settings_path, "--plugin-dir", plugin_dir])
    return values


def is_session_invocation(args: Sequence[str]) -> bool:
    """Distinguish a Claude conversation from administrative/help subcommands."""
    if any(arg in ("--help", "-h", "--version", "-v") for arg in args):
        return False
    positional = _first_positional(args)
    return positional not in CLAUDE_SUBCOMMANDS


def is_stream_json_invocation(args: Sequence[str]) -> bool:
    """Detect SDK-driven sessions whose prompt and protocol state live on stdin."""
    for index, arg in enumerate(args):
        if arg == "--input-format" and index + 1 < len(args):
            return args[index + 1] == "stream-json"
        if arg.startswith("--input-format="):
            return arg.split("=", 1)[1] == "stream-json"
    return False


def make_resume_args(
    original_args: Sequence[str],
    session_id: str,
    settings_path: str,
    plugin_dir: str,
) -> List[str]:
    """Replace an interrupted Claude prompt with a guarded native session resume."""
    prompt = (
        "Continue the interrupted task from this saved session. Inspect the current "
        "working tree and transcript first, and do not repeat side effects that already "
        "completed before the usage limit."
    )
    kept: List[str] = []
    args = list(original_args)
    option_arity = _claude_option_arity()
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("--resume", "-r"):
            index += 2
            continue
        if arg in ("--continue", "-c"):
            index += 1
            continue
        if arg == "--settings":
            index += 2
            continue
        if arg == "--plugin-dir":
            index += 2
            continue
        if arg in ("--print", "-p"):
            kept.append(arg)
            index += 1
            continue
        if arg.startswith("-"):
            kept.append(arg)
            if "=" not in arg and option_arity.get(arg, 0) == 1 and index + 1 < len(args):
                kept.append(args[index + 1])
                index += 2
            else:
                index += 1
            continue
        # A session invocation has one initial prompt. Replace it with the recovery prompt.
        index += 1
    kept.extend(
        [
            "--resume",
            session_id,
            "--settings",
            settings_path,
            "--plugin-dir",
            plugin_dir,
            prompt,
        ]
    )
    return kept


def windows_from_status_payload(payload: Dict[str, Any]) -> List[QuotaWindow]:
    """Normalize Claude status-line five-hour and weekly quota data."""
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return []
    result: List[QuotaWindow] = []
    for field, kind in (("five_hour", WINDOW_FIVE_HOUR), ("seven_day", WINDOW_WEEKLY)):
        value = limits.get(field)
        if not isinstance(value, dict):
            continue
        utilization = _as_float(
            value.get("used_percentage", value.get("utilization", 0))
        )
        if utilization is None:
            continue
        if 0 <= utilization <= 1 and "used_percentage" not in value:
            utilization *= 100
        status = _normalize_status(value.get("status"), utilization)
        result.append(
            QuotaWindow(
                kind=kind,
                used_percent=utilization,
                resets_at=_as_int(value.get("resets_at")),
                status=status,
                limit_id=field,
            )
        )
    return result


def window_from_rate_event(payload: Dict[str, Any]) -> Optional[QuotaWindow]:
    """Normalize a structured Claude rate-limit event into one quota window."""
    info = payload.get("rate_limit_info", payload)
    if not isinstance(info, dict):
        return None
    rate_type = str(info.get("rate_limit_type") or "")
    if rate_type == "five_hour":
        kind = WINDOW_FIVE_HOUR
    elif rate_type == "seven_day":
        kind = WINDOW_WEEKLY
    elif rate_type in ("seven_day_opus", "seven_day_sonnet"):
        kind = WINDOW_MODEL
    else:
        kind = WINDOW_UNKNOWN
    utilization = _as_float(info.get("utilization"))
    used = 0.0 if utilization is None else utilization
    if used <= 1:
        used *= 100
    return QuotaWindow(
        kind=kind,
        used_percent=used,
        resets_at=_as_int(info.get("resets_at")),
        status=_normalize_status(info.get("status"), used),
        limit_id=rate_type or None,
    )


def discover_status_line(cwd: str) -> Optional[Dict[str, Any]]:
    """Find the effective user/project Claude status-line command, if configured."""
    candidates: List[Path] = []
    home = Path.home()
    candidates.append(home / ".claude" / "settings.json")
    project_root = _git_root(cwd) or Path(cwd)
    candidates.append(project_root / ".claude" / "settings.json")
    candidates.append(project_root / ".claude" / "settings.local.json")
    selected: Optional[Dict[str, Any]] = None
    for path in candidates:
        value = read_json(path)
        if not value:
            continue
        status = value.get("statusLine")
        if isinstance(status, dict) and isinstance(status.get("command"), str):
            selected = status
    return selected


def run_original_status_line(
    original_file: Optional[str], input_bytes: bytes
) -> Optional[int]:
    """Forward status-line input to the user's prior command when one exists."""
    if not original_file:
        return None
    value = read_json(Path(original_file))
    if not value:
        return None
    command = value.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        completed = subprocess.run(command, input=input_bytes, shell=True)
        return completed.returncode
    except OSError:
        return 1


def _first_positional(args: Sequence[str]) -> Optional[str]:
    arity = _claude_option_arity()
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if arg.startswith("-"):
            if "=" not in arg and arity.get(arg, 0) == 1:
                index += 2
            else:
                index += 1
            continue
        return arg
    return None


def _claude_option_arity() -> Dict[str, int]:
    values = [
        "--add-dir",
        "--agent",
        "--allowedTools",
        "--allowed-tools",
        "--append-subagent-system-prompt",
        "--append-system-prompt",
        "--append-system-prompt-file",
        "--betas",
        "--channels",
        "--debug-file",
        "--disallowedTools",
        "--disallowed-tools",
        "--effort",
        "--fallback-model",
        "--input-format",
        "--json-schema",
        "--max-budget-usd",
        "--max-turns",
        "--mcp-config",
        "--model",
        "--name",
        "--output-format",
        "--permission-mode",
        "--permission-prompt-tool",
        "--plugin-dir",
        "--resume",
        "-r",
        "--session-id",
        "--setting-sources",
        "--settings",
        "--system-prompt",
        "--system-prompt-file",
        "--teammate-mode",
        "--tools",
        "--worktree",
        "-w",
    ]
    return {value: 1 for value in values}


def _git_root(cwd: str) -> Optional[Path]:
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return Path(value) if value else None


def _normalize_status(value: Any, used_percent: float) -> str:
    status = str(value or "").lower()
    if status == "rejected":
        return STATUS_REJECTED
    if status in ("allowed_warning", "warning"):
        return STATUS_WARNING
    if used_percent >= 100:
        return STATUS_REJECTED
    return STATUS_ALLOWED


def _as_int(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
