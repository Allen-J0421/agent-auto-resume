# Contributing to Agent Resume

Thanks for helping improve Agent Resume. This project supervises real command
workflows, so changes should favor safe, observable behavior over aggressive
automatic recovery.

## Before opening a pull request

1. Create a focused branch and keep unrelated formatting changes out of it.
2. Add or update tests for behavior changes, especially around quota handling,
   process control, and session recovery.
3. Run the local checks:

   ```bash
   python3 -m unittest discover -v
   python3 -m compileall -q agent_resume
   ```

4. Update the README when a command, option, safety guarantee, or provider
   compatibility expectation changes.

## Pull request guidance

Explain the user-visible behavior, safety tradeoffs, and test coverage in the
pull request description. Avoid adding provider credentials, account data,
recorded CLI transcripts, or temporary runtime directories to the repository.

Changes that replay work, alter process-group signals, or weaken recovery
fallbacks need particular care: the default behavior should remain to avoid
duplicating side effects.

## Reporting bugs

Please include the operating system, Python version, provider CLI/version,
exact command, expected behavior, actual behavior, and a redacted verbose log
if available. Do not post secrets, session transcripts, or account identifiers.

Security-sensitive reports should follow [SECURITY.md](SECURITY.md) instead of
being filed as public issues.
