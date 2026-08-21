"""Filesystem primitives shared by every writing verb.

Two guarantees the rest of the tool depends on: no reader ever observes a
partially written file, and no two writers ever interleave. Both matter here
because a cron scan, an in-session skill run, and a manual invocation can
overlap by design.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockBusy(RuntimeError):
    """Raised when the state lock could not be acquired within the timeout."""


@contextmanager
def state_lock(state_dir: Path, timeout: float = 30.0):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / ".lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy(
                        f"another psum run holds {lock_path}; try again shortly"
                    )
                time.sleep(0.1)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write(path: Path, text: str) -> bool:
    """Write text via temp+rename. Return True only if the content changed.

    The no-change short-circuit is what keeps an unchanged project from
    producing a git diff, a vault modification, and a phone notification on
    every single run.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text() == text:
                return False
        except (OSError, UnicodeDecodeError):
            pass
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return True


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def facts_error(path: Path) -> str:
    """The right stderr message when `read_json(path)` returned its default.

    read_json cannot distinguish "never scanned" from "corrupt" -- both come
    back as the default -- and every caller used to print the same
    run-a-scan message for both. Telling a reader to run `psum scan` when the
    file is actually malformed sends them to a command that will not fix it,
    and costs them a scan to find that out.
    """
    if path.exists():
        return f"{path} is unreadable or malformed — inspect it, or re-run `psum scan`"
    return f"no {path.name} — run `psum scan` first"
