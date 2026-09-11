import tempfile
import unittest
from pathlib import Path

from session_registry import SessionPathError, SessionRegistry


class SessionRegistryTests(unittest.TestCase):
    def test_platform_sessions_are_independent_and_contained(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots = {
                "Chaturbate": Path(tmp, "cb"),
                "MyFreeCams": Path(tmp, "mfc"),
                "Stripchat": Path(tmp, "sc"),
            }
            for root in roots.values():
                root.mkdir()
            sessions = {
                platform: root / "session 1 24.07.2026"
                for platform, root in roots.items()
            }
            for path in sessions.values():
                path.mkdir()
            registry = SessionRegistry(roots)
            for platform, path in sessions.items():
                registry.set(platform, path)
            self.assertEqual(
                registry.get("Chaturbate"),
                str(sessions["Chaturbate"].resolve()),
            )
            self.assertEqual(
                registry.get("MyFreeCams"),
                str(sessions["MyFreeCams"].resolve()),
            )
            with self.assertRaises(SessionPathError):
                registry.set("Chaturbate", sessions["MyFreeCams"])
