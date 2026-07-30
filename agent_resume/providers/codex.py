from __future__ import annotations

import json
import select
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from agent_resume.models import (
    ProviderSnapshot,
    QuotaWindow,
    STATUS_ALLOWED,
    STATUS_REJECTED,
    classify_duration,
    merge_windows,
)


class CodexProtocolError(RuntimeError):
    pass


def read_rate_limits(binary: str, timeout: float = 12.0) -> ProviderSnapshot:
    """Read and normalize the current Codex subscription windows once."""
    process = _start_app_server(binary)
    try:
        _initialize(process, timeout)
        _send(process, {"method": "account/rateLimits/read", "id": 1})
        message = _wait_for_id(process, 1, timeout)
        if "error" in message:
            raise CodexProtocolError(_error_text(message["error"]))
        result = message.get("result")
        if not isinstance(result, dict):
            raise CodexProtocolError("Codex returned no rate-limit result")
        snapshots = _snapshots_from_result(result)
        windows = _deduplicate_windows(snapshots)
        capability = "full" if windows else "unavailable"
        detail = None if windows else "No ChatGPT subscription windows were returned"
        return ProviderSnapshot("codex", windows, capability=capability, detail=detail)
    finally:
        _terminate(process)


class CodexMonitor:
    """Maintain a direct-mode app-server connection and publish normalized updates."""

    def __init__(
        self,
        binary: str,
        callback: Callable[[ProviderSnapshot], None],
        error_callback: Optional[Callable[[str], None]] = None,
        poll_seconds: float = 60.0,
    ) -> None:
        self.binary = binary
        self.callback = callback
        self.error_callback = error_callback
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None

    def start(self) -> None:
        """Start the background monitor once; repeated calls are harmless."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="agent-resume-codex-monitor", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop polling, terminate its app server, and join the monitor thread."""
        self._stop.set()
        if self._process is not None:
            _terminate(self._process)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        """Reconnect with bounded backoff so monitor loss never kills the workflow."""
        retry = 1.0
        while not self._stop.is_set():
            try:
                self._run_connection()
                retry = 1.0
            except Exception as exc:  # monitor errors degrade rather than kill workflows
                if self.error_callback is not None and not self._stop.is_set():
                    self.error_callback(str(exc))
                if self._stop.wait(retry):
                    return
                retry = min(retry * 2, 30.0)

    def _run_connection(self) -> None:
        """Poll and consume rate-limit notifications for one app-server session."""
        process = _start_app_server(self.binary)
        self._process = process
        try:
            _initialize(process, 12.0)
            next_request_id = 10
            next_poll = 0.0
            while not self._stop.is_set() and process.poll() is None:
                now = time.monotonic()
                if now >= next_poll:
                    _send(
                        process,
                        {"method": "account/rateLimits/read", "id": next_request_id},
                    )
                    next_request_id += 1
                    next_poll = now + self.poll_seconds
                message = _read_message(process, min(1.0, max(0.05, next_poll - now)))
                if message is None:
                    continue
                if message.get("method") == "account/rateLimits/updated":
                    params = message.get("params", {})
                    snapshot = params.get("rateLimits")
                    if isinstance(snapshot, dict):
                        self.callback(
                            ProviderSnapshot("codex", _windows_from_snapshot(snapshot))
                        )
                    continue
                result = message.get("result")
                if isinstance(result, dict) and (
                    "rateLimits" in result or "rateLimitsByLimitId" in result
                ):
                    self.callback(
                        ProviderSnapshot(
                            "codex",
                            _deduplicate_windows(_snapshots_from_result(result)),
                        )
                    )
            if not self._stop.is_set():
                raise CodexProtocolError("Codex app-server monitor exited")
        finally:
            self._process = None
            _terminate(process)


def _start_app_server(binary: str) -> subprocess.Popen:
    """Start Codex's documented stdio app-server transport."""
    try:
        return subprocess.Popen(
            [binary, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        raise CodexProtocolError("Unable to start Codex app-server: {}".format(exc))


def _initialize(process: subprocess.Popen, timeout: float) -> None:
    """Complete the app-server initialization handshake before making requests."""
    _send(
        process,
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "agent_resume",
                    "title": "Agent Resume",
                    "version": "0.1.0",
                }
            },
        },
    )
    response = _wait_for_id(process, 0, timeout)
    if "error" in response:
        raise CodexProtocolError(_error_text(response["error"]))
    _send(process, {"method": "initialized"})


def _send(process: subprocess.Popen, message: Dict[str, Any]) -> None:
    if process.stdin is None:
        raise CodexProtocolError("Codex app-server stdin is unavailable")
    try:
        process.stdin.write(
            (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        )
        process.stdin.flush()
    except OSError as exc:
        raise CodexProtocolError("Codex app-server write failed: {}".format(exc))


def _wait_for_id(
    process: subprocess.Popen, request_id: int, timeout: float
) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = _read_message(process, max(0.05, deadline - time.monotonic()))
        if message is None:
            continue
        if message.get("id") == request_id:
            return message
    raise CodexProtocolError("Timed out waiting for Codex app-server")


def _read_message(
    process: subprocess.Popen, timeout: float
) -> Optional[Dict[str, Any]]:
    if process.stdout is None:
        raise CodexProtocolError("Codex app-server stdout is unavailable")
    if process.poll() is not None:
        raise CodexProtocolError(
            "Codex app-server exited with code {}".format(process.returncode)
        )
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        return None
    line = process.stdout.readline()
    if not line:
        if process.poll() is not None:
            raise CodexProtocolError(
                "Codex app-server exited with code {}".format(process.returncode)
            )
        return None
    try:
        value = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CodexProtocolError("Invalid Codex app-server JSON: {}".format(exc))
    return value if isinstance(value, dict) else None


def _snapshots_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        values = [value for value in by_id.values() if isinstance(value, dict)]
        if values:
            return values
    snapshot = result.get("rateLimits")
    return [snapshot] if isinstance(snapshot, dict) else []


def _windows_from_snapshot(snapshot: Dict[str, Any]) -> List[QuotaWindow]:
    """Convert Codex primary/secondary rate-limit fields into common windows."""
    result: List[QuotaWindow] = []
    reached = str(snapshot.get("rateLimitReachedType") or "").lower()
    limit_id = _optional_string(snapshot.get("limitId"))
    for name in ("primary", "secondary"):
        value = snapshot.get(name)
        if not isinstance(value, dict):
            continue
        try:
            used = float(value.get("usedPercent", 0))
        except (TypeError, ValueError):
            continue
        duration = _optional_int(value.get("windowDurationMins"))
        resets_at = _optional_int(value.get("resetsAt"))
        result.append(
            QuotaWindow(
                kind=classify_duration(duration),
                used_percent=used,
                resets_at=resets_at,
                status=STATUS_REJECTED
                if reached and (reached == name or used >= 100)
                else STATUS_ALLOWED,
                limit_id=limit_id,
                duration_minutes=duration,
            )
        )
    return result


def _deduplicate_windows(snapshots: List[Dict[str, Any]]) -> List[QuotaWindow]:
    """Preserve distinct Codex limits while combining repeated snapshot records."""
    result: Dict[str, QuotaWindow] = {}
    for snapshot in snapshots:
        for window in _windows_from_snapshot(snapshot):
            key = "{}:{}:{}".format(
                window.limit_id or "", window.kind, window.duration_minutes or ""
            )
            result[key] = window
    return merge_windows([], result.values())


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    for stream in (process.stdin, process.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def _error_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("message") or value)
    return str(value)


def _optional_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_string(value: Any) -> Optional[str]:
    return None if value is None else str(value)
