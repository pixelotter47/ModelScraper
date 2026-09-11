"""Validated same-directory atomic file persistence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable


class AtomicWriter:
    def __init__(self, fault_injector: Callable[[str, Path], None] | None = None):
        self.fault_injector = fault_injector

    def _inject(self, stage: str, path: Path) -> None:
        if self.fault_injector:
            self.fault_injector(stage, path)

    def write_bytes(
        self,
        path: str | os.PathLike[str],
        payload: bytes,
        validator: Callable[[bytes], None] | None = None,
    ) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        self._inject("before_write", target)
        try:
            fd, raw_temp = tempfile.mkstemp(
                prefix=f".{target.name}.modelscraper-",
                suffix=".tmp",
                dir=target.parent,
            )
            temp_path = Path(raw_temp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                self._inject("after_write", target)
                self._inject("before_fsync", target)
                os.fsync(handle.fileno())
            if validator:
                validator(temp_path.read_bytes())
            self._inject("before_replace", target)
            os.replace(temp_path, target)
            temp_path = None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def write_text(
        self, path: str | os.PathLike[str], text: str
    ) -> None:
        self.write_bytes(path, text.encode("utf-8"))

    def write_lines(
        self, path: str | os.PathLike[str], lines: Iterable[str]
    ) -> None:
        normalized = [str(item).rstrip("\r\n") for item in lines]
        text = "".join(f"{item}\n" for item in normalized)
        self.write_text(path, text)

    def write_json(
        self, path: str | os.PathLike[str], value: Any
    ) -> None:
        payload = (
            json.dumps(value, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")

        def validate(raw: bytes) -> None:
            json.loads(raw.decode("utf-8"))

        self.write_bytes(path, payload, validate)


_DEFAULT_WRITER = AtomicWriter()


def atomic_write_text(path, text):
    _DEFAULT_WRITER.write_text(path, text)


def atomic_write_lines(path, lines):
    _DEFAULT_WRITER.write_lines(path, lines)


def atomic_write_json(path, value):
    _DEFAULT_WRITER.write_json(path, value)


def canonical_json_bytes(value):
    """Deterministic UTF-8 JSON serialization so hashes are reproducible."""
    payload = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    return (payload + "\n").encode("utf-8")


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def read_validated_json(path, validator=None):
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if validator:
        validator(value)
    return value


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
