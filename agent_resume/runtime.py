from __future__ import annotations

import json
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def runtime_base() -> Path:
    """Return the per-user private directory used for transient run artifacts."""
    temporary_root = os.environ.get("TMPDIR") or tempfile.gettempdir()
    root = Path(os.path.realpath(temporary_root)) / "agent-resume" / str(os.getuid())
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def create_run_dir() -> Path:
    """Create a unique, owner-only directory for one supervised run."""
    path = runtime_base() / str(uuid.uuid4())
    path.mkdir(mode=0o700)
    return path


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    """Durably replace a JSON state file without exposing a partial write."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd = os.open(
        str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def find_latest_state(cwd: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Find the most recently updated run state associated with a directory."""
    expected = os.path.realpath(cwd or os.getcwd())
    candidates: List[Dict[str, Any]] = []
    try:
        children: Iterable[Path] = runtime_base().iterdir()
    except OSError:
        return None
    for child in children:
        if not child.is_dir():
            continue
        state = read_json(child / "state.json")
        if state is None:
            continue
        if os.path.realpath(str(state.get("cwd", ""))) != expected:
            continue
        state["_runtime_dir"] = str(child)
        candidates.append(state)
    if not candidates:
        return None
    return max(candidates, key=lambda value: float(value.get("updated_at", 0)))


def prune_old_runs(max_age_seconds: int = 7 * 24 * 60 * 60) -> None:
    """Remove stale private runtime directories on a best-effort basis."""
    cutoff = time.time() - max_age_seconds
    try:
        children = list(runtime_base().iterdir())
    except OSError:
        return
    for child in children:
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                _remove_tree(child)
        except OSError:
            continue


def _remove_tree(path: Path) -> None:
    for entry in path.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            _remove_tree(entry)
        else:
            try:
                entry.unlink()
            except OSError:
                pass
    try:
        path.rmdir()
    except OSError:
        pass
