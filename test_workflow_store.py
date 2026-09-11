"""Generic schema-v2 run store contracts."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from workflow_store import GenericRunStore, RunContext, RunStoreError
from workflow_types import OutcomeStatus, StepName, StepOutcome


CONFIG = {
    "digest_sha256": "d" * 64,
    "canonicalizer_version": "sc-url-1",
    "classifier_version": "sc-page-1",
    "api_contract_version": "sc-api-1",
    "relevant_values": {"page_size": 400},
}


def make_store(root, name="session 1 28.07.2026"):
    session = Path(root, name)
    session.mkdir(parents=True, exist_ok=True)
    return GenericRunStore(
        session, platform_key="stripchat", config=dict(CONFIG)
    )


def outcome_for(context, step, status=OutcomeStatus.SUCCEEDED, **extra):
    factory = {
        OutcomeStatus.SUCCEEDED: StepOutcome.succeeded,
        OutcomeStatus.CANCELLED: StepOutcome.cancelled,
        OutcomeStatus.FAILED: StepOutcome.failed,
        OutcomeStatus.INCOMPLETE: StepOutcome.incomplete,
    }[status]
    return factory(
        run_id=context.run_id,
        generation_id=context.generation_id,
        platform=context.platform_key,
        step=step,
        session_id=context.session_id,
        **extra,
    )


class RunLifecycleTests(unittest.TestCase):
    def test_create_run_writes_v2_manifest_and_state(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(mode="full_auto", source="test")
            manifest = store.load_manifest(context)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["manifest_revision"], 1)
            self.assertEqual(manifest["platform_key"], "stripchat")
            self.assertEqual(manifest["session_id"], context.session_id)
            self.assertEqual(manifest["status"], "running")
            self.assertEqual(manifest["publication"]["state"], "none")
            self.assertEqual(
                manifest["config"]["digest_sha256"], CONFIG["digest_sha256"]
            )
            state = store.load_run_state()
            self.assertEqual(state["active_run_id"], context.run_id)
            self.assertIsNone(state["committed_generation"])

    def test_new_run_never_reuses_a_directory(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            first = store.create_run(source="test")
            second = store.create_run(source="test")
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertNotEqual(first.generation_id, second.generation_id)
            self.assertTrue(os.path.isdir(first.run_dir))
            self.assertTrue(os.path.isdir(second.run_dir))

    def test_invalid_mode_and_source_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            with self.assertRaises(RunStoreError):
                store.create_run(mode="destructive")
            with self.assertRaises(RunStoreError):
                store.create_run(source="unknown")

    def test_manifest_identity_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            wrong = RunContext(
                session_path=context.session_path,
                session_id=context.session_id,
                run_id=context.run_id,
                generation_id="00000000-0000-4000-8000-000000000000",
                platform_key=context.platform_key,
                mode=context.mode,
            )
            with self.assertRaises(RunStoreError):
                store.load_manifest(wrong)

    def test_manifest_revision_increments_and_rejects_regression(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            store.update_manifest(context, lambda m: None)
            manifest = store.load_manifest(context)
            self.assertEqual(manifest["manifest_revision"], 2)
            raw = json.loads(
                Path(context.manifest_path).read_text(encoding="utf-8")
            )
            raw["manifest_revision"] = 1
            Path(context.manifest_path).write_text(
                json.dumps(raw), encoding="utf-8"
            )
            with self.assertRaises(RunStoreError):
                store.load_manifest(context)

    def test_future_schema_rejected_for_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            raw = json.loads(
                Path(context.manifest_path).read_text(encoding="utf-8")
            )
            raw["schema_version"] = 3
            Path(context.manifest_path).write_text(
                json.dumps(raw), encoding="utf-8"
            )
            with self.assertRaises(ValueError):
                store.load_manifest(context)


class ArtifactTests(unittest.TestCase):
    def test_artifact_round_trip_with_identity_and_upstream(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            record = store.artifact_lines(
                context,
                StepName.VPN_SNAPSHOT,
                "vpn_urls.txt",
                ["https://stripchat.com/Synthetic-Model@1/"],
                logical_name="step1.vpn_urls",
                schema="canonical_url_set/v1",
            )
            outcome = outcome_for(
                context, StepName.VPN_SNAPSHOT, artifacts=(record,)
            )
            store.record_step_attempt(context, outcome)
            document, path = store.load_artifact(context, "step1.vpn_urls")
            self.assertEqual(document["record_count"], 1)
            self.assertEqual(
                document["identity"]["generation_id"], context.generation_id
            )
            self.assertTrue(os.path.exists(path))
            lines = store.read_artifact_lines(context, "step1.vpn_urls")
            self.assertEqual(
                lines, ["https://stripchat.com/Synthetic-Model@1/"]
            )

    def test_zero_record_artifact_is_valid_with_manifest_record(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            record = store.artifact_lines(
                context,
                StepName.COMPARE,
                "candidates.txt",
                [],
                logical_name="step3.candidates",
                schema="canonical_url_set/v1",
            )
            self.assertEqual(record.record_count, 0)
            store.record_step_attempt(
                context,
                outcome_for(context, StepName.COMPARE, artifacts=(record,)),
            )
            self.assertEqual(
                store.read_artifact_lines(context, "step3.candidates"), []
            )

    def test_artifact_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            record = store.artifact_lines(
                context,
                StepName.VPN_SNAPSHOT,
                "vpn_urls.txt",
                ["https://stripchat.com/A/"],
                logical_name="step1.vpn_urls",
                schema="canonical_url_set/v1",
            )
            store.record_step_attempt(
                context,
                outcome_for(
                    context, StepName.VPN_SNAPSHOT, artifacts=(record,)
                ),
            )
            full = Path(context.session_path, record.path)
            full.write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(RunStoreError):
                store.load_artifact(context, "step1.vpn_urls")

    def test_artifact_cannot_be_rewritten_in_same_generation(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            record = store.artifact_lines(
                context,
                StepName.VPN_SNAPSHOT,
                "vpn_urls.txt",
                ["https://stripchat.com/A/"],
                logical_name="step1.vpn_urls",
                schema="canonical_url_set/v1",
            )
            store.record_step_attempt(
                context,
                outcome_for(
                    context, StepName.VPN_SNAPSHOT, artifacts=(record,)
                ),
            )
            with self.assertRaises(RunStoreError):
                store.artifact_lines(
                    context,
                    StepName.VPN_SNAPSHOT,
                    "vpn_urls.txt",
                    ["https://stripchat.com/B/"],
                    logical_name="step1.vpn_urls",
                    schema="canonical_url_set/v1",
                )


class StepAttemptTests(unittest.TestCase):
    def test_attempts_are_append_only_and_current_advances(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            first = outcome_for(
                context,
                StepName.VERIFY,
                OutcomeStatus.INCOMPLETE,
                error_code="transient_unknowns_remaining",
                resumable=True,
            )
            store.record_step_attempt(context, first, retry_epoch=0)
            second = outcome_for(context, StepName.VERIFY)
            store.record_step_attempt(context, second, retry_epoch=1)
            manifest = store.load_manifest(context)
            step_entry = manifest["steps"][StepName.VERIFY.value]
            self.assertEqual(len(step_entry["attempts"]), 2)
            self.assertEqual(
                step_entry["attempts"][0]["outcome"]["status"], "incomplete"
            )
            self.assertEqual(step_entry["current"]["status"], "succeeded")
            self.assertEqual(manifest["status"], "succeeded")

    def test_waiting_outcome_is_rejected_as_terminal_record(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            waiting = StepOutcome._create(
                OutcomeStatus.WAITING_FOR_USER,
                run_id=context.run_id,
                generation_id=context.generation_id,
                platform=context.platform_key,
                step=StepName.VERIFY,
                session_id=context.session_id,
            )
            with self.assertRaises(RunStoreError):
                store.record_step_attempt(context, waiting)

    def test_identity_mismatch_outcomes_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            cases = {
                "platform": {"platform": "Stripchat"},
                "session": {"session_id": "other session"},
                "run": {"run_id": "00000000-0000-4000-8000-000000000000"},
            }
            for label, overrides in cases.items():
                values = {
                    "run_id": context.run_id,
                    "generation_id": context.generation_id,
                    "platform": context.platform_key,
                    "step": StepName.VPN_SNAPSHOT,
                    "session_id": context.session_id,
                }
                values.update(overrides)
                with self.subTest(case=label), self.assertRaises(
                    RunStoreError
                ):
                    store.record_step_attempt(
                        context, StepOutcome.succeeded(**values)
                    )

    def test_require_step_success_gates_prerequisites(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            with self.assertRaises(RunStoreError):
                store.require_step_success(context, StepName.VPN_SNAPSHOT)
            store.record_step_attempt(
                context, outcome_for(context, StepName.VPN_SNAPSHOT)
            )
            current = store.require_step_success(
                context, StepName.VPN_SNAPSHOT
            )
            self.assertEqual(current["status"], "succeeded")


class PublicationAndWaitingTests(unittest.TestCase):
    def test_publication_state_is_monotonic(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            store.set_publication_state(context, "preparing")
            store.set_publication_state(context, "prepared")
            store.set_publication_state(context, "committed")
            with self.assertRaises(RunStoreError):
                store.set_publication_state(context, "preparing")

    def test_waiting_set_and_clear_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            store.set_waiting(context, "captcha_required", None)
            manifest = store.load_manifest(context)
            self.assertTrue(manifest["waiting"]["active"])
            self.assertEqual(manifest["status"], "waiting_for_user")
            self.assertEqual(
                store.load_run_state()["status"], "waiting_for_user"
            )
            store.clear_waiting(context)
            manifest = store.load_manifest(context)
            self.assertFalse(manifest["waiting"]["active"])
            self.assertEqual(manifest["status"], "running")


class CheckpointTests(unittest.TestCase):
    def _checkpoint_payload(self):
        return {
            "candidate_artifact_id": "step3.candidates",
            "candidate_sha256": "c" * 64,
            "candidate_count": 2,
            "retry_epoch": 0,
            "max_retry_epochs": 3,
            "attempt_budget_per_epoch": 2,
            "records": {},
            "summary": {
                "blocked": 0,
                "accessible": 0,
                "unknown_transient": 0,
                "unknown_terminal": 0,
                "unattempted": 2,
            },
        }

    def test_checkpoint_round_trip_binds_identity_and_config(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            store.write_checkpoint(context, self._checkpoint_payload())
            value = store.load_checkpoint(context)
            self.assertEqual(
                value["identity"]["generation_id"], context.generation_id
            )
            self.assertEqual(
                value["config_digest_sha256"], CONFIG["digest_sha256"]
            )

    def test_checkpoint_config_digest_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            context = store.create_run(source="test")
            store.write_checkpoint(context, self._checkpoint_payload())
            other = GenericRunStore(
                context.session_path,
                platform_key="stripchat",
                config={**CONFIG, "digest_sha256": "e" * 64},
            )
            with self.assertRaises(RunStoreError):
                other.load_checkpoint(context)


class RunStatePointerTests(unittest.TestCase):
    def test_committed_pointer_requires_all_fields(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            store.create_run(source="test")
            with self.assertRaises(RunStoreError):
                store.set_committed_generation({"transaction_id": "t"})

    def test_committed_pointer_survives_new_active_run(self):
        with tempfile.TemporaryDirectory() as root:
            store = make_store(root)
            first = store.create_run(source="test")
            pointer = {
                "transaction_id": "00000000-0000-4000-8000-000000000001",
                "run_id": first.run_id,
                "generation_id": first.generation_id,
                "commit_relative_path": "generations/x/commit.json",
                "final_artifact_relative_path": "runs/x/final.txt",
                "final_sha256": "f" * 64,
                "final_record_count": 0,
                "committed_at": "2026-07-28T00:00:00+00:00",
            }
            store.set_committed_generation(pointer)
            second = store.create_run(source="test")
            state = store.load_run_state()
            self.assertEqual(state["active_run_id"], second.run_id)
            self.assertEqual(
                state["committed_generation"]["run_id"], first.run_id
            )


if __name__ == "__main__":
    unittest.main()
