"""Generic master repository: injected policy and two-phase publication."""

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from master_repository import (
    GenericMasterRepository,
    MasterProtectionPolicy,
    MasterProvenance,
)


def fake_canonicalize(value: str) -> str:
    value = str(value).strip()
    if not value.startswith("https://example.com/"):
        raise ValueError("invalid_url")
    if not value.endswith("/"):
        value += "/"
    handle = value[len("https://example.com/") : -1]
    if not handle or "/" in handle:
        raise ValueError("invalid_url")
    return value


def fake_identity(value: str) -> str:
    return value.lower()


def make_repository(base_dir):
    return GenericMasterRepository(
        base_dir,
        platform_key="testplatform",
        canonicalize_url=fake_canonicalize,
        identity_key=fake_identity,
        protection_policy=MasterProtectionPolicy(),
    )


def provenance(final_sha="0" * 64):
    return MasterProvenance(
        platform_key="testplatform",
        session_id="session 1",
        run_id=str(uuid.uuid4()),
        generation_id=str(uuid.uuid4()),
        transaction_id=str(uuid.uuid4()),
        final_sha256=final_sha,
    )


class InjectedPolicyTests(unittest.TestCase):
    def test_publish_accepts_platform_urls_and_derives_txt(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            count = repository.publish(
                [
                    {"name": "https://example.com/Model-B/"},
                    {"name": "https://example.com/Model-A/"},
                ]
            )
            self.assertEqual(count, 2)
            data = json.loads(
                Path(repository.json_path).read_text(encoding="utf-8")
            )
            self.assertIsInstance(data, list)
            txt = [
                line.strip()
                for line in Path(repository.txt_path)
                .read_text(encoding="utf-8")
                .splitlines()
                if line.strip()
            ]
            self.assertEqual(
                txt,
                [entry["name"] for entry in data],
                "TXT must derive exactly from the JSON revision",
            )
            meta = json.loads(
                Path(repository.meta_path).read_text(encoding="utf-8")
            )
            self.assertEqual(meta["platform_key"], "testplatform")

    def test_wrong_host_urls_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            with self.assertRaises(ValueError):
                repository.publish(
                    [{"name": "https://other.com/Model-A/"}]
                )
            self.assertFalse(os.path.exists(repository.json_path))

    def test_identity_key_collision_is_a_hard_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            payload = [
                {"name": "https://example.com/Model-A/"},
                {"name": "https://example.com/MODEL-a/"},
            ]
            Path(repository.json_path).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                repository.load(required=True)

    def test_merge_keeps_existing_case_for_same_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            repository.publish(
                [{"name": "https://example.com/Model-A/", "date": "old"}]
            )
            repository.publish(
                [
                    {"name": "https://example.com/Model-A/", "date": "old"},
                    {"name": "https://example.com/MODEL-a/", "date": "new"},
                ]
            )
            entries = repository.load(required=True)
            self.assertEqual(len(entries), 1)
            self.assertEqual(
                entries[0]["name"], "https://example.com/Model-A/"
            )
            self.assertEqual(entries[0]["date"], "new")


class TwoPhasePublicationTests(unittest.TestCase):
    def test_prepare_touches_no_live_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            staging = Path(tmp, "staging")
            repository.prepare(
                [{"name": "https://example.com/Model-A/"}],
                provenance=provenance(),
                staging_dir=staging,
            )
            self.assertFalse(os.path.exists(repository.json_path))
            self.assertFalse(os.path.exists(repository.txt_path))
            self.assertTrue(
                os.path.exists(staging / "master_data.staged.json")
            )

    def test_commit_projects_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            staging = Path(tmp, "staging")
            prepared = repository.prepare(
                [{"name": "https://example.com/Model-A/"}],
                provenance=provenance(),
                staging_dir=staging,
            )
            first = repository.commit(prepared)
            self.assertTrue(first.changed)
            self.assertEqual(first.revision_id, prepared.revision_id)
            second = repository.commit(prepared)
            self.assertFalse(second.changed)
            self.assertEqual(second.revision_id, prepared.revision_id)
            entries = repository.load(required=True)
            self.assertEqual(len(entries), 1)

    def test_commit_detects_base_revision_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            repository.publish([{"name": "https://example.com/Model-A/"}])
            prepared = repository.prepare(
                [
                    {"name": "https://example.com/Model-A/"},
                    {"name": "https://example.com/Model-B/"},
                ],
                provenance=provenance(),
                staging_dir=Path(tmp, "staging"),
            )
            repository.publish(
                [{"name": "https://example.com/Model-C/"}]
            )
            result = repository.commit(prepared)
            self.assertFalse(result.changed)
            self.assertEqual(result.error_code, "master_revision_conflict")

    def test_validate_prepared_rejects_staging_tamper(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            prepared = repository.prepare(
                [{"name": "https://example.com/Model-A/"}],
                provenance=provenance(),
                staging_dir=Path(tmp, "staging"),
            )
            Path(prepared.staged_txt_path).write_text(
                "https://example.com/Tampered/\n", encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                repository.validate_prepared(prepared)

    def test_empty_revision_is_a_valid_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            repository.publish([{"name": "https://example.com/Model-A/"}])
            prepared = repository.prepare(
                [],
                provenance=provenance(),
                staging_dir=Path(tmp, "staging"),
            )
            result = repository.commit(prepared)
            self.assertTrue(result.changed)
            self.assertEqual(result.item_count, 0)
            self.assertEqual(repository.load(), [])
            self.assertEqual(
                Path(repository.txt_path).read_text(encoding="utf-8"), ""
            )


class ProtectionPolicyTests(unittest.TestCase):
    def test_accessible_removal_disabled_by_default_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = make_repository(tmp)
            repository.publish([{"name": "https://example.com/Model-A/"}])
            for run in ("run-1", "run-2", "run-3"):
                repository.apply_verification_records(
                    [
                        {
                            "original_url": "https://example.com/Model-A/",
                            "verdict": "accessible",
                            "reason_code": "player_present",
                            "attempt": 1,
                            "timestamp": "2026-07-28T00:00:00+00:00",
                            "observed_url": "https://example.com/Model-A/",
                            "run_id": run,
                        }
                    ]
                )
            entries = repository.load(required=True)
            self.assertEqual(
                len(entries), 1, "default policy never removes entries"
            )


if __name__ == "__main__":
    unittest.main()
