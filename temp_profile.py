"""Owned temporary Chrome profiles outside persisted session folders."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from public_runtime import public_state_root
from storage_utils import AtomicWriter
from workflow_types import utc_now_iso


MARKER = ".modelscraper-profile.json"
SCHEMA_VERSION = 1


class TemporaryChromeProfile:
    def __init__(
        self,
        run_id=None,
        *,
        root=None,
        logger=None,
        process_waiter=None,
        sleep=time.sleep,
        retries=5,
    ):
        runtime = root or public_state_root() / "chrome_profiles"
        self.root = Path(runtime).resolve()
        self.run_id = str(run_id or uuid.uuid4())
        self.logger = logger or (lambda _message: None)
        self.process_waiter = process_waiter
        self.sleep = sleep
        self.retries = max(1, int(retries))
        self.path = None
        self.cleanup_warning = None

    def create(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = Path(
            tempfile.mkdtemp(
                prefix=f"profile-{self.run_id[:8]}-", dir=self.root
            )
        ).resolve()
        AtomicWriter().write_json(
            self.path / MARKER,
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "created_at": utc_now_iso(),
                "pid": os.getpid(),
            },
        )
        return str(self.path)

    def cleanup(self):
        if self.path is None or not self.path.exists():
            return True
        self._validate_owned_path(self.path)
        if self.process_waiter:
            self.process_waiter(str(self.path), 5.0)
        last_error = None
        for attempt in range(self.retries):
            try:
                shutil.rmtree(self.path)
                self.path = None
                return True
            except OSError as exc:
                last_error = exc
                if attempt < self.retries - 1:
                    self.sleep(min(1.0, 0.1 * (2**attempt)))
        self.cleanup_warning = str(last_error)
        self._enqueue(self.path, self.cleanup_warning)
        self.logger(
            f"[WARN] Temporary Chrome profile queued for cleanup: {self.path}"
        )
        return False

    def __enter__(self):
        return self.create()

    def __exit__(self, exc_type, exc, traceback):
        self.cleanup()
        return False

    def _validate_owned_path(self, path):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root) or resolved == self.root:
            raise ValueError("temporary profile escapes owned root")
        if Path(path).is_symlink() or Path(path).is_junction():
            raise ValueError("temporary profile cannot be a link")
        marker = resolved / MARKER
        if not marker.is_file():
            raise ValueError("temporary profile ownership marker missing")
        value = json.loads(marker.read_text(encoding="utf-8"))
        if (
            value.get("schema_version") != SCHEMA_VERSION
            or value.get("run_id") != self.run_id
        ):
            raise ValueError("temporary profile ownership mismatch")

    def _enqueue(self, path, error):
        queue_path = self.root / "cleanup_pending.json"
        pending = []
        if queue_path.exists():
            try:
                value = json.loads(queue_path.read_text(encoding="utf-8"))
                if isinstance(value, list):
                    pending = value
            except (OSError, ValueError):
                pending = []
        pending = [
            item for item in pending if item.get("path") != str(path)
        ]
        pending.append(
            {
                "path": str(path),
                "run_id": self.run_id,
                "queued_at": utc_now_iso(),
                "error": str(error)[:200],
            }
        )
        AtomicWriter().write_json(queue_path, pending)


def cleanup_orphan_profiles(
    *,
    root=None,
    minimum_age_seconds=24 * 60 * 60,
    now=None,
    logger=None,
):
    manager = TemporaryChromeProfile(root=root, logger=logger)
    manager.root.mkdir(parents=True, exist_ok=True)
    current = float(now if now is not None else time.time())
    removed = []
    warnings = []
    for candidate in manager.root.iterdir():
        if not candidate.is_dir() or candidate.is_symlink():
            continue
        marker = candidate / MARKER
        if not marker.is_file():
            continue
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            if value.get("schema_version") != SCHEMA_VERSION:
                continue
            age = current - candidate.stat().st_mtime
            if age < minimum_age_seconds:
                continue
            owned = TemporaryChromeProfile(
                value.get("run_id"), root=manager.root, logger=logger
            )
            owned.path = candidate
            if owned.cleanup():
                removed.append(str(candidate))
            elif owned.cleanup_warning:
                warnings.append(owned.cleanup_warning)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(str(exc))
    return {"removed": tuple(removed), "warnings": tuple(warnings)}
