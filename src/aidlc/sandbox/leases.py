"""Local, cross-process leases shared by execution and orphan recovery."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re

from aidlc.config import Settings

JOB_NAME = re.compile(r"^aidlc-sandbox-[a-f0-9]{32}$")
CONTAINER_ID = re.compile(r"^[a-f0-9]{64}$")


def sandbox_owner(settings: Settings) -> str:
    return hashlib.sha256(str(settings.data_dir.resolve()).encode()).hexdigest()


class JobLease:
    """The kernel releases the lock even after SIGKILL; no PID/age heuristics.

    Keep lease files in place: unlinking a locked inode can let another process
    create and lock a different inode for the same job. Files contain no source.
    All sandbox services for one data directory must share this local filesystem.
    """

    def __init__(self, settings: Settings, name: str):
        if not JOB_NAME.fullmatch(name):
            raise ValueError("Invalid sandbox job name")
        directory = settings.data_dir / "sandbox-leases"
        directory.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(directory / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.fd)
            raise

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> JobLease:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
