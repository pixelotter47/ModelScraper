"""Deterministic workflow test doubles shared by safety regression tests."""

from __future__ import annotations

import datetime as _datetime
import threading
from dataclasses import dataclass, field


class FakeClock:
    def __init__(self, start="2026-07-24T12:00:00+00:00"):
        self._current = _datetime.datetime.fromisoformat(start)
        self.sleeps = []

    def now(self):
        return self._current

    def sleep(self, seconds):
        seconds = float(seconds)
        self.sleeps.append(seconds)
        self._current += _datetime.timedelta(seconds=seconds)


class FakeStopToken:
    def __init__(self, cancelled=False):
        self._cancelled = bool(cancelled)
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            self._cancelled = True

    def is_cancelled(self):
        with self._lock:
            return self._cancelled


@dataclass
class FakeVpnController:
    fail_on: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def _call(self, name):
        self.calls.append(name)
        if name in self.fail_on:
            raise RuntimeError(f"synthetic failure: {name}")

    def connect_ireland(self):
        self._call("connect_ireland")

    def disconnect(self):
        self._call("disconnect")

    def remove_chrome_from_split_tunnel(self):
        self._call("remove_chrome")

    def add_chrome_to_split_tunnel(self):
        self._call("add_chrome")
