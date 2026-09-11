"""Shadow-mode rollout gates and deterministic replay equivalence.

These are the automated parts of Phase 12: a full workflow through fake
transports proving that shadow mode changes no live file, that Stop and
Resume reach the same published bytes as an uninterrupted run, and that
the independent integrity validator accepts a committed generation.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from storage_utils import sha256_file
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
from workflow_types import OutcomeStatus, StepName


BLOCKED = ("Blocked-One", "Blocked-Two")
OPEN = ("Open-One",)
VPN_SET = BLOCKED + OPEN
LOCAL_SET = ()


def page_script():
    script = {}
    for name in BLOCKED:
        script[f"https://stripchat.com/{name}/"] = {
            "ready_state": "complete",
            "account_notice_texts": ("Account Hidden",),
            "visible_disabled_selector_count": 2,
        }
    for name in OPEN:
        script[f"https://stripchat.com/{name}/"] = {
            "ready_state": "complete",
            "video_element_count": 1,
        }
    return script


class Workspace:
    def __init__(self, root, shadow=False):
        self.root = Path(root)
        self.session = self.root / "session 1 28.07.2026"
        self.session.mkdir(parents=True, exist_ok=True)
        self.store = StripchatRunStore(self.session)
        self.context = self.store.create_run(mode="full_auto", source="test")
        self.shadow = shadow
        self.policy = {"vpn_state": "disconnected", "relay_code": None}
        self.routes = {}

    def runner(self, stop_token=None):
        return HardenedStripchatRunner(
            self.store,
            api_client_factory=lambda route: self.routes[route],
            policy_observer=lambda: dict(self.policy),
            stop_token=stop_token,
        )

    def finalizer(self):
        return WorkflowFinalizer(
            self.store,
            self.context,
            stripchat_master_repository(self.root),
            master_entry_builder=merge_blocked_into_master,
            projections=STRIPCHAT_PROJECTIONS,
            shadow=self.shadow,
        )

    def steps_1_to_3(self):
        self.policy = {"vpn_state": "connected", "relay_code": "ie-dub-wg-1"}
        self.routes["vpn"] = FakeApiClient(snapshot_result(VPN_SET))
        assert self.runner().run_step1(self.context).status is (
            OutcomeStatus.SUCCEEDED
        )
        self.policy = {"vpn_state": "disconnected", "relay_code": None}
        self.routes["local"] = FakeApiClient(snapshot_result(LOCAL_SET))
        assert self.runner().run_step2(self.context).status is (
            OutcomeStatus.SUCCEEDED
        )
        assert self.runner().run_step3(self.context).status is (
            OutcomeStatus.SUCCEEDED
        )

    def step4(self, stop_token=None, script=None):
        return self.runner(stop_token).run_step4(
            self.context,
            provider=ScriptedBrowserProvider(script or page_script()),
            finalizer=self.finalizer().finalize,
        )

    def live_state(self):
        values = {}
        for name in (
            "MASTER_BLOCKED_DATA.json",
            "MASTER_BLOCKED.txt",
            "MASTER_BLOCKED_META.json",
        ):
            path = self.root / name
            values[name] = sha256_file(path) if path.is_file() else None
        for name in STRIPCHAT_PROJECTIONS:
            path = self.session / name
            values[name] = sha256_file(path) if path.is_file() else None
        return values

    def seed(self):
        stripchat_master_repository(self.root).publish(
            [{"name": "https://stripchat.com/Existing-Model/", "date": "old"}]
        )
        (self.session / "FINAL_BLOCKED.txt").write_text(
            "https://stripchat.com/Previous-Final/\n", encoding="utf-8"
        )


def validate_run_integrity(store, context, session, root):
    """Independent reload-from-disk validator (new run integrity gate)."""
    problems = []
    manifest = store.load_manifest(context)
    checkpoint = store.load_checkpoint(context)
    identity = context.identity.to_dict()
    if manifest["platform_key"] != identity["platform_key"]:
        problems.append("manifest platform mismatch")
    if checkpoint and checkpoint["identity"] != identity:
        problems.append("checkpoint identity mismatch")
    for logical, document in manifest["artifacts"].items():
        try:
            store.load_artifact(context, logical)
        except Exception as exc:
            problems.append(f"{logical}: {exc}")
        if document["identity"] != identity:
            problems.append(f"{logical}: identity mismatch")
    for step in (
        StepName.VPN_SNAPSHOT,
        StepName.LOCAL_SNAPSHOT,
        StepName.COMPARE,
    ):
        current = manifest["steps"].get(step.value, {}).get("current")
        if not current or current["status"] != "succeeded":
            problems.append(f"{step.value} has no successful current outcome")
    if checkpoint:
        summary = checkpoint["summary"]
        total = sum(
            summary[key]
            for key in (
                "blocked",
                "accessible",
                "unknown_terminal",
                "unknown_transient",
                "unattempted",
            )
        )
        if total != checkpoint["candidate_count"]:
            problems.append("verdict counts do not add up to candidates")
    state = store.load_run_state()
    pointer = (state or {}).get("committed_generation")
    if pointer:
        final = Path(session, pointer["final_artifact_relative_path"])
        if sha256_file(final) != pointer["final_sha256"]:
            problems.append("committed final hash mismatch")
        blocked = [
            line.strip()
            for line in final.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        records = (checkpoint or {}).get("records", {})
        expected = sorted(
            record["canonical_url"]
            for record in records.values()
            if record.get("classification") == "blocked"
        )
        if blocked != expected:
            problems.append("final does not equal the blocked verdicts")
        repository = stripchat_master_repository(root)
        consistency = repository.validate_consistency(repair_txt=False)
        if not consistency.valid:
            problems.append("master JSON/TXT disagree")
        names = {entry["name"] for entry in repository.load()}
        if not set(blocked).issubset(names):
            problems.append("committed final is not covered by the master")
    return problems


class ShadowGateTests(unittest.TestCase):
    def test_full_shadow_flow_changes_no_live_file(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Workspace(root, shadow=True)
            workspace.seed()
            before = workspace.live_state()
            workspace.steps_1_to_3()
            outcome = workspace.step4()
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(workspace.live_state(), before)
            manifest = workspace.store.load_manifest(workspace.context)
            self.assertEqual(manifest["publication"]["state"], "prepared")
            self.assertFalse(
                os.path.exists(workspace.context.commit_path),
                "shadow mode must never create a commit witness",
            )

    def test_shadow_staging_matches_what_hardened_would_publish(self):
        with tempfile.TemporaryDirectory() as shadow_root, (
            tempfile.TemporaryDirectory()
        ) as live_root:
            shadow = Workspace(shadow_root, shadow=True)
            shadow.seed()
            shadow.steps_1_to_3()
            shadow.step4()
            live = Workspace(live_root, shadow=False)
            live.seed()
            live.steps_1_to_3()
            live.step4()
            staged = Path(
                shadow.context.publication_dir, "master_text.staged.txt"
            ).read_text(encoding="utf-8")
            published = (live.root / "MASTER_BLOCKED.txt").read_text(
                encoding="utf-8"
            )
            self.assertEqual(staged, published)


class DeterministicReplayTests(unittest.TestCase):
    def _published(self, workspace):
        return {
            "final": (workspace.session / "FINAL_BLOCKED.txt").read_text(
                encoding="utf-8"
            ),
            "master_json": (
                workspace.root / "MASTER_BLOCKED_DATA.json"
            ).read_text(encoding="utf-8"),
            "master_txt": (workspace.root / "MASTER_BLOCKED.txt").read_text(
                encoding="utf-8"
            ),
        }

    def test_stop_then_resume_reaches_the_same_published_bytes(self):
        class StopAfterFirst:
            def __init__(self):
                self.calls = 0

            def is_cancelled(self):
                self.calls += 1
                return self.calls > 2

        with tempfile.TemporaryDirectory() as clean_root, (
            tempfile.TemporaryDirectory()
        ) as interrupted_root:
            clean = Workspace(clean_root)
            clean.seed()
            clean.steps_1_to_3()
            self.assertIs(clean.step4().status, OutcomeStatus.SUCCEEDED)

            interrupted = Workspace(interrupted_root)
            interrupted.seed()
            interrupted.steps_1_to_3()
            stopped = interrupted.step4(stop_token=StopAfterFirst())
            self.assertIs(stopped.status, OutcomeStatus.CANCELLED)
            self.assertEqual(
                (interrupted.session / "FINAL_BLOCKED.txt").read_text(
                    encoding="utf-8"
                ),
                "https://stripchat.com/Previous-Final/\n",
                "a cancelled run must not touch the published final",
            )
            resumed = interrupted.step4()
            self.assertIs(resumed.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(
                self._published(clean)["final"],
                self._published(interrupted)["final"],
            )
            self.assertEqual(
                self._published(clean)["master_txt"],
                self._published(interrupted)["master_txt"],
            )

    def test_process_restart_resumes_from_disk_only(self):
        with tempfile.TemporaryDirectory() as root:
            first = Workspace(root)
            first.seed()
            first.steps_1_to_3()

            class StopImmediately:
                def is_cancelled(self):
                    return True

            self.assertIs(
                first.step4(stop_token=StopImmediately()).status,
                OutcomeStatus.CANCELLED,
            )
            # A completely fresh store object, as a new process would build.
            second = Workspace.__new__(Workspace)
            second.root = first.root
            second.session = first.session
            second.store = StripchatRunStore(first.session)
            second.context = second.store.load_active()
            second.shadow = False
            second.policy = {"vpn_state": "disconnected", "relay_code": None}
            second.routes = {}
            outcome = second.step4()
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(
                (first.session / "FINAL_BLOCKED.txt").read_text(
                    encoding="utf-8"
                ),
                "https://stripchat.com/Blocked-One/\n"
                "https://stripchat.com/Blocked-Two/\n",
            )


class NewRunIntegrityGateTests(unittest.TestCase):
    def test_committed_run_passes_the_independent_validator(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Workspace(root)
            workspace.seed()
            workspace.steps_1_to_3()
            self.assertIs(workspace.step4().status, OutcomeStatus.SUCCEEDED)
            fresh_store = StripchatRunStore(workspace.session)
            problems = validate_run_integrity(
                fresh_store,
                workspace.context,
                workspace.session,
                workspace.root,
            )
            self.assertEqual(problems, [])

    def test_zero_blocked_run_passes_the_validator(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Workspace(root)
            workspace.seed()
            workspace.policy = {
                "vpn_state": "connected",
                "relay_code": "ie-dub-wg-1",
            }
            workspace.routes["vpn"] = FakeApiClient(snapshot_result(OPEN))
            workspace.runner().run_step1(workspace.context)
            workspace.policy = {
                "vpn_state": "disconnected",
                "relay_code": None,
            }
            workspace.routes["local"] = FakeApiClient(snapshot_result(()))
            workspace.runner().run_step2(workspace.context)
            workspace.runner().run_step3(workspace.context)
            outcome = workspace.step4()
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertEqual(outcome.output_count, 0)
            problems = validate_run_integrity(
                StripchatRunStore(workspace.session),
                workspace.context,
                workspace.session,
                workspace.root,
            )
            self.assertEqual(problems, [])
            self.assertEqual(
                (workspace.session / "FINAL_BLOCKED.txt").read_text(
                    encoding="utf-8"
                ),
                "",
                "a valid empty final replaces the old non-empty one",
            )


class NonDestructiveGateTests(unittest.TestCase):
    def test_transient_unknowns_never_publish(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Workspace(root)
            workspace.seed()
            before = workspace.live_state()
            workspace.steps_1_to_3()
            script = page_script()
            script["https://stripchat.com/Open-One/"] = {
                "navigation_error": "navigation_timeout",
                "current_url": "",
            }
            outcome = workspace.step4(script=script)
            self.assertIs(outcome.status, OutcomeStatus.INCOMPLETE)
            self.assertEqual(workspace.live_state(), before)

    def test_terminal_nontarget_unknown_never_mutates_the_master(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = Workspace(root)
            workspace.seed()
            workspace.steps_1_to_3()
            script = page_script()
            script["https://stripchat.com/Open-One/"] = {
                "ready_state": "complete",
                "account_notice_texts": ("Account Disabled",),
            }
            outcome = workspace.step4(script=script)
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertTrue(outcome.warnings)
            names = {
                entry["name"]
                for entry in stripchat_master_repository(
                    workspace.root
                ).load()
            }
            self.assertIn("https://stripchat.com/Existing-Model/", names)
            self.assertNotIn("https://stripchat.com/Open-One/", names)

    def test_an_offline_room_finishes_the_run_instead_of_stalling_it(self):
        """The whole shape of the Step 4 stall, end to end.

        Models go offline between the snapshot and the browser pass, so a
        share of every candidate list is offline by the time Step 4 opens
        it. While the offline shutter went unrecognised those rooms stayed
        transient unknowns, which is precisely what publication refuses to
        run alongside - so a run could confirm hundreds of blocked models
        and still publish none of them.
        """
        with tempfile.TemporaryDirectory() as root:
            workspace = Workspace(root)
            workspace.seed()
            workspace.steps_1_to_3()
            script = page_script()
            script["https://stripchat.com/Open-One/"] = {
                "ready_state": "complete",
                "offline_notice_detected": True,
            }
            outcome = workspace.step4(script=script)
            self.assertIs(outcome.status, OutcomeStatus.SUCCEEDED)
            self.assertTrue(outcome.warnings)
            names = {
                entry["name"]
                for entry in stripchat_master_repository(
                    workspace.root
                ).load()
            }
            for blocked in BLOCKED:
                self.assertIn(f"https://stripchat.com/{blocked}/", names)
            self.assertNotIn("https://stripchat.com/Open-One/", names)


class ClassifierVersionRolloverTests(unittest.TestCase):
    """A verdict from an older decision table is not resumed into a new one."""

    def test_resume_refuses_a_foreign_classifier_and_says_what_to_do(self):
        from stripchat_adapter import StripchatWorkflowAdapter

        with tempfile.TemporaryDirectory() as root:
            workspace = Workspace(root)
            workspace.seed()
            workspace.steps_1_to_3()
            workspace.store.write_checkpoint(
                workspace.context,
                {
                    "candidate_artifact_id": "step3.candidates",
                    "candidate_sha256": "0" * 64,
                    "candidate_count": 0,
                    "retry_epoch": 0,
                    "max_retry_epochs": 3,
                    "attempt_budget_per_epoch": 2,
                    "records": {},
                    "summary": {
                        "blocked": 0,
                        "accessible": 0,
                        "unknown_transient": 0,
                        "unknown_terminal": 0,
                        "unattempted": 0,
                    },
                },
            )
            path = Path(workspace.context.checkpoint_path)
            stored = json.loads(path.read_text(encoding="utf-8"))
            stored["classifier_version"] = "sc-page-0"
            path.write_text(json.dumps(stored), encoding="utf-8")

            adapter = StripchatWorkflowAdapter(root)
            adapter.bind_session(workspace.session)
            descriptor = adapter.inspect_resume()

            self.assertFalse(descriptor.available)
            self.assertEqual(descriptor.reason_code, "checkpoint_mismatch")
            self.assertIn("Start a new run", descriptor.message)


if __name__ == "__main__":
    unittest.main()
