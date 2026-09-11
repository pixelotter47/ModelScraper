"""Dependency-free cross-process ModelScraper workspace lease."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from workflow_types import utc_now_iso


class WorkflowBusyError(RuntimeError):
    def __init__(self, owner=None):
        self.owner = owner or {}
        super().__init__("workflow_already_running")


class WorkflowLease:
    def __init__(
        self,
        workspace,
        *,
        run_id="",
        platform="",
        task="",
        runtime_dir=None,
    ):
        normalized = os.path.normcase(
            os.path.abspath(os.fspath(workspace))
        )
        key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        if runtime_dir is None:
            root = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
            runtime_dir = os.path.join(root, "ModelScraper", "locks")
        self.runtime_dir = Path(runtime_dir)
        self.path = self.runtime_dir / f"{key}.lock"
        self.run_id = str(run_id or "")
        self.platform = str(platform or "")
        self.task = str(task or "")
        self._handle = None

    def acquire(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            self._lock(handle)
        except OSError as exc:
            owner = self._read_owner(handle)
            handle.close()
            raise WorkflowBusyError(owner) from exc
        owner = {
            "pid": os.getpid(),
            "run_id": self.run_id,
            "platform": self.platform,
            "task": self.task,
            "acquired_at": utc_now_iso(),
        }
        handle.seek(1)
        handle.truncate()
        handle.write(json.dumps(owner).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def release(self):
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            handle.seek(0)
            self._unlock(handle)
        finally:
            handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False

    @staticmethod
    def _read_owner(handle):
        try:
            handle.seek(1)
            raw = handle.read(4096)
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _lock(handle):
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle):
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
