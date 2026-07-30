from __future__ import annotations

import errno
import fcntl
import json
import os
import pty
import queue
import selectors
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import tempfile
import time
import tty
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent_resume.models import (
    ProviderSnapshot,
    QuotaWindow,
    STATUS_ALLOWED,
    STATUS_REJECTED,
    WINDOW_UNKNOWN,
    merge_windows,
)
from agent_resume.providers.claude import prepare_integration
from agent_resume.providers.codex import CodexMonitor, read_rate_limits
from agent_resume.runtime import atomic_write_json, create_run_dir, prune_old_runs


STATE_RUNNING = "running"
STATE_PAUSE_PENDING = "pause_pending"
STATE_WAITING = "waiting"
STATE_VERIFYING = "verifying"
STATE_FAILED = "failed"
STATE_COMPLETED = "completed"

class Supervisor:
    """Supervise one workflow and pause its process group across quota resets."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: str,
        provider_mode: str = "auto",
        threshold: float = 98.0,
        reset_grace: int = 15,
        session_resume: bool = True,
        retry_idempotent: bool = False,
        verbose: bool = False,
        direct_provider: Optional[str] = None,
        executable_path: Optional[str] = None,
    ) -> None:
        """Prepare isolated IPC, provider integrations, and initial public state."""
        if not command:
            raise ValueError("a command is required")
        self.command = list(command)
        self.cwd = os.path.realpath(cwd)
        self.provider_mode = provider_mode
        self.threshold = threshold
        self.reset_grace = reset_grace
        self.session_resume = session_resume
        self.retry_idempotent = retry_idempotent
        self.verbose = verbose
        self.direct_provider = direct_provider
        self.executable_path = executable_path

        prune_old_runs()
        self.run_dir = create_run_dir()
        self.run_id = self.run_dir.name
        preferred_socket = str(self.run_dir / "supervisor.sock")
        if len(preferred_socket.encode("utf-8")) < 100:
            self.socket_path = preferred_socket
        else:
            self.socket_path = os.path.join(
                os.path.realpath(tempfile.gettempdir()),
                "ar-{}-{}.sock".format(
                    os.getuid(), self.run_id.replace("-", "")[:24]
                ),
            )
        self.state_path = self.run_dir / "state.json"
        self.shim_dir = self.run_dir / "shims"
        self.shim_dir.mkdir(mode=0o700)

        self.real_binaries = {
            "codex": shutil.which("codex"),
            "claude": shutil.which("claude"),
        }
        self.windows: Dict[str, List[QuotaWindow]] = {"codex": [], "claude": []}
        self.capabilities: Dict[str, str] = {}
        self.active_provider: Optional[str] = direct_provider
        self.session_ids: Dict[str, Optional[str]] = {"codex": None, "claude": None}
        self.last_failure: Dict[str, Dict[str, Any]] = {}
        self.state = STATE_RUNNING
        self.blocked_provider: Optional[str] = None
        self.blocked_until: Optional[float] = None
        self.paused = False
        self.pause_requested_provider: Optional[str] = None
        self.pause_requested_until: Optional[float] = None
        self.pending_stdin = bytearray()
        self.child: Optional[subprocess.Popen] = None
        self.child_pgid: Optional[int] = None
        self.watchdog: Optional[subprocess.Popen] = None
        self.watchdog_write_fd: Optional[int] = None
        self.server: Optional[socket.socket] = None
        self.selector = selectors.DefaultSelector()
        self.client_buffers: Dict[socket.socket, bytearray] = {}
        self.master_fd: Optional[int] = None
        self.saved_tty: Optional[List[Any]] = None
        self.codex_monitor: Optional[CodexMonitor] = None
        self.monitor_events: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.termination_signal: Optional[int] = None
        self._old_handlers: Dict[int, Any] = {}
        self.signal_read_fd: Optional[int] = None
        self.signal_write_fd: Optional[int] = None

        self.claude_settings, self.claude_plugin = prepare_integration(
            self.run_dir, self.cwd
        )
        self.original_statusline = self.run_dir / "original-claude-statusline.json"
        self._create_shims()
        self._write_state()

    def run(self) -> int:
        """Start the guarded workflow and drive it until the child exits."""
        self._start_server()
        environment = self._child_environment()
        use_pty = (
            os.isatty(sys.stdin.fileno())
            and os.isatty(sys.stdout.fileno())
            and os.isatty(sys.stderr.fileno())
        )
        try:
            self._install_signal_handlers()
            self._start_child(environment, use_pty)
            self._start_watchdog()
            if self.direct_provider == "codex":
                self._start_codex_monitor()
            return_code = self._event_loop()
            if return_code == 0:
                self.state = STATE_COMPLETED
            else:
                self.state = STATE_FAILED
            self._write_state(exit_code=return_code)
            return return_code
        finally:
            self._cleanup()

    def _start_server(self) -> None:
        """Open the owner-only Unix socket used by child-provider shims."""
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(self.socket_path)
        except Exception:
            server.close()
            raise
        os.chmod(self.socket_path, 0o600)
        server.listen(16)
        server.setblocking(False)
        self.server = server
        self.selector.register(server, selectors.EVENT_READ, "server")

    def _create_shims(self) -> None:
        """Create PATH shims that route provider calls through this supervisor."""
        for provider in ("codex", "claude"):
            path = self.shim_dir / provider
            content = (
                "#!/bin/sh\n"
                'exec "$AGENT_RESUME_PYTHON" -m agent_resume.cli _shim '
                + provider
                + ' "$@"\n'
            )
            with path.open("w", encoding="utf-8") as handle:
                handle.write(content)
            path.chmod(0o700)

    def _child_environment(self) -> Dict[str, str]:
        """Build the child-only environment that activates interception safely."""
        environment = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        existing_python_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            package_root
            if not existing_python_path
            else package_root + os.pathsep + existing_python_path
        )
        environment["PATH"] = str(self.shim_dir) + os.pathsep + environment.get(
            "PATH", ""
        )
        environment["AGENT_RESUME_PYTHON"] = sys.executable
        environment["AGENT_RESUME_SOCKET"] = self.socket_path
        environment["AGENT_RESUME_RUN_DIR"] = str(self.run_dir)
        environment["AGENT_RESUME_CLAUDE_SETTINGS"] = str(self.claude_settings)
        environment["AGENT_RESUME_CLAUDE_PLUGIN"] = str(self.claude_plugin)
        environment["AGENT_RESUME_ORIGINAL_STATUSLINE_FILE"] = str(
            self.original_statusline
        )
        environment["AGENT_RESUME_SESSION_RESUME"] = (
            "1" if self.session_resume else "0"
        )
        environment["AGENT_RESUME_RETRY_IDEMPOTENT"] = (
            "1" if self.retry_idempotent else "0"
        )
        for provider, path in self.real_binaries.items():
            if path:
                environment["AGENT_RESUME_REAL_{}".format(provider.upper())] = path
        return environment

    def _start_child(self, environment: Dict[str, str], use_pty: bool) -> None:
        """Launch the workflow in a new session, optionally preserving its TTY."""
        if use_pty:
            master_fd, slave_fd = pty.openpty()
            self.master_fd = master_fd
            self._copy_terminal_settings(slave_fd)
            self._resize_pty()
            self.child = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=environment,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
            os.close(slave_fd)
            os.set_blocking(master_fd, False)
            self.selector.register(master_fd, selectors.EVENT_READ, "pty")
            self.saved_tty = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())
            os.set_blocking(sys.stdin.fileno(), False)
            self.selector.register(sys.stdin.fileno(), selectors.EVENT_READ, "stdin")
        else:
            self.child = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=environment,
                start_new_session=True,
            )
        self.child_pgid = self.child.pid
        self._write_state()

    def _start_watchdog(self) -> None:
        """Launch a helper that resumes a stopped child if this process disappears."""
        if self.child_pgid is None:
            return
        read_fd, write_fd = os.pipe()
        environment = dict(os.environ)
        package_root = str(Path(__file__).resolve().parent.parent)
        environment["PYTHONPATH"] = package_root + os.pathsep + environment.get(
            "PYTHONPATH", ""
        )
        self.watchdog = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_resume.watchdog",
                str(read_fd),
                str(self.child_pgid),
            ],
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=environment,
        )
        os.close(read_fd)
        self.watchdog_write_fd = write_fd

    def _start_codex_monitor(self) -> None:
        """Begin direct Codex quota monitoring when running Codex interactively."""
        binary = self.real_binaries.get("codex")
        if not binary or self.codex_monitor is not None:
            return
        self.codex_monitor = CodexMonitor(
            binary,
            callback=lambda snapshot: self.monitor_events.put(("snapshot", snapshot)),
            error_callback=lambda detail: self.monitor_events.put(("error", detail)),
        )
        self.codex_monitor.start()

    def _event_loop(self) -> int:
        """Multiplex child I/O, shim IPC, signals, and reset deadlines until exit."""
        assert self.child is not None
        while True:
            self._drain_monitor_events()
            self._maybe_resume()
            if self.child.poll() is not None:
                self._drain_pty()
                return_code = int(self.child.returncode or 0)
                return 128 - return_code if return_code < 0 else return_code
            timeout = 0.1
            if self.paused and self.blocked_until is not None:
                timeout = max(0.0, self.blocked_until - time.time())
            for key, _ in self.selector.select(timeout):
                if key.data == "server":
                    self._accept_clients()
                elif key.data == "client":
                    self._read_client(key.fileobj)
                elif key.data == "pty":
                    self._read_pty()
                elif key.data == "stdin":
                    self._read_stdin()
                elif key.data == "signal":
                    self._drain_signal_pipe()
            if self.termination_signal is not None and self.child.poll() is None:
                # The signal was already forwarded. Continue pumping output until exit.
                pass

    def _accept_clients(self) -> None:
        assert self.server is not None
        while True:
            try:
                connection, _ = self.server.accept()
            except BlockingIOError:
                return
            connection.setblocking(False)
            self.client_buffers[connection] = bytearray()
            self.selector.register(connection, selectors.EVENT_READ, "client")

    def _read_client(self, connection: socket.socket) -> None:
        buffer = self.client_buffers.get(connection)
        if buffer is None:
            self._close_client(connection)
            return
        try:
            chunk = connection.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self._close_client(connection)
            return
        if not chunk:
            self._close_client(connection)
            return
        buffer.extend(chunk)
        if len(buffer) > 1024 * 1024:
            self._respond_and_close(connection, {"action": "fail", "detail": "request too large"})
            return
        if b"\n" not in buffer:
            return
        line = bytes(buffer).split(b"\n", 1)[0]
        try:
            message = json.loads(line.decode("utf-8"))
            if not isinstance(message, dict):
                raise ValueError("request must be an object")
            response = self._handle_request(message)
        except (UnicodeDecodeError, ValueError) as exc:
            response = {"action": "fail", "detail": str(exc)}
        self._respond_and_close(connection, response)

    def _handle_request(self, message: Dict[str, Any]) -> Dict[str, Any]:
        request_type = message.get("type")
        provider = str(message.get("provider") or "")
        if request_type == "gate":
            return self._handle_gate(provider)
        if request_type == "event":
            return self._handle_provider_event(provider, message)
        if request_type == "result":
            return self._handle_result(provider, message)
        return {"action": "fail", "detail": "unknown supervisor request"}

    def _handle_gate(self, provider: str) -> Dict[str, Any]:
        """Check quota at a provider invocation boundary before allowing it to run."""
        if not self._provider_enabled(provider):
            return {"action": "proceed", "capability": "disabled"}
        self.active_provider = provider
        if provider == "codex":
            binary = self.real_binaries.get("codex")
            if binary:
                try:
                    snapshot = read_rate_limits(binary)
                    self._apply_snapshot(snapshot, at_boundary=True)
                except Exception as exc:
                    self.capabilities["codex"] = "degraded: {}".format(exc)
                    self._log("Codex telemetry unavailable: {}".format(exc))
            else:
                self.capabilities["codex"] = "unavailable: executable not found"
        blocking = self._exhausted_windows(provider)
        if blocking:
            reset = self._reset_for(blocking)
            if reset is None:
                return {
                    "action": "fail",
                    "detail": "quota is exhausted but the provider supplied no reset time",
                }
            self._pause(provider, float(reset + self.reset_grace))
        self._write_state()
        return {"action": "proceed", "capability": self.capabilities.get(provider, "full")}

    def _handle_provider_event(
        self, provider: str, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Merge shim telemetry and schedule a pause for blocked quota windows."""
        if not self._provider_enabled(provider):
            return {"action": "ack"}
        self.active_provider = provider
        session_id = message.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_ids[provider] = session_id
        event = message.get("event")
        if event == "quota":
            incoming = []
            for value in message.get("windows", []):
                if isinstance(value, dict):
                    incoming.append(QuotaWindow.from_dict(value))
            self.windows[provider] = merge_windows(self.windows.get(provider, []), incoming)
            blocking = self._exhausted_windows(provider)
            if blocking:
                reset = self._reset_for(blocking)
                if reset is not None:
                    self._pause(provider, float(reset + self.reset_grace))
            self._write_state()
            return {"action": "ack"}
        if event == "session":
            self._write_state()
            return {"action": "ack"}
        if event == "quota_failure":
            reset = _optional_int(message.get("resets_at"))
            if reset is None:
                reset = self._reset_for(self._exhausted_windows(provider))
            recoverable = reset is not None and reset > time.time()
            self.last_failure[provider] = {
                "pending": True,
                "recoverable": recoverable,
                "session_id": self.session_ids.get(provider),
                "detail": message.get("detail"),
            }
            if recoverable:
                if not self._blocking_windows(provider):
                    self.windows[provider] = merge_windows(
                        self.windows.get(provider, []),
                        [
                            QuotaWindow(
                                kind=WINDOW_UNKNOWN,
                                used_percent=100,
                                resets_at=reset,
                                status=STATUS_REJECTED,
                                limit_id="failure",
                            )
                        ],
                    )
                self._pause(provider, float(reset + self.reset_grace))
            else:
                self._log(
                    "{} quota failure has no safe reset timestamp".format(provider)
                )
            self._write_state()
            return {
                "action": "ack",
                "recoverable": recoverable,
            }
        return {"action": "ack"}

    def _handle_result(
        self, provider: str, message: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Choose ordinary exit, session recovery, retry, or safe failure after a call."""
        exit_code = int(message.get("exit_code", 1))
        failure = self.last_failure.get(provider)
        if exit_code == 0:
            if failure:
                failure["pending"] = False
            return {"action": "exit"}
        if not failure or not failure.get("pending"):
            return {"action": "exit"}
        failure["pending"] = False
        if not failure.get("recoverable"):
            return {
                "action": "fail",
                "detail": "quota failure could not be assigned a safe reset time",
            }
        session_id = failure.get("session_id")
        if self.session_resume and isinstance(session_id, str) and session_id:
            return {"action": "resume_session", "session_id": session_id}
        if self.retry_idempotent:
            return {"action": "retry"}
        return {
            "action": "fail",
            "detail": "quota reset, but no safe live or session continuation was available",
        }

    def _apply_snapshot(
        self, snapshot: ProviderSnapshot, at_boundary: bool = True
    ) -> None:
        """Store a provider snapshot and defer/perform a pause at the safe boundary."""
        provider = snapshot.provider
        self.windows[provider] = merge_windows(
            self.windows.get(provider, []), snapshot.windows
        )
        self.capabilities[provider] = snapshot.capability
        if snapshot.session_id:
            self.session_ids[provider] = snapshot.session_id
        blocking = self._exhausted_windows(provider)
        if blocking:
            reset = self._reset_for(blocking)
            if reset is not None:
                until = float(reset + self.reset_grace)
                if at_boundary:
                    self._pause(provider, until)
                else:
                    self.pause_requested_provider = provider
                    self.pause_requested_until = until
                    self.state = STATE_PAUSE_PENDING
        self._write_state()

    def _pause(self, provider: str, until: float) -> None:
        """Stop the complete child process group until the specified reset deadline."""
        if until <= time.time():
            return
        if self.paused:
            self.blocked_until = max(float(self.blocked_until or 0), until)
            self.blocked_provider = provider
            self._write_state()
            return
        if self.child_pgid is None:
            return
        self.state = STATE_PAUSE_PENDING
        self._write_state()
        try:
            os.killpg(self.child_pgid, signal.SIGSTOP)
        except ProcessLookupError:
            return
        self.paused = True
        self.blocked_provider = provider
        self.blocked_until = until
        self.state = STATE_WAITING
        self._log(
            "{} usage paused until {}".format(
                provider, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until))
            )
        )
        self._write_state()

    def _maybe_resume(self) -> None:
        """Verify expiry where possible, then continue a process group paused for quota."""
        if (
            not self.paused
            and self.pause_requested_until is not None
            and time.time() >= self.pause_requested_until
        ):
            self._expire_reset_windows(self.pause_requested_provider)
            self.pause_requested_provider = None
            self.pause_requested_until = None
            self.state = STATE_RUNNING
            self._write_state()
        if not self.paused or self.blocked_until is None:
            return
        if time.time() < self.blocked_until:
            return
        provider = self.blocked_provider
        self.state = STATE_VERIFYING
        self._write_state()
        if provider == "codex":
            binary = self.real_binaries.get("codex")
            if binary:
                try:
                    snapshot = read_rate_limits(binary)
                    self.windows["codex"] = merge_windows(
                        self.windows.get("codex", []), snapshot.windows
                    )
                    blocking = self._exhausted_windows("codex")
                    if blocking:
                        reset = self._reset_for(blocking)
                        if reset is not None and reset + self.reset_grace > time.time():
                            self.blocked_until = float(reset + self.reset_grace)
                            self.state = STATE_WAITING
                            self._write_state()
                            return
                except Exception as exc:
                    self._log("Codex reset verification degraded: {}".format(exc))
        self._expire_reset_windows(provider)
        if self.child_pgid is not None:
            try:
                os.killpg(self.child_pgid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        self.paused = False
        self.blocked_until = None
        self.blocked_provider = None
        self.state = STATE_RUNNING
        self._log("{} usage window reset; workflow resumed".format(provider or "provider"))
        self._write_state()
        if self.pending_stdin and self.master_fd is not None:
            try:
                _write_all(self.master_fd, bytes(self.pending_stdin))
            except OSError:
                pass
            self.pending_stdin.clear()
        if (
            self.direct_provider
            and provider == self.direct_provider
            and self.last_failure.get(provider or "", {}).get("pending")
            and self.master_fd is not None
        ):
            # A typed interactive failure is now back at its prompt. Queue a native
            # continuation message through the PTY; ordinary proactive pauses do not.
            prompt = (
                "Continue the interrupted task. Inspect current state first and do not "
                "repeat completed side effects.\n"
            )
            try:
                os.write(self.master_fd, prompt.encode("utf-8"))
                self.last_failure[provider]["pending"] = False
            except OSError:
                pass

    def _blocking_windows(self, provider: str) -> List[QuotaWindow]:
        return [
            window
            for window in self.windows.get(provider, [])
            if window.blocks(self.threshold)
        ]

    def _exhausted_windows(self, provider: str) -> List[QuotaWindow]:
        now = time.time()
        result = []
        for window in self.windows.get(provider, []):
            if window.resets_at is not None and window.resets_at <= now:
                continue
            if (
                window.status == STATUS_REJECTED
                or window.used_percent >= self.threshold
            ):
                result.append(window)
        return result

    def _reset_for(self, windows: Sequence[QuotaWindow]) -> Optional[int]:
        now = time.time()
        explicit = [
            window.resets_at
            for window in windows
            if window.resets_at is not None and window.resets_at > now
        ]
        estimates = list(explicit)
        for window in windows:
            if window.resets_at is not None:
                continue
            duration = window.duration_minutes
            if duration is None and window.kind == "five_hour":
                duration = 5 * 60
            elif duration is None and window.kind == "weekly":
                duration = 7 * 24 * 60
            if duration is not None:
                estimates.append(window.observed_at + duration * 60)
        future = [value for value in estimates if value > now]
        if future and not explicit:
            self.capabilities[
                self.active_provider or self.blocked_provider or "unknown"
            ] = "degraded: reset time conservatively estimated from window class"
        return max(future) if future else None

    def _expire_reset_windows(self, provider: Optional[str]) -> None:
        if not provider:
            return
        now = time.time()
        for window in self.windows.get(provider, []):
            if window.resets_at is not None and window.resets_at <= now:
                window.used_percent = 0
                window.status = STATUS_ALLOWED
                window.observed_at = int(now)

    def _drain_monitor_events(self) -> None:
        """Apply asynchronous Codex snapshots without interrupting active output."""
        while True:
            try:
                event, value = self.monitor_events.get_nowait()
            except queue.Empty:
                return
            if event == "snapshot" and isinstance(value, ProviderSnapshot):
                # A direct Codex monitor can observe an update while a turn is
                # streaming. Defer SIGSTOP until the next terminal input boundary.
                self._apply_snapshot(value, at_boundary=False)
            elif event == "error":
                self.capabilities["codex"] = "degraded: {}".format(value)
                self._write_state()

    def _provider_enabled(self, provider: str) -> bool:
        if provider not in ("codex", "claude"):
            return False
        return self.provider_mode in ("auto", "both", provider)

    def _respond_and_close(
        self, connection: socket.socket, response: Dict[str, Any]
    ) -> None:
        try:
            connection.setblocking(True)
            connection.sendall(
                (json.dumps(response, separators=(",", ":")) + "\n").encode("utf-8")
            )
        except OSError:
            pass
        self._close_client(connection)

    def _close_client(self, connection: socket.socket) -> None:
        try:
            self.selector.unregister(connection)
        except Exception:
            pass
        self.client_buffers.pop(connection, None)
        try:
            connection.close()
        except OSError:
            pass

    def _read_pty(self) -> None:
        if self.master_fd is None:
            return
        try:
            data = os.read(self.master_fd, 65536)
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EBADF):
                raise
            return
        if data:
            _write_all(sys.stdout.fileno(), data)

    def _drain_pty(self) -> None:
        if self.master_fd is None:
            return
        while True:
            try:
                data = os.read(self.master_fd, 65536)
            except OSError:
                return
            if not data:
                return
            _write_all(sys.stdout.fileno(), data)

    def _read_stdin(self) -> None:
        if self.master_fd is None:
            return
        try:
            data = os.read(sys.stdin.fileno(), 65536)
        except BlockingIOError:
            return
        except OSError:
            return
        if data:
            if self.pause_requested_until is not None:
                until = self.pause_requested_until
                provider = self.pause_requested_provider or "codex"
                self.pause_requested_until = None
                self.pause_requested_provider = None
                if until > time.time():
                    self.pending_stdin.extend(data)
                    self._pause(provider, until)
                    return
            try:
                _write_all(self.master_fd, data)
            except OSError:
                pass
        else:
            try:
                self.selector.unregister(sys.stdin.fileno())
            except Exception:
                pass

    def _copy_terminal_settings(self, slave_fd: int) -> None:
        try:
            attributes = termios.tcgetattr(sys.stdin.fileno())
            termios.tcsetattr(slave_fd, termios.TCSANOW, attributes)
        except termios.error:
            pass

    def _resize_pty(self) -> None:
        if self.master_fd is None:
            return
        try:
            size = fcntl.ioctl(
                sys.stdin.fileno(), termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0)
            )
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, size)
        except OSError:
            pass

    def _install_signal_handlers(self) -> None:
        self.signal_read_fd, self.signal_write_fd = os.pipe()
        os.set_blocking(self.signal_read_fd, False)
        os.set_blocking(self.signal_write_fd, False)
        self.selector.register(self.signal_read_fd, selectors.EVENT_READ, "signal")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGWINCH):
            self._old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self._signal_handler)

    def _signal_handler(self, signum: int, _frame: Any) -> None:
        if self.signal_write_fd is not None:
            try:
                os.write(self.signal_write_fd, bytes((signum & 0xFF,)))
            except OSError:
                pass
        if signum == signal.SIGWINCH:
            self._resize_pty()
            return
        self.termination_signal = signum
        if self.paused and self.child_pgid is not None:
            try:
                os.killpg(self.child_pgid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            self.paused = False
        if self.child_pgid is not None:
            try:
                os.killpg(self.child_pgid, signum)
            except ProcessLookupError:
                pass

    def _drain_signal_pipe(self) -> None:
        if self.signal_read_fd is None:
            return
        while True:
            try:
                if not os.read(self.signal_read_fd, 4096):
                    return
            except BlockingIOError:
                return
            except OSError:
                return

    def _write_state(self, exit_code: Optional[int] = None) -> None:
        """Persist the current run state for status inspection and crash recovery."""
        provider = self.active_provider
        windows = self.windows.get(provider or "", [])
        state: Dict[str, Any] = {
            "run_id": self.run_id,
            "state": self.state,
            "provider": provider,
            "windows": [window.to_dict() for window in windows],
            "child_pid": self.child.pid if self.child is not None else None,
            "session_id": self.session_ids.get(provider or ""),
            "cwd": self.cwd,
            "command": self.command,
            "threshold": self.threshold,
            "reset_grace": self.reset_grace,
            "blocked_until": int(self.blocked_until)
            if self.blocked_until is not None
            else None,
            "capabilities": self.capabilities,
            "updated_at": time.time(),
        }
        if exit_code is not None:
            state["exit_code"] = exit_code
        atomic_write_json(self.state_path, state)

    def _log(self, message: str) -> None:
        if self.verbose:
            sys.stderr.write("[agent-resume] {}\n".format(message))
            sys.stderr.flush()

    def _cleanup(self) -> None:
        """Restore terminal/process state and tear down transient supervisor resources."""
        if self.codex_monitor is not None:
            self.codex_monitor.stop()
        if self.paused and self.child_pgid is not None:
            try:
                os.killpg(self.child_pgid, signal.SIGCONT)
            except ProcessLookupError:
                pass
            self.paused = False
        if self.watchdog_write_fd is not None:
            try:
                os.close(self.watchdog_write_fd)
            except OSError:
                pass
            self.watchdog_write_fd = None
        if self.watchdog is not None:
            try:
                self.watchdog.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.watchdog.terminate()
        if self.saved_tty is not None:
            try:
                termios.tcsetattr(
                    sys.stdin.fileno(), termios.TCSADRAIN, self.saved_tty
                )
                os.set_blocking(sys.stdin.fileno(), True)
            except (OSError, termios.error):
                pass
        for descriptor in (self.master_fd,):
            if descriptor is not None:
                try:
                    self.selector.unregister(descriptor)
                except Exception:
                    pass
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if self.signal_read_fd is not None:
            try:
                self.selector.unregister(self.signal_read_fd)
            except Exception:
                pass
        for descriptor in (self.signal_read_fd, self.signal_write_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        for connection in list(self.client_buffers):
            self._close_client(connection)
        if self.server is not None:
            try:
                self.selector.unregister(self.server)
            except Exception:
                pass
            try:
                self.server.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        for sig, handler in self._old_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass
        self.selector.close()


def _optional_int(value: Any) -> Optional[int]:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(descriptor, view)
            view = view[written:]
        except InterruptedError:
            continue
        except BlockingIOError:
            time.sleep(0.01)
