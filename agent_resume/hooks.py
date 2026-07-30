from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, Optional

from agent_resume.protocol import request
from agent_resume.providers.claude import (
    run_original_status_line,
    windows_from_status_payload,
)


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Dispatch the Claude status-line or StopFailure hook entrypoint."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 2
    action = args[0]
    data = sys.stdin.buffer.read()
    try:
        payload = json.loads(data.decode("utf-8")) if data else {}
    except (UnicodeDecodeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if action == "statusline":
        return statusline(payload, data)
    if action == "stop-failure":
        return stop_failure(payload)
    return 2


def statusline(payload: Dict[str, Any], raw: bytes) -> int:
    """Report Claude window usage while preserving an existing status-line command."""
    windows = windows_from_status_payload(payload)
    event = {
        "type": "event",
        "event": "quota",
        "provider": "claude",
        "windows": [window.to_dict() for window in windows],
        "session_id": payload.get("session_id"),
    }
    _notify(event)

    original_file = os.environ.get("AGENT_RESUME_ORIGINAL_STATUSLINE_FILE")
    original_result = run_original_status_line(original_file, raw)
    if original_result is not None:
        return original_result

    if windows:
        parts = []
        for window in windows:
            label = "5h" if window.kind == "five_hour" else "7d"
            parts.append("{} {:.0f}%".format(label, window.used_percent))
        sys.stdout.write("Agent Resume · " + " · ".join(parts) + "\n")
    return 0


def stop_failure(payload: Dict[str, Any]) -> int:
    """Report a typed Claude quota rejection to the waiting supervisor."""
    if payload.get("error") != "rate_limit":
        return 0
    reset = _find_reset_timestamp(payload)
    _notify(
        {
            "type": "event",
            "event": "quota_failure",
            "provider": "claude",
            "session_id": payload.get("session_id"),
            "resets_at": reset,
            "detail": payload.get("error_details")
            or payload.get("last_assistant_message"),
            "observed_at": int(time.time()),
        }
    )
    return 0


def _notify(event: Dict[str, Any]) -> None:
    """Send a best-effort hook event to the per-run Unix socket."""
    socket_path = os.environ.get("AGENT_RESUME_SOCKET")
    if not socket_path:
        return
    try:
        request(socket_path, event, timeout=24 * 60 * 60)
    except (OSError, ValueError):
        return


def _find_reset_timestamp(payload: Any) -> Optional[int]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("resets_at", "reset_at", "resetsAt", "resetAt"):
                try:
                    result = int(value)
                    if result > 1_000_000_000:
                        return result
                except (TypeError, ValueError):
                    pass
            nested = _find_reset_timestamp(value)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _find_reset_timestamp(value)
            if nested is not None:
                return nested
    elif isinstance(payload, str):
        match = re.search(r"\b(1[7-9]\d{8}|2\d{9})\b", payload)
        if match:
            return int(match.group(1))
    return None


if __name__ == "__main__":
    raise SystemExit(main())
