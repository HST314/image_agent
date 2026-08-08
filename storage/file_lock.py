"""Cross-platform file locking backed by portalocker."""

from __future__ import annotations

import os
from typing import IO, Any

import portalocker

LOCK_EX = portalocker.LOCK_EX
LOCK_SH = portalocker.LOCK_SH
LOCK_NB = portalocker.LOCK_NB


def _as_file(target: int | IO[Any]) -> tuple[IO[Any], bool]:
    if not isinstance(target, int):
        return target, False
    # Windows portalocker requires fileno(); the caller still owns this fd.
    # The descriptor is already opened O_RDWR by callers.  Avoid append mode:
    # it would mutate the descriptor flags and make later lock-owner metadata
    # writes append after a truncate on POSIX.
    return os.fdopen(target, "r+b", closefd=False), True


def lock(target: int | IO[Any], flags: int) -> None:
    stream, temporary = _as_file(target)
    try:
        portalocker.lock(stream, flags)
    except portalocker.exceptions.LockException as exc:
        if flags & LOCK_NB:
            raise BlockingIOError("file lock is already held") from exc
        raise
    finally:
        if temporary:
            stream.close()


def unlock(target: int | IO[Any]) -> None:
    stream, temporary = _as_file(target)
    try:
        portalocker.unlock(stream)
    finally:
        if temporary:
            stream.close()
