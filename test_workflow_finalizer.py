"""Two-phase publication: fault injection at every boundary + recovery."""

import os
import tempfile
import unittest
from pathlib import Path

from storage_utils import AtomicWriter, sha256_file
from stripchat_core import HardenedStripchatRunner
from stripchat_store import (
    STRIPCHAT_PROJECTIONS,
    StripchatRunStore,
    merge_blocked_into_master,
    stripchat_master_repository,
)
from test_stripchat_core import (
    FakeApiClient,
    ScriptedBrowserProvider,
    snapshot_result,
)
from workflow_finalizer import WorkflowFinalizer
from workflow_types import OutcomeStatus


class InjectedFault(RuntimeError):
    pass


class FaultingWriter(AtomicWriter):
    """Raises once when writing a path whose name matches the target.

    ``arm_predicate`` delays arming until a condition holds, so a
    filename written both before and after the commit decision (the
    checkpoint, the session run state) can be faulted on the post-commit
    write specifically.
    """

    def __init__(self, target_name=None, stage="before_replace", arm_predicate=None):
        super().__init__(fault_injector=self._inject_fault)
        self.target_name = target_name
        self.stage = stage
        self.arm_predicate = arm_predicate
        self.armed = target_name is not None

    def _inject_fault(self, stage, path):
        if not self.armed:
            return
        if self.arm_predicate is not None and not self.arm_predicate():
            return
        if stage == self.stage and Path(path).name == self.target_name:
            self.armed = False
            raise InjectedFault(f"{stage}:{Path(path).name}")


class PublicationHarness:
    def __init__(self, root, writer=None):
        self.root = Path(root)
        self.master_dir = self.root
        session = self.root / "session 1 28.07.2026"
        session.mkdir(parents=True, exist_ok=True)
        self.session = session
        self.writer = writer or AtomicWriter()
        self.store = StripchatRunStore(session, writer=self.writer)
        self.context = self.store.create_run(mode="full_auto", source="test")
        self.policy = {"vpn_state": "disconnected", "relay_code": None}
        self.routes = {}

    def runner(self):
        return HardenedStripchatRunner(
            self.store,
            api_client_factory=lambda route: self.routes[route],
            policy_observer=lambda: dict(self.policy),
        )

    def run_through_step3(self, vpn=("Blocked-One", "Open-One"), local=()):
        self.policy = {
            "vpn_state": "connected",
            "relay_code": "ie-dub-wg-103",
        }
        self.routes["vpn"] = FakeApiClient(snapshot_result(vpn))
        assert (
            self.runner().run_step1(self.context).status
            is OutcomeStatus.SUCCEEDED
        )
        self.policy = {"vpn_state": "disconnected", "relay_code": None}
        self.routes["local"] = FakeApiClient(snapshot_result(local))
        assert (
            self.runner().run_step2(self.context).status
            is OutcomeStatus.SUCCEEDED
        )
        assert (
            self.runner().run_step3(self.context).status
            is OutcomeStatus.SUCCEEDED
        )

    def provider(self):
        return ScriptedBrowserProvider(
            {
                "https://stripchat.com/Blocked-One/": {
                    "ready_state": "complete",
                    "account_notice_texts": ("Account Hidden",),
                },
                "https://stripchat.com/Open-One/": {
                    "ready_state": "complete",
                    "video_element_count": 1,
                },
            }
        )

    def finalizer(self, shadow=False, writer=None):
        return WorkflowFinalizer(
            self.store,
            self.context,
            stripchat_master_repository(
                self.master_dir, writer=writer or self.writer
            ),
            master_entry_builder=merge_blocked_into_master,
            projections=STRIPCHAT_PROJECTIONS,
            shadow=shadow,
        )

    def run_step4(self, shadow=False, writer=None):
        finalizer = self.finalizer(shadow=shadow, writer=writer)
        return self.runner().run_step4(
            self.context,
            provider=self.provider(),
            finalizer=finalizer.finalize,
        )

    def live_hashes(self):
        values = {}
        for name in (
            "MASTER_BLOCKED_DATA.json",
            "MASTER_BLOCKED.txt",
        ):
            path = self.master_dir / name
            values[name] = sha256_file(path) if path.exists() else None
        final = self.session / "FINAL_BLOCKED.txt"
        values["FINAL_BLOCKED.txt"] = (
            sha256_file(final) if final.exists() else None
        )
        return values


def seed_live_master(harness):
    repository = stripchat_master_repository(harness.master_dir)
    repository.publish(
        [{"name": "https://stripchat.com/Existing-Model/", "date": "old"}]
    )
    (harness.session / "FINAL_BLOCKED.txt").write_text(
        "https://stripchat.com/Previous-Final/\n", encoding="utf-8"
    )


class HappyPathTests(unittest.TestCase):
    def test_full_publication_projects_final_master_and_views(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            harness.run_through_step3()
            outcome = harness.run_step4()
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            final = (harness.session / "FINAL_BLOCKED.txt").read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                final, "https://stripchat.com/Blocked-One/\n"
            )
            repository = stripchat_master_repository(harness.master_dir)
            names = {entry["name"] for entry in repository.load()}
            self.assertIn("https://stripchat.com/Blocked-One/", names)
            self.assertIn("https://stripchat.com/Existing-Model/", names)
            self.assertNotIn("https://stripchat.com/Open-One/", names)
            for view in (
                "sc_vpn_list.txt",
                "sc_local_list.txt",
                "sc_candidates.txt",
                "sc_debug_log.txt",
                "sc_metadata.json",
            ):
                self.assertTrue(
                    (harness.session / view).exists(), view
                )
            manifest = harness.store.load_manifest(harness.context)
            self.assertEqual(
                manifest["publication"]["state"], "projected"
            )
            state = harness.store.load_run_state()
            self.assertEqual(
                state["committed_generation"]["generation_id"],
                harness.context.generation_id,
            )
            checkpoint = harness.store.load_checkpoint(harness.context)
            self.assertTrue(checkpoint["sealed"])

    def test_zero_blocked_publishes_a_valid_empty_final(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            harness.run_through_step3(vpn=("Open-One",), local=())
            outcome = harness.run_step4()
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(outcome.output_count, 0)
            final = (harness.session / "FINAL_BLOCKED.txt").read_text(
                encoding="utf-8"
            )
            self.assertEqual(final, "")
            state = harness.store.load_run_state()
            self.assertEqual(
                state["committed_generation"]["final_record_count"], 0
            )

    def test_shadow_mode_changes_no_live_file(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            before = harness.live_hashes()
            harness.run_through_step3()
            outcome = harness.run_step4(shadow=True)
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertTrue(
                any("shadow" in warning for warning in outcome.warnings)
            )
            self.assertEqual(harness.live_hashes(), before)
            manifest = harness.store.load_manifest(harness.context)
            self.assertEqual(manifest["publication"]["state"], "prepared")
            self.assertFalse(os.path.exists(harness.context.commit_path))


class PreCommitFaultTests(unittest.TestCase):
    TARGETS = (
        "verification_results.json",
        "final_blocked.staged.txt",
        "master_data.staged.json",
        "master_text.staged.txt",
        "master_meta.staged.json",
        "journal.json",
        "commit.json",
    )

    def test_faults_before_the_witness_leave_live_state_untouched(self):
        for target in self.TARGETS:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as root:
                writer = FaultingWriter(target)
                harness = PublicationHarness(root, writer=writer)
                seed_live_master(harness)
                before = harness.live_hashes()
                harness.run_through_step3()
                outcome = harness.run_step4(writer=writer)
                self.assertIs(outcome.status, OutcomeStatus.FAILED, target)
                self.assertIn(
                    outcome.error_code, ("prepare_failed", "commit_failed")
                )
                self.assertEqual(harness.live_hashes(), before, target)
                self.assertFalse(
                    os.path.exists(harness.context.commit_path), target
                )
                for logical in (
                    "step1.vpn_urls",
                    "step2.local_urls",
                    "step3.candidates",
                ):
                    harness.store.load_artifact(harness.context, logical)


class PostCommitFaultTests(unittest.TestCase):
    TARGETS = (
        "run_state.json",
        "MASTER_BLOCKED_DATA.json",
        "MASTER_BLOCKED.txt",
        "MASTER_BLOCKED_META.json",
        "FINAL_BLOCKED.txt",
        "verification_checkpoint.json",
    )

    def _fault_then_recover(self, target):
        with tempfile.TemporaryDirectory() as root:
            witness = {"path": None}
            writer = FaultingWriter(
                target,
                arm_predicate=lambda: bool(
                    witness["path"] and os.path.exists(witness["path"])
                ),
            )
            harness = PublicationHarness(root, writer=writer)
            witness["path"] = harness.context.commit_path
            seed_live_master(harness)
            harness.run_through_step3()
            outcome = harness.run_step4(writer=writer)
            self.assertIs(outcome.status, OutcomeStatus.FAILED, target)
            self.assertEqual(outcome.error_code, "projection_failed", target)
            self.assertTrue(outcome.resumable)
            self.assertTrue(
                os.path.exists(harness.context.commit_path), target
            )

            # A fresh process recovers by rolling forward, never touching
            # a browser.
            recovery_store = StripchatRunStore(harness.session)
            finalizer = WorkflowFinalizer(
                recovery_store,
                harness.context,
                stripchat_master_repository(harness.master_dir),
                master_entry_builder=merge_blocked_into_master,
                projections=STRIPCHAT_PROJECTIONS,
            )
            recovered = finalizer.recover()
            self.assertIsNotNone(recovered, target)
            self.assertIs(
                recovered.status, OutcomeStatus.SUCCEEDED, target
            )
            repository = stripchat_master_repository(harness.master_dir)
            names = {entry["name"] for entry in repository.load()}
            self.assertIn("https://stripchat.com/Blocked-One/", names)
            final = (harness.session / "FINAL_BLOCKED.txt").read_text(
                encoding="utf-8"
            )
            self.assertEqual(final, "https://stripchat.com/Blocked-One/\n")
            manifest = recovery_store.load_manifest(harness.context)
            self.assertEqual(manifest["publication"]["state"], "projected")
            self.assertFalse(
                manifest["publication"]["recovery_required"], target
            )
            # Recovery is idempotent: a second pass changes nothing.
            self.assertIsNone(finalizer.recover(), target)

    def test_every_post_commit_fault_rolls_forward(self):
        for target in self.TARGETS:
            with self.subTest(target=target):
                self._fault_then_recover(target)


class ResumeAfterPrepareTests(unittest.TestCase):
    def test_prepared_journal_is_reused_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            harness.run_through_step3()
            shadow_outcome = harness.run_step4(shadow=True)
            self.assertIs(shadow_outcome.status, OutcomeStatus.SUCCEEDED)
            manifest = harness.store.load_manifest(harness.context)
            first_txn = manifest["publication"]["transaction_id"]
            finalizer = harness.finalizer(shadow=False)
            outcome = finalizer.finalize({})
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            manifest = harness.store.load_manifest(harness.context)
            self.assertEqual(
                manifest["publication"]["transaction_id"], first_txn
            )
            self.assertEqual(manifest["publication"]["state"], "projected")


if __name__ == "__main__":
    unittest.main()


class RecoveryOfProjectedGenerationTests(unittest.TestCase):
    """A committed generation must always be repairable.

    _roll_forward used to write the "projecting" state unconditionally.
    Recovery of an already-"projected" generation is a backward move, which
    the store rejects, so every repair dead-ended in projection_failed.
    """

    def test_missing_root_projection_is_repaired(self):
        for view in sorted(STRIPCHAT_PROJECTIONS):
            with self.subTest(view=view), tempfile.TemporaryDirectory() as root:
                harness = PublicationHarness(root)
                seed_live_master(harness)
                harness.run_through_step3()
                self.assertIs(
                    harness.run_step4().status, OutcomeStatus.SUCCEEDED
                )
                target = harness.session / view
                target.unlink()

                finalizer = WorkflowFinalizer(
                    StripchatRunStore(harness.session),
                    harness.context,
                    stripchat_master_repository(harness.master_dir),
                    master_entry_builder=merge_blocked_into_master,
                    projections=STRIPCHAT_PROJECTIONS,
                )
                outcome = finalizer.recover()
                self.assertIsNotNone(outcome, view)
                self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED, view)
                self.assertTrue(target.exists(), f"{view} was not repaired")

    def test_repeated_recovery_of_a_healthy_generation_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            harness.run_through_step3()
            harness.run_step4()
            finalizer = WorkflowFinalizer(
                StripchatRunStore(harness.session),
                harness.context,
                stripchat_master_repository(harness.master_dir),
                master_entry_builder=merge_blocked_into_master,
                projections=STRIPCHAT_PROJECTIONS,
            )
            self.assertIsNone(finalizer.recover())
            self.assertIsNone(finalizer.recover())


class StaleRevisionIsRejectedBeforeCommitTests(unittest.TestCase):
    """A revision staged against an older master must not be committed.

    The commit witness is the point of no return: once written, recovery can
    only replay the staged revision. Committing a revision whose base master
    has moved wedged that generation permanently.
    """

    def test_master_moving_after_prepare_fails_without_a_witness(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            harness.run_through_step3()
            # Stage the publication without committing (shadow behavior).
            self.assertIs(
                harness.run_step4(shadow=True).status,
                OutcomeStatus.SUCCEEDED,
            )
            before = harness.live_hashes()

            # Another session publishes to the shared master in between.
            stripchat_master_repository(harness.master_dir).publish(
                [
                    {"name": "https://stripchat.com/Existing-Model/"},
                    {"name": "https://stripchat.com/Other-Session-Model/"},
                ]
            )
            moved = harness.live_hashes()
            self.assertNotEqual(moved, before)

            outcome = harness.finalizer(shadow=False).finalize({})
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "master_revision_conflict")
            self.assertTrue(outcome.new_generation_required)
            self.assertFalse(
                os.path.exists(harness.context.commit_path),
                "a stale revision must not leave a commit witness",
            )
            self.assertEqual(
                harness.live_hashes(),
                moved,
                "the other session's master must be untouched",
            )

    def test_a_fresh_run_publishes_after_the_conflict(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            harness.run_through_step3()
            harness.run_step4(shadow=True)
            stripchat_master_repository(harness.master_dir).publish(
                [
                    {"name": "https://stripchat.com/Existing-Model/"},
                    {"name": "https://stripchat.com/Other-Session-Model/"},
                ]
            )
            harness.finalizer(shadow=False).finalize({})

            fresh = PublicationHarness.__new__(PublicationHarness)
            fresh.root = harness.root
            fresh.master_dir = harness.master_dir
            fresh.session = harness.session
            fresh.writer = harness.writer
            fresh.store = StripchatRunStore(harness.session)
            fresh.context = fresh.store.create_run(
                mode="full_auto", source="test"
            )
            fresh.policy = {"vpn_state": "disconnected", "relay_code": None}
            fresh.routes = {}
            fresh.run_through_step3()
            outcome = fresh.run_step4()
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            names = {
                entry["name"]
                for entry in stripchat_master_repository(
                    harness.master_dir
                ).load()
            }
            self.assertIn("https://stripchat.com/Blocked-One/", names)
            self.assertIn("https://stripchat.com/Other-Session-Model/", names)


class RecoveryNeverEscapesUntypedTests(unittest.TestCase):
    def test_unreadable_staging_before_commit_falls_back_to_re_prepare(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            harness.run_through_step3()
            harness.run_step4(shadow=True)
            Path(harness.context.publication_dir, "journal.json").unlink()

            finalizer = harness.finalizer(shadow=False)
            self.assertIsNone(
                finalizer.recover(),
                "a pre-commit staging problem is recoverable, not fatal",
            )
            outcome = finalizer.finalize({})
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)

    def test_unreadable_staging_after_commit_is_a_typed_failure(self):
        with tempfile.TemporaryDirectory() as root:
            harness = PublicationHarness(root)
            seed_live_master(harness)
            harness.run_through_step3()
            harness.run_step4()
            Path(harness.context.publication_dir, "journal.json").unlink()
            (harness.session / "FINAL_BLOCKED.txt").unlink()

            finalizer = WorkflowFinalizer(
                StripchatRunStore(harness.session),
                harness.context,
                stripchat_master_repository(harness.master_dir),
                master_entry_builder=merge_blocked_into_master,
                projections=STRIPCHAT_PROJECTIONS,
            )
            outcome = finalizer.recover()
            self.assertIsNotNone(outcome)
            self.assertIs(outcome.status, OutcomeStatus.FAILED)
            self.assertEqual(outcome.error_code, "projection_pending")
            self.assertTrue(outcome.resumable)
