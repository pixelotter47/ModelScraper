import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from public_runtime import public_state_root
from temp_profile import (
    MARKER,
    TemporaryChromeProfile,
    cleanup_orphan_profiles,
)


class TemporaryProfileTests(unittest.TestCase):
    def test_cleanup_does_not_require_python312_junction_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TemporaryChromeProfile("compatibility", root=Path(tmp, "runtime"))
            manager.create()
            with mock.patch.object(
                Path, "is_junction", create=True,
                side_effect=AssertionError("Path.is_junction is unavailable on Python 3.11"),
            ):
                self.assertTrue(manager.cleanup())

    @unittest.skipUnless(os.name == "nt", "Windows reparse-point ownership guard")
    def test_cleanup_rejects_reparse_points_before_removing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TemporaryChromeProfile("junction", root=Path(tmp, "runtime"))
            path = Path(manager.create())
            attributes = mock.Mock(
                st_mode=stat.S_IFDIR,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
            )
            with mock.patch.object(Path, "lstat", return_value=attributes):
                with mock.patch("temp_profile.shutil.rmtree") as remove:
                    with self.assertRaisesRegex(ValueError, "cannot be a link"):
                        manager.cleanup()
            remove.assert_not_called()
            self.assertTrue(path.exists())

    def test_public_cleanup_never_scans_personal_profile_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            personal_root = Path(tmp, "ModelScraper", "chrome_profiles")
            personal = TemporaryChromeProfile("personal", root=personal_root)
            personal_path = Path(personal.create())
            before = (personal_path / MARKER).read_bytes()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                public = TemporaryChromeProfile("public")
                public_path = Path(public.create())
                self.assertTrue(public_path.is_relative_to(public_state_root().resolve()))
                result = cleanup_orphan_profiles(minimum_age_seconds=0)
            self.assertIn(str(public_path), result["removed"])
            self.assertTrue(personal_path.exists())
            self.assertEqual((personal_path / MARKER).read_bytes(), before)

    def test_different_checkouts_get_different_profile_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}):
                first = public_state_root(Path(tmp, "checkout-a"))
                second = public_state_root(Path(tmp, "checkout-b"))
            self.assertNotEqual(first, second)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_profile_is_outside_session_and_removed_on_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp, "cb sessions", "session 1")
            session.mkdir(parents=True)
            root = Path(tmp, "runtime")
            manager = TemporaryChromeProfile("run-1", root=root)
            path = Path(manager.create())
            self.assertTrue(path.is_relative_to(root.resolve()))
            self.assertFalse(path.is_relative_to(session.resolve()))
            self.assertTrue(Path(path, MARKER).is_file())
            self.assertTrue(manager.cleanup())
            self.assertFalse(path.exists())

    def test_locked_cleanup_is_retried_and_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = TemporaryChromeProfile(
                "run-2", root=Path(tmp, "runtime"), sleep=lambda _x: None
            )
            path = Path(manager.create())
            with mock.patch(
                "temp_profile.shutil.rmtree",
                side_effect=PermissionError("locked"),
            ) as remove:
                self.assertFalse(manager.cleanup())
            self.assertEqual(remove.call_count, manager.retries)
            queue = json.loads(
                Path(manager.root, "cleanup_pending.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(queue[0]["path"], str(path))
            self.assertTrue(path.exists())

    def test_startup_scavenger_ignores_unowned_and_recent_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "runtime")
            root.mkdir()
            unowned = root / "unowned"
            unowned.mkdir()
            recent = TemporaryChromeProfile("recent", root=root)
            recent_path = Path(recent.create())
            result = cleanup_orphan_profiles(
                root=root,
                minimum_age_seconds=3600,
                now=recent_path.stat().st_mtime,
            )
            self.assertEqual(result["removed"], ())
            self.assertTrue(unowned.exists())
            self.assertTrue(recent_path.exists())
