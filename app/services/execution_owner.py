from __future__ import annotations

import os
from pathlib import Path


class ExecutionOwnerError(RuntimeError):
    """The service cannot safely become this database's execution owner."""


class ExecutionOwnerLock:
    """Hold an OS lock for the entire local execution service lifetime.

    Keep the lock file in place after release: unlinking it would let another
    process lock a different inode while an existing owner still holds it.
    The OS releases ownership even after a crash or forced process exit.
    """

    def __init__(self, database_path: Path):
        database = Path(database_path).expanduser().resolve()
        self.path = database.with_name(f"{database.name}.execution.lock")
        self._fd: int | None = None

    def __enter__(self) -> ExecutionOwnerLock:
        if self._fd is not None:
            raise RuntimeError("Execution ownership is already held by this lock.")
        try:
            import fcntl
        except ImportError as exc:
            # Never silently run without duplicate-execution protection.
            raise ExecutionOwnerError(
                "This platform does not support Kalshi Model's execution-owner lock."
            ) from exc
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise ExecutionOwnerError(
                f"Cannot acquire Kalshi Model execution ownership at {self.path}: {exc}"
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ExecutionOwnerError(
                "Kalshi Model is already running for this database. "
                "Close the existing instance before launching another. "
                f"Execution lock: {self.path}"
            ) from exc
        except OSError as exc:
            os.close(fd)
            raise ExecutionOwnerError(
                f"Cannot lock Kalshi Model execution ownership at {self.path}: {exc}"
            ) from exc
        self._fd = fd
        return self

    def __exit__(self, *_args: object) -> None:
        if self._fd is not None:
            # Closing releases flock without replacing the shared lock inode.
            os.close(self._fd)
            self._fd = None
