from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict, List, Sequence

from agent_resume.protocol import request
from agent_resume.providers.claude import augment_args, make_resume_args
from agent_resume.telemetry import StructuredTelemetry, uses_structured_stdout


RECOVERY_PROMPT = (
    "Continue the interrupted task from this saved session. Inspect the current "
    "working tree and transcript first, and do not repeat side effects that already "
    "completed before the usage limit."
)


def run(provider: str, args: Sequence[str]) -> int:
    """Gate one intercepted provider invocation and apply safe recovery instructions."""
    provider = provider.lower()
    if provider not in ("codex", "claude"):
        sys.stderr.write("agent-resume: unsupported shim provider {!r}\n".format(provider))
        return 2
    socket_path = os.environ.get("AGENT_RESUME_SOCKET")
    real_binary = os.environ.get("AGENT_RESUME_REAL_{}".format(provider.upper()))
    if not socket_path or not real_binary:
        sys.stderr.write("agent-resume: shim environment is incomplete\n")
        return 75

    original_args = list(args)
    current_args = _prepare_args(provider, original_args)
    recovery_count = 0
    while True:
        gate = _request(
            socket_path,
            {
                "type": "gate",
                "provider": provider,
                "pid": os.getpid(),
                "args": original_args,
            },
        )
        if gate.get("action") == "fail":
            _print_detail(gate)
            return 75

        try:
            return_code = _execute(
                provider, real_binary, current_args, socket_path
            )
        except OSError as exc:
            sys.stderr.write(
                "agent-resume: unable to run {}: {}\n".format(real_binary, exc)
            )
            return 127

        result = _request(
            socket_path,
            {
                "type": "result",
                "provider": provider,
                "pid": os.getpid(),
                "exit_code": return_code,
                "recovery_count": recovery_count,
            },
        )
        action = result.get("action", "exit")
        if action == "resume_session":
            session_id = result.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                return 75
            recovery_count += 1
            if provider == "claude":
                current_args = make_resume_args(
                    original_args,
                    session_id,
                    _required_env("AGENT_RESUME_CLAUDE_SETTINGS"),
                    _required_env("AGENT_RESUME_CLAUDE_PLUGIN"),
                )
            else:
                current_args = _codex_resume_args(original_args, session_id)
            continue
        if action == "retry":
            recovery_count += 1
            current_args = _prepare_args(provider, original_args)
            continue
        if action == "fail":
            _print_detail(result)
            return 75
        return return_code


def _prepare_args(provider: str, args: Sequence[str]) -> List[str]:
    if provider != "claude":
        return list(args)
    return augment_args(
        args,
        _required_env("AGENT_RESUME_CLAUDE_SETTINGS"),
        _required_env("AGENT_RESUME_CLAUDE_PLUGIN"),
    )


def _execute(
    provider: str,
    real_binary: str,
    args: Sequence[str],
    socket_path: str,
) -> int:
    """Run the real CLI, forwarding JSONL unchanged while extracting telemetry."""
    if not uses_structured_stdout(provider, args):
        return int(subprocess.run([real_binary] + list(args)).returncode)
    process = subprocess.Popen(
        [real_binary] + list(args),
        stdout=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdout is not None
    telemetry = StructuredTelemetry(provider)
    while True:
        line = process.stdout.readline()
        if not line:
            break
        try:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        except BrokenPipeError:
            process.terminate()
            break
        for event in telemetry.consume(line):
            _request(socket_path, event)
    return int(process.wait())


def _codex_resume_args(original_args: Sequence[str], session_id: str) -> List[str]:
    """Build Codex's native session-resume form from an intercepted invocation."""
    args = list(original_args)
    if args and args[0] in ("exec", "e"):
        return ["exec", "resume", session_id, RECOVERY_PROMPT]
    return ["resume", session_id, RECOVERY_PROMPT]


def _request(socket_path: str, value: Dict[str, object]) -> Dict[str, object]:
    try:
        return request(socket_path, value, timeout=8 * 24 * 60 * 60)
    except (OSError, ValueError) as exc:
        sys.stderr.write("agent-resume: supervisor communication failed: {}\n".format(exc))
        return {"action": "fail"}


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError("missing {}".format(name))
    return value


def _print_detail(value: Dict[str, object]) -> None:
    detail = value.get("detail")
    if detail:
        sys.stderr.write("agent-resume: {}\n".format(detail))
