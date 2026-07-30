from __future__ import annotations

import dataclasses
import time
from typing import Any, Dict, Iterable, List, Optional


WINDOW_FIVE_HOUR = "five_hour"
WINDOW_WEEKLY = "weekly"
WINDOW_MODEL = "model"
WINDOW_UNKNOWN = "unknown"

STATUS_ALLOWED = "allowed"
STATUS_WARNING = "warning"
STATUS_REJECTED = "rejected"


def classify_duration(minutes: Optional[int]) -> str:
    """Map provider window durations to the small cross-provider vocabulary."""
    if minutes is None:
        return WINDOW_UNKNOWN
    if 240 <= minutes <= 360:
        return WINDOW_FIVE_HOUR
    if 6 * 24 * 60 <= minutes <= 8 * 24 * 60:
        return WINDOW_WEEKLY
    return WINDOW_UNKNOWN


@dataclasses.dataclass
class QuotaWindow:
    kind: str
    used_percent: float
    resets_at: Optional[int]
    status: str = STATUS_ALLOWED
    observed_at: int = dataclasses.field(default_factory=lambda: int(time.time()))
    limit_id: Optional[str] = None
    duration_minutes: Optional[int] = None

    def blocks(self, threshold: float, now: Optional[float] = None) -> bool:
        """Return whether this window currently requires the workflow to wait."""
        current = time.time() if now is None else now
        exhausted = self.status == STATUS_REJECTED or self.used_percent >= threshold
        return exhausted and self.resets_at is not None and self.resets_at > current

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "used_percent": round(float(self.used_percent), 3),
            "resets_at": self.resets_at,
            "status": self.status,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "QuotaWindow":
        return cls(
            kind=str(value.get("kind", WINDOW_UNKNOWN)),
            used_percent=float(value.get("used_percent", 0)),
            resets_at=_optional_int(value.get("resets_at")),
            status=str(value.get("status", STATUS_ALLOWED)),
            observed_at=int(value.get("observed_at", time.time())),
            limit_id=_optional_str(value.get("limit_id")),
            duration_minutes=_optional_int(value.get("duration_minutes")),
        )


@dataclasses.dataclass
class ProviderSnapshot:
    provider: str
    windows: List[QuotaWindow]
    session_id: Optional[str] = None
    capability: str = "full"
    detail: Optional[str] = None

    def blocking_windows(
        self, threshold: float, now: Optional[float] = None
    ) -> List[QuotaWindow]:
        """Return the snapshot windows that are over threshold and unexpired."""
        return [window for window in self.windows if window.blocks(threshold, now)]


def merge_windows(
    existing: Iterable[QuotaWindow], incoming: Iterable[QuotaWindow]
) -> List[QuotaWindow]:
    """Merge sparse updates without assuming provider window ordering.

    Newer observations win, while missing reset/duration fields inherit known values.
    """
    merged: Dict[str, QuotaWindow] = {}
    for window in existing:
        merged[_window_key(window)] = window
    for window in incoming:
        key = _window_key(window)
        previous = merged.get(key)
        if previous is not None:
            if window.observed_at < previous.observed_at:
                continue
            if window.resets_at is None:
                window.resets_at = previous.resets_at
            if window.duration_minutes is None:
                window.duration_minutes = previous.duration_minutes
        merged[key] = window
    return sorted(merged.values(), key=_window_sort_key)


def latest_reset(windows: Iterable[QuotaWindow]) -> Optional[int]:
    values = [window.resets_at for window in windows if window.resets_at is not None]
    return max(values) if values else None


def _window_key(window: QuotaWindow) -> str:
    if window.limit_id:
        return "{}:{}:{}".format(
            window.limit_id, window.kind, window.duration_minutes or ""
        )
    return "{}:{}".format(window.kind, window.duration_minutes or "")


def _window_sort_key(window: QuotaWindow) -> Any:
    order = {
        WINDOW_FIVE_HOUR: 0,
        WINDOW_WEEKLY: 1,
        WINDOW_MODEL: 2,
        WINDOW_UNKNOWN: 3,
    }
    return order.get(window.kind, 4), window.limit_id or ""


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)
