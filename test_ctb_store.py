import json
import os
import tempfile
import unittest
from pathlib import Path

from ctb_store import ChaturbateRunStore, MasterRepository
from ctb_verifier import candidate_digest
from storage_utils import AtomicWriter
from workflow_types import (
    StepName,
    StepOutcome,
    VerificationRecord,
    VerificationVerdict,
)


class AtomicStorageTests(unittest.TestCase):
    def test_atomic_write_failure_keeps_previous_valid_file(self):
        for stage in (
            "before_write",
            "after_write",
            "before_fsync",
            "before_replace",
        ):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp, "value.json")
                target.write_bytes(b'{"old": true}\n')

                def inject(current, _path):
                    if current == stage:
                        raise OSError("synthetic write failure")

                with self.assertRaises(OSError):
                    AtomicWriter(inject).write_json(target, {"new": True})
                self.assertEqual(target.read_bytes(), b'{"old": true}\n')
                self.assertFalse(
                    list(target.parent.glob(".value.json.modelscraper-*.tmp"))
                )


class ChaturbateRunStoreTests(unittest.TestCase):
    def test_create_record_publish_and_reload_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run(mode="full_auto")
            record = store.artifact_lines(
                context,
                StepName.VPN_SNAPSHOT,
                "models.txt",
                ["https://chaturbate.com/synthetic_a/"],
                "vpn_snapshot",
            )
            outcome = StepOutcome.succeeded(
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform,
                step=StepName.VPN_SNAPSHOT,
                output_count=1,
                artifacts=(record,),
            )
            store.record_step(context, outcome)
            store.publish_lines(context, record, "cb_vpn_list.txt")
            self.assertEqual(store.load_active(), context)
            self.assertEqual(
                Path(tmp, "cb_vpn_list.txt").read_text(encoding="utf-8"),
                "https://chaturbate.com/synthetic_a/\n",
            )

    def _seed_checkpoint(self, store, context, urls, verdicts):
        store.write_checkpoint(
            context,
            {
                "candidate_sha256": candidate_digest(urls),
                "candidate_urls": list(urls),
                "verdicts": verdicts,
                "attempts": {url: 1 for url in verdicts},
            },
        )

    def test_prune_checkpoint_keeps_verdicts_for_remaining_urls(self):
        alice = "https://chaturbate.com/alice/"
        bob = "https://chaturbate.com/bob/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            verdict = VerificationRecord(
                original_url=bob,
                verdict=VerificationVerdict.BLOCKED,
                reason_code="denied_notice",
                attempt=1,
                timestamp="2026-07-24T12:00:00+00:00",
                observed_url=bob,
                run_id=context.run_id,
            ).to_dict()
            self._seed_checkpoint(store, context, [alice, bob], {bob: verdict})

            self.assertTrue(store.prune_checkpoint(context, [bob]))

            pruned = store.load_checkpoint(context)
            self.assertEqual(pruned["candidate_urls"], [bob])
            self.assertEqual(pruned["candidate_sha256"], candidate_digest([bob]))
            self.assertEqual(list(pruned["verdicts"]), [bob])
            self.assertEqual(list(pruned["attempts"]), [bob])

    def test_prune_checkpoint_is_a_noop_when_nothing_is_removed(self):
        bob = "https://chaturbate.com/bob/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            self._seed_checkpoint(store, context, [bob], {})
            self.assertFalse(store.prune_checkpoint(context, [bob]))

    def test_prune_checkpoint_refuses_to_add_unknown_urls(self):
        alice = "https://chaturbate.com/alice/"
        bob = "https://chaturbate.com/bob/"
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            self._seed_checkpoint(store, context, [bob], {})
            self.assertFalse(store.prune_checkpoint(context, [alice, bob]))
            self.assertEqual(
                store.load_checkpoint(context)["candidate_urls"], [bob]
            )

    def test_prune_checkpoint_without_checkpoint_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            self.assertFalse(
                store.prune_checkpoint(context, ["https://chaturbate.com/bob/"])
            )

    def test_platform_or_generation_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ChaturbateRunStore(tmp)
            context = store.create_run()
            bad = StepOutcome.succeeded(
                run_id=context.run_id,
                generation_id="other",
                platform=context.platform,
                step=StepName.COMPARE,
            )
            with self.assertRaises(ValueError):
                store.record_step(context, bad)

    def test_legacy_adoption_is_explicit_copy_only_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            originals = {}
            for name, content in (
                ("cb_vpn_list.txt", "https://chaturbate.com/synthetic_a/\n"),
                ("cb_local_list.txt", ""),
                ("cb_candidates.txt", "https://chaturbate.com/synthetic_a/\n"),
            ):
                path = Path(tmp, name)
                path.write_text(content, encoding="utf-8")
                originals[name] = path.read_bytes()
            store = ChaturbateRunStore(tmp)
            report = store.adopt_legacy(dry_run=True)
            self.assertEqual(report["next_step"], StepName.VERIFY.value)
            self.assertFalse(Path(tmp, "runs").exists())
            first = store.adopt_legacy(dry_run=False)
            second = store.adopt_legacy(dry_run=False)
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(
                first["context"].generation_id,
                second["context"].generation_id,
            )
            for name, payload in originals.items():
                self.assertEqual(Path(tmp, name).read_bytes(), payload)


class MasterRepositoryTests(unittest.TestCase):
    def test_txt_mismatch_repairs_from_json_without_blacklist_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = MasterRepository(tmp)
            repository.publish(
                [
                    {
                        "name": "https://chaturbate.com/synthetic_a/",
                        "date": "2026-07-24",
                        "session": 1,
                    }
                ]
            )
            blacklist = Path(tmp, "GLOBAL_BLACKLIST.txt")
            blacklist.write_bytes(
                b"https://chaturbate.com/protected_blacklist/\n"
            )
            Path(repository.txt_path).write_text("", encoding="utf-8")
            result = repository.validate_consistency(repair_txt=True)
            self.assertTrue(result.valid)
            self.assertTrue(result.repaired_txt)
            self.assertEqual(
                blacklist.read_bytes(),
                b"https://chaturbate.com/protected_blacklist/\n",
            )
            self.assertEqual(
                Path(repository.txt_path).read_text(encoding="utf-8"),
                "https://chaturbate.com/synthetic_a/\n",
            )

    def test_corrupt_master_json_blocks_repair_and_preserves_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = MasterRepository(tmp)
            Path(repository.json_path).write_bytes(b"{broken")
            Path(repository.txt_path).write_bytes(
                b"https://chaturbate.com/synthetic_a/\n"
            )
            before = Path(repository.txt_path).read_bytes()
            result = repository.validate_consistency(repair_txt=True)
            self.assertFalse(result.valid)
            self.assertEqual(result.error_code, "master_json_invalid")
            self.assertEqual(Path(repository.txt_path).read_bytes(), before)

    def test_master_publication_keeps_list_schema_and_revision_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = MasterRepository(tmp)
            repository.publish(
                [
                    {
                        "name": "https://chaturbate.com/synthetic_a/",
                        "date": "2026-07-24",
                        "session": 1,
                        "last_verdict": "unknown",
                    }
                ]
            )
            payload = json.loads(
                Path(repository.json_path).read_text(encoding="utf-8")
            )
            metadata = json.loads(
                Path(repository.meta_path).read_text(encoding="utf-8")
            )
            self.assertIsInstance(payload, list)
            self.assertEqual(metadata["item_count"], 1)
            self.assertEqual(len(metadata["json_sha256"]), 64)
            self.assertEqual(len(metadata["txt_sha256"]), 64)

    def test_unknown_master_verdict_is_never_removed_or_blacklisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = MasterRepository(tmp)
            url = "https://chaturbate.com/synthetic_a/"
            repository.publish(
                [{"name": url, "date": "2026-07-24", "session": 1}]
            )
            blacklist = Path(repository.blacklist_path)
            blacklist.write_bytes(
                b"https://chaturbate.com/existing_blacklist/\n"
            )
            before = blacklist.read_bytes()
            result = repository.apply_verification_records(
                [
                    VerificationRecord(
                        original_url=url,
                        verdict=VerificationVerdict.UNKNOWN,
                        reason_code="navigation_timeout",
                        attempt=1,
                        timestamp="2026-07-24T12:00:00+00:00",
                        observed_url="",
                        run_id="run-1",
                    )
                ]
            )
            self.assertEqual(result["removed"], ())
            self.assertEqual(blacklist.read_bytes(), before)
            entry = repository.load(required=True)[0]
            self.assertTrue(entry["pending_verification"])
            self.assertEqual(entry["last_verdict"], "unknown")

    def test_accessible_removal_requires_two_distinct_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = MasterRepository(tmp)
            url = "https://chaturbate.com/synthetic_a/"
            repository.publish(
                [{"name": url, "date": "2026-07-24", "session": 1}]
            )

            def accessible(run_id):
                return VerificationRecord(
                    original_url=url,
                    verdict=VerificationVerdict.ACCESSIBLE,
                    reason_code="room_container",
                    attempt=1,
                    timestamp=f"2026-07-24T12:00:0{len(run_id)}+00:00",
                    observed_url=url,
                    run_id=run_id,
                )

            repository.apply_verification_records([accessible("run-1")])
            repository.apply_verification_records([accessible("run-1")])
            self.assertEqual(len(repository.load(required=True)), 1)
            result = repository.apply_verification_records(
                [accessible("run-2")]
            )
            self.assertEqual(result["removed"], (url,))
            self.assertEqual(repository.load(required=True), [])
            self.assertIn(
                url,
                repository.read_url_set(repository.blacklist_path),
            )

    def test_blocked_resets_accessible_confirmation_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository = MasterRepository(tmp)
            url = "https://chaturbate.com/synthetic_a/"
            repository.publish(
                [{"name": url, "date": "2026-07-24", "session": 1}]
            )
            verdicts = [
                (VerificationVerdict.ACCESSIBLE, "run-1"),
                (VerificationVerdict.BLOCKED, "run-2"),
                (VerificationVerdict.ACCESSIBLE, "run-3"),
            ]
            for verdict, run_id in verdicts:
                repository.apply_verification_records(
                    [
                        VerificationRecord(
                            original_url=url,
                            verdict=verdict,
                            reason_code=(
                                "room_container"
                                if verdict is VerificationVerdict.ACCESSIBLE
                                else "denied_notice"
                            ),
                            attempt=1,
                            timestamp="2026-07-24T12:00:00+00:00",
                            observed_url=url,
                            run_id=run_id,
                        )
                    ]
                )
            entry = repository.load(required=True)[0]
            self.assertEqual(entry["accessible_confirmations"], 1)
            self.assertEqual(entry["accessible_run_ids"], ["run-3"])


class SharedContractCharacterizationTests(unittest.TestCase):
    """Public-import fence guarding the safety-kernel extraction.

    The generic extraction must keep these names importable with these
    constructor shapes; a wrapper rename that breaks one of them is a
    regression, not a refactor.
    """

    def test_public_imports_and_constructors_are_stable(self):
        from ctb_store import RunContext
        from ctb_verifier import (
            ChaturbateVerifier,
            TRANSIENT_REASONS,
            candidate_digest as digest_function,
        )

        self.assertTrue(callable(digest_function))
        self.assertIn("navigation_timeout", TRANSIENT_REASONS)
        with tempfile.TemporaryDirectory() as tmp:
            repository = MasterRepository(tmp)
            self.assertTrue(
                repository.json_path.endswith("MASTER_BLOCKED_DATA.json")
            )
            self.assertTrue(
                repository.txt_path.endswith("MASTER_BLOCKED.txt")
            )
            store = ChaturbateRunStore(tmp)
            context = store.create_run(mode="manual", source="test")
            self.assertIsInstance(context, RunContext)
            self.assertEqual(context.platform, "Chaturbate")
            self.assertTrue(os.path.exists(context.manifest_path))
            self.assertIsNotNone(ChaturbateVerifier)


if __name__ == "__main__":
    unittest.main()
