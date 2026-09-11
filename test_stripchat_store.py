"""Stripchat spec, config digest, run store, legacy inspector, backups."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from stripchat_store import (
    DEFAULT_STRIPCHAT_CONFIG,
    MasterBackupError,
    STRIPCHAT_SPEC,
    StripchatRunStore,
    build_store_config,
    compute_config_digest,
    create_master_backup,
    inspect_legacy_session,
    inspect_legacy_sessions,
    stripchat_master_repository,
    validate_master_backup,
)


class SpecTests(unittest.TestCase):
    def test_spec_identity_and_filenames(self):
        self.assertEqual(STRIPCHAT_SPEC.key, "stripchat")
        self.assertEqual(STRIPCHAT_SPEC.session_root_name, "sc sessions")
        self.assertEqual(STRIPCHAT_SPEC.artifacts.vpn, "sc_vpn_list.txt")
        self.assertEqual(STRIPCHAT_SPEC.artifacts.local, "sc_local_list.txt")
        self.assertEqual(
            STRIPCHAT_SPEC.artifacts.candidates, "sc_candidates.txt"
        )
        self.assertEqual(STRIPCHAT_SPEC.artifacts.metadata, "sc_metadata.json")
        self.assertEqual(STRIPCHAT_SPEC.artifacts.debug, "sc_debug_log.txt")
        self.assertEqual(STRIPCHAT_SPEC.artifacts.final, "FINAL_BLOCKED.txt")

    def test_capabilities_match_the_plan(self):
        capabilities = STRIPCHAT_SPEC.capabilities
        self.assertTrue(capabilities.resumable_verification)
        self.assertTrue(capabilities.adaptive_api_snapshots)
        self.assertTrue(capabilities.machine_policy_required)
        self.assertFalse(capabilities.legacy_adoption_supported)


class ConfigDigestTests(unittest.TestCase):
    def test_digest_is_stable_and_value_sensitive(self):
        first = compute_config_digest(DEFAULT_STRIPCHAT_CONFIG)
        second = compute_config_digest(dict(DEFAULT_STRIPCHAT_CONFIG))
        self.assertEqual(first, second)
        changed = dict(DEFAULT_STRIPCHAT_CONFIG)
        changed["jaccard_threshold"] = 0.99
        self.assertNotEqual(first, compute_config_digest(changed))

    def test_unknown_override_is_rejected(self):
        with self.assertRaises(ValueError):
            build_store_config({"nonsense": True})

    def test_store_config_carries_versions(self):
        config = build_store_config()
        self.assertEqual(config["canonicalizer_version"], "sc-url-1")
        # sc-page-2 corrected the offline/not-found evidence and stopped
        # gating a decided page on the load event; the bump is what forces
        # a new generation instead of reusing sc-page-1 verdicts.
        self.assertEqual(config["classifier_version"], "sc-page-2")
        self.assertEqual(config["api_contract_version"], "sc-api-2")


class RunStoreTests(unittest.TestCase):
    def test_creates_v2_stripchat_run(self):
        with tempfile.TemporaryDirectory() as root:
            session = Path(root, "session 7 28.07.2026")
            session.mkdir()
            store = StripchatRunStore(session)
            context = store.create_run(mode="full_auto", source="test")
            manifest = store.load_manifest(context)
            self.assertEqual(manifest["platform_key"], "stripchat")
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(
                manifest["config"]["canonicalizer_version"], "sc-url-1"
            )

    def test_session_root_containment_is_enforced(self):
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root, "outside")
            outside.mkdir()
            with self.assertRaises(Exception):
                StripchatRunStore(
                    outside, session_root=Path(root, "sc sessions")
                )


class MasterRepositoryConfigurationTests(unittest.TestCase):
    def test_accepts_stripchat_urls_and_rejects_foreign_hosts(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = stripchat_master_repository(tmp)
            count = repository.publish(
                [
                    {"name": "https://stripchat.com/Synthetic-Model@1/"},
                    {"name": "https://stripchat.com/Other_Model-2/"},
                ]
            )
            self.assertEqual(count, 2)
            with self.assertRaises(ValueError):
                repository.publish(
                    [{"name": "https://chaturbate.com/model/"}]
                )

    def test_case_fold_collision_fails_hard(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = stripchat_master_repository(tmp)
            payload = [
                {"name": "https://stripchat.com/Model-A/"},
                {"name": "https://stripchat.com/MODEL-a/"},
            ]
            Path(repository.json_path).write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                repository.load(required=True)

    def test_synthetic_master_validates_read_only(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        master_dir = temporary.name
        json_path = os.path.join(master_dir, "MASTER_BLOCKED_DATA.json")
        urls = ["https://stripchat.com/Synthetic-Model@1/", "https://stripchat.com/synthetic_model_2/"]
        Path(json_path).write_text(json.dumps([{"name": url} for url in urls]), encoding="utf-8")
        Path(master_dir, "MASTER_BLOCKED.txt").write_text("\n".join(sorted(urls)) + "\n", encoding="utf-8")
        before = Path(json_path).read_bytes()
        repository = stripchat_master_repository(master_dir)
        entries = repository.load(required=True)
        consistency = repository.validate_consistency(repair_txt=False)
        self.assertTrue(consistency.valid, consistency.message)
        self.assertEqual(consistency.json_count, len(entries))
        self.assertEqual(
            Path(json_path).read_bytes(),
            before,
            "read-only validation must not change a byte",
        )


class LegacyInspectorTests(unittest.TestCase):
    def _write(self, path, text="x\n"):
        Path(path).write_text(text, encoding="utf-8")

    def test_classifications(self):
        with tempfile.TemporaryDirectory() as root:
            complete = Path(root, "session 1 22.01.2026")
            complete.mkdir()
            self._write(complete / "FINAL_BLOCKED.txt")

            incomplete = Path(root, "session 2 22.01.2026")
            incomplete.mkdir()
            self._write(incomplete / "sc_vpn_list.txt")

            invalid = Path(root, "session 3 22.01.2026")
            invalid.mkdir()

            ambiguous = Path(root, "session 4 22.01.2026")
            ambiguous.mkdir()
            self._write(ambiguous / "FINAL_BLOCKED.txt", "")
            time.sleep(0.05)
            self._write(ambiguous / "sc_vpn_list.txt")
            newer = time.time() + 10
            os.utime(ambiguous / "sc_vpn_list.txt", (newer, newer))

            hardened = Path(root, "session 5 28.07.2026")
            hardened.mkdir()
            self._write(hardened / "run_state.json", "{}")

            reports = {
                report["session_id"]: report["classification"]
                for report in inspect_legacy_sessions(root)
            }
            self.assertEqual(reports["session 1 22.01.2026"], "legacy_complete")
            self.assertEqual(
                reports["session 2 22.01.2026"], "legacy_incomplete"
            )
            self.assertEqual(reports["session 3 22.01.2026"], "legacy_invalid")
            self.assertEqual(
                reports["session 4 22.01.2026"], "legacy_ambiguous"
            )
            self.assertEqual(reports["session 5 28.07.2026"], "hardened")

    def test_inspection_never_mutates_and_never_resumes_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            session = Path(root, "session 1 22.01.2026")
            session.mkdir()
            self._write(session / "FINAL_BLOCKED.txt", "line\n")
            before = (session / "FINAL_BLOCKED.txt").read_bytes()
            report = inspect_legacy_session(session)
            self.assertFalse(report["resumable"])
            self.assertEqual(
                (session / "FINAL_BLOCKED.txt").read_bytes(), before
            )

    def test_report_contains_no_model_urls(self):
        with tempfile.TemporaryDirectory() as root:
            session = Path(root, "session 1 22.01.2026")
            session.mkdir()
            self._write(
                session / "FINAL_BLOCKED.txt",
                "https://stripchat.com/Real-Handle@1/\n",
            )
            report = inspect_legacy_session(session)
            self.assertNotIn("Real-Handle@1", json.dumps(report))


class MasterBackupTests(unittest.TestCase):
    def _seed_master(self, base_dir):
        repository = stripchat_master_repository(base_dir)
        repository.publish(
            [
                {"name": "https://stripchat.com/Synthetic-Model@1/"},
                {"name": "https://stripchat.com/Other_Model-2/"},
            ]
        )
        return repository

    def test_backup_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_master(tmp)
            backups = Path(tmp, "backups")
            result = create_master_backup(
                tmp,
                workspace_id="ws-test",
                operation="test",
                backups_root=backups,
            )
            validation = validate_master_backup(result["path"])
            self.assertTrue(validation["valid"], validation.get("reason"))
            self.assertEqual(validation["manifest"]["item_count"], 2)

    def test_backup_refuses_missing_or_invalid_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(MasterBackupError) as caught:
                create_master_backup(
                    tmp,
                    workspace_id="ws-test",
                    operation="test",
                    backups_root=Path(tmp, "backups"),
                )
            self.assertEqual(caught.exception.code, "backup_source_missing")
            repository = self._seed_master(tmp)
            Path(repository.txt_path).write_text(
                "https://stripchat.com/Drifted-Model/\n", encoding="utf-8"
            )
            with self.assertRaises(MasterBackupError) as caught:
                create_master_backup(
                    tmp,
                    workspace_id="ws-test",
                    operation="test",
                    backups_root=Path(tmp, "backups"),
                )
            self.assertEqual(caught.exception.code, "backup_source_invalid")

    def test_backup_tamper_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._seed_master(tmp)
            result = create_master_backup(
                tmp,
                workspace_id="ws-test",
                operation="test",
                backups_root=Path(tmp, "backups"),
            )
            target = Path(result["path"], "MASTER_BLOCKED.txt")
            target.write_text("tampered\n", encoding="utf-8")
            validation = validate_master_backup(result["path"])
            self.assertFalse(validation["valid"])

    def test_live_master_is_untouched_by_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = self._seed_master(tmp)
            before = Path(repository.json_path).read_bytes()
            create_master_backup(
                tmp,
                workspace_id="ws-test",
                operation="test",
                backups_root=Path(tmp, "backups"),
            )
            self.assertEqual(Path(repository.json_path).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
