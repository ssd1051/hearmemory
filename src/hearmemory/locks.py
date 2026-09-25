"""hearmemory.locks -- cross-process file locking.

file_lock is a context manager over fcntl.flock with a polling timeout (flock has
no native timeout). It yields True when the lock was acquired, False otherwise --
callers that get False must fall back to the lock-free spool (never block).
"""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Union

from .interfaces import LOCK_TIMEOUT_S_DEFAULT

_POLL_S = 0.01


@contextmanager
def file_lock(path: Union[str, "os.PathLike[str]"], timeout_s: float = LOCK_TIMEOUT_S_DEFAULT) -> Iterator[bool]:
    """Exclusive advisory lock on `path` (created if missing; parent dir must already exist).
    timeout_s <= 0 means "try once, non-blocking" (used for the pipeline lock)."""
    p = Path(path)
    fh = None
    acquired = False
    try:
        try:
            fh = open(p, "a+")
        except OSError:
            yield False
            return
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())))
        yield acquired
    finally:
        if fh is not None:
            if acquired:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                fh.close()
            except OSError:
                pass
