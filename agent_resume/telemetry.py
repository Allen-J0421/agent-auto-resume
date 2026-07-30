from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from agent_resume.providers.claude import window_from_rate_event


class StructuredTelemetry:
    """Observe provider JSONL without changing bytes sent to the caller."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.session_id: Optional[str] = None

    def consume(self, line: bytes) -> List[Dict[str, Any]]:
        """Convert one provider JSONL record into zero or more supervisor events."""
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return []
        if not isinstance(value, dict):
            return []
        if self.provider == "claude":
            return self._claude(value)
        return self._codex(value)

    def _claude(self, value: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract Claude session, quota-window, and typed-limit events."""
        session_id = _find_string(value, ("session_id",))
        if session_id:
            self.session_id = session_id
        event_type = str(value.get("type") or "")
        if event_type == "rate_limit_event" or "rate_limit_info" in value:
            window = window_from_rate_event(value)
            if window is None:
                return []
            events = [
                {
                    "type": "event",
                    "event": "quota",
                    "provider": "claude",
                    "session_id": self.session_id,
                    "windows": [window.to_dict()],
                }
            ]
            if window.status == "rejected":
                events.append(
                    {
                        "type": "event",
                        "event": "quota_failure",
                        "provider": "claude",
                        "session_id": self.session_id,
                        "resets_at": window.resets_at,
                        "detail": "typed Claude rate-limit rejection",
                    }
                )
            return events
        if session_id:
            return [
                {
                    "type": "event",
                    "event": "session",
                    "provider": "claude",
                    "session_id": session_id,
                }
            ]
        return []

    def _codex(self, value: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Extract Codex thread IDs and structured usage-limit failures."""
        event_type = str(value.get("type") or value.get("method") or "")
        session_id = _find_string(value, ("thread_id", "threadId"))
        if event_type in ("thread.started", "thread/started") and session_id:
            self.session_id = session_id
            return [
                {
                    "type": "event",
                    "event": "session",
                    "provider": "codex",
                    "session_id": session_id,
                }
            ]
        if _contains_usage_limit(value):
            return [
                {
                    "type": "event",
                    "event": "quota_failure",
                    "provider": "codex",
                    "session_id": self.session_id,
                    "resets_at": _find_epoch(value),
                    "detail": "typed Codex usageLimitExceeded error",
                }
            ]
        return []


def uses_structured_stdout(provider: str, args: Sequence[str]) -> bool:
    """Return whether these CLI arguments make stdout safe to inspect as JSONL."""
    if provider == "codex":
        return "--json" in args or "--experimental-json" in args
    for index, arg in enumerate(args):
        if arg == "--output-format" and index + 1 < len(args):
            return args[index + 1] == "stream-json"
        if arg.startswith("--output-format="):
            return arg.split("=", 1)[1] == "stream-json"
    return False


def _find_string(value: Any, keys: Sequence[str]) -> Optional[str]:
    if isinstance(value, dict):
        for key in keys:
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
        for nested in value.values():
            found = _find_string(nested, keys)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_string(nested, keys)
            if found:
                return found
    return None


def _contains_usage_limit(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _contains_usage_limit(key) or _contains_usage_limit(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_usage_limit(nested) for nested in value)
    if isinstance(value, str):
        normalized = value.replace("_", "").replace("-", "").lower()
        return "usagelimitexceeded" in normalized
    return False


def _find_epoch(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in ("resets_at", "reset_at", "resetsAt", "resetAt"):
                try:
                    result = int(nested)
                    if result > 1_000_000_000:
                        return result
                except (TypeError, ValueError):
                    pass
            result = _find_epoch(nested)
            if result is not None:
                return result
    elif isinstance(value, list):
        for nested in value:
            result = _find_epoch(nested)
            if result is not None:
                return result
    return None
