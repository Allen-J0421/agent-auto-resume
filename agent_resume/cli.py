from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from agent_resume import __version__
from agent_resume.diagnostics import run_doctor
from agent_resume.runtime import find_latest_state
from agent_resume.shim import run as run_shim
from agent_resume.supervisor import Supervisor


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse the public command line and dispatch to the selected operation."""
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    if raw_arguments and raw_arguments[0] == "_shim":
        if len(raw_arguments) < 2 or raw_arguments[1] not in ("codex", "claude"):
            return 2
        return run_shim(raw_arguments[1], raw_arguments[2:])
    parser = _parser()
    arguments = parser.parse_args(raw_arguments)

    if arguments.command_name == "doctor":
        result = run_doctor()
        if arguments.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_doctor(result)
        return 0 if result["ok"] else 1
    if arguments.command_name == "status":
        return _status(arguments.json)
    if arguments.command_name == "run":
        command = _strip_separator(arguments.program)
        if not command:
            parser.error("run requires -- <program> [args...]")
        return _supervise(arguments, command, direct_provider=None)
    if arguments.command_name == "cli":
        command = [arguments.cli_provider] + list(arguments.cli_args)
        return _supervise(arguments, command, direct_provider=arguments.cli_provider)
    parser.error("a command is required")
    return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-resume",
        description="Pause Codex and Claude CLI workflows across subscription resets.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    run_parser = subparsers.add_parser("run", help="supervise any workflow")
    _add_common_options(run_parser)
    run_parser.add_argument(
        "program", nargs=argparse.REMAINDER, help="program and arguments after --"
    )

    cli_parser = subparsers.add_parser("cli", help="run a guarded provider CLI")
    _add_common_options(cli_parser)
    cli_parser.add_argument("cli_provider", choices=("codex", "claude"))
    cli_parser.add_argument("cli_args", nargs=argparse.REMAINDER)

    doctor_parser = subparsers.add_parser(
        "doctor", help="check provider and process-control compatibility"
    )
    doctor_parser.add_argument("--json", action="store_true")

    status_parser = subparsers.add_parser(
        "status", help="show the latest run in this working directory"
    )
    status_parser.add_argument("--json", action="store_true")

    return parser


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--provider",
        choices=("auto", "codex", "claude", "both"),
        default="auto",
    )
    parser.add_argument("--threshold", type=float, default=98.0)
    parser.add_argument("--reset-grace", type=int, default=15)
    parser.add_argument("--no-session-resume", action="store_true")
    parser.add_argument("--retry-idempotent", action="store_true")
    parser.add_argument(
        "--interception-grace",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="warn if no provider invocation was intercepted after this long",
    )
    parser.add_argument(
        "--require-interception",
        action="store_true",
        help="exit 75 instead of continuing when the grace period passes "
        "without an intercepted provider invocation",
    )
    parser.add_argument("--verbose", action="store_true")


def _supervise(
    arguments: argparse.Namespace,
    command: Sequence[str],
    direct_provider: Optional[str],
) -> int:
    """Validate shared options and run one command under a Supervisor."""
    if not 0 <= arguments.threshold <= 100:
        raise SystemExit("agent-resume: --threshold must be between 0 and 100")
    if arguments.reset_grace < 0:
        raise SystemExit("agent-resume: --reset-grace cannot be negative")
    if arguments.interception_grace < 0:
        raise SystemExit("agent-resume: --interception-grace cannot be negative")
    if os.name != "posix" or not sys.platform.startswith(("darwin", "linux")):
        raise SystemExit("agent-resume: only macOS and Linux are supported")
    provider_mode = arguments.provider
    if direct_provider and provider_mode == "auto":
        provider_mode = direct_provider
    supervisor = Supervisor(
        command=command,
        cwd=os.getcwd(),
        provider_mode=provider_mode,
        threshold=arguments.threshold,
        reset_grace=arguments.reset_grace,
        session_resume=not arguments.no_session_resume,
        retry_idempotent=arguments.retry_idempotent,
        verbose=arguments.verbose,
        direct_provider=direct_provider,
        interception_grace=arguments.interception_grace,
        require_interception=arguments.require_interception,
    )
    try:
        return supervisor.run()
    except FileNotFoundError as exc:
        sys.stderr.write("agent-resume: command not found: {}\n".format(exc.filename))
        return 127
    except PermissionError as exc:
        if exc.filename:
            detail = "cannot execute {}: {}".format(exc.filename, exc)
        else:
            detail = "permission denied while creating process-control IPC: {}".format(
                exc
            )
        sys.stderr.write("agent-resume: {}\n".format(detail))
        return 126


def _status(as_json: bool) -> int:
    """Render the newest persisted run state for the current directory."""
    state = find_latest_state()
    if state is None:
        if as_json:
            print("null")
        else:
            print("No Agent Resume run found for {}".format(os.getcwd()))
        return 1
    public = _public_state(state)
    if as_json:
        print(json.dumps(public, indent=2, sort_keys=True))
        return 0
    provider = public["provider"] or "not observed"
    print("{} · {} · provider {}".format(public["run_id"], public["state"], provider))
    if public["child_pid"]:
        print("child pid: {}".format(public["child_pid"]))
    if public.get("interception") == "none":
        print(
            "interception: none — no provider invocation was intercepted; "
            "the workload was not protected"
        )
    elif public.get("interception_missing"):
        print(
            "interception: partial — never observed: {}".format(
                ", ".join(public["interception_missing"])
            )
        )
    for window in public["windows"]:
        reset = window.get("resets_at")
        reset_text = (
            time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(reset))
            if reset
            else "unknown"
        )
        print(
            "{}: {:.1f}% · {} · resets {}".format(
                window["kind"],
                float(window["used_percent"]),
                window["status"],
                reset_text,
            )
        )
    return 0


def _public_state(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": state.get("run_id"),
        "state": state.get("state"),
        "provider": state.get("provider"),
        "windows": state.get("windows", []),
        "child_pid": state.get("child_pid"),
        "session_id": state.get("session_id"),
        "interception": state.get("interception"),
        "interception_missing": state.get("interception_missing", []),
    }


def _print_doctor(result: Dict[str, Any]) -> None:
    for item in result["checks"]:
        marker = "ok" if item["ok"] else "warning"
        print("{:7} {:22} {}".format(marker, item["name"], item["detail"]))
    print()
    print("Known interception boundaries:")
    for limitation in result["limitations"]:
        print("- {}".format(limitation))


def _strip_separator(values: Sequence[str]) -> List[str]:
    result = list(values)
    if result and result[0] == "--":
        result.pop(0)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
