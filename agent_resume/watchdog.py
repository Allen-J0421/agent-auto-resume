from __future__ import annotations

import os
import signal
import sys
from typing import Iterable, Optional


def main(argv: Optional[Iterable[str]] = None) -> int:
    """Wait for supervisor EOF, then unfreeze its child process group if needed."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        return 2
    try:
        read_fd = int(args[0])
        process_group = int(args[1])
    except ValueError:
        return 2
    try:
        while os.read(read_fd, 1):
            pass
    except OSError:
        pass
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
    try:
        os.killpg(process_group, signal.SIGCONT)
    except (ProcessLookupError, PermissionError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
