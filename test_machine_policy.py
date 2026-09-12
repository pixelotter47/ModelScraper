"""Machine-global policy lease, journal-before-mutation, and recovery."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from machine_policy import (
    MachinePolicyController,
    MachinePolicyError,
    MachinePolicyJournal,
    MachinePolicyLease,
    MachinePolicySnapshot,
    MachinePolicyTarget,
    observe_policy,
    default_runtime_dir,
    workspace_id,
)
from workflow_types import utc_now_iso
from test_runtime_lock import windows_short_path


class FakeVpn:
    """Records commands and reports a scripted machine state."""

    def __init__(self, connected=True, relay="ie-dub-wg-103", excluded=True):
        self.connected = connected
        self.relay = relay
        self.excluded = excluded
        self.calls = []
        self.chrome_path = r"C:\chrome.exe"
        self.relay_location = "ie"
        self.fail = set()

    def _maybe_fail(self, name):
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    def status(self):
        if self.connected:
            return f"Connected\n    Relay: {self.relay}"
        return "Disconnected"

    @staticmethod
    def is_connected(text):
        return str(text).strip().lower().startswith("connected")

    @staticmethod
    def is_disconnected(text):
        return str(text).strip().lower().startswith("disconnected")

    def split_tunnel_apps(self):
        return [self.chrome_path] if self.excluded else []

    @staticmethod
    def _path_in_list(target, paths):
        return any(
            os.path.normcase(str(path)) == os.path.normcase(str(target))
            for path in paths
        )

    def connect_relay(self):
        self.calls.append("connect_relay")
        self._maybe_fail("connect_relay")
        self.connected = True

    def ensure_connected_relay(self):
        self.calls.append("ensure_connected_relay")
        self._maybe_fail("ensure_connected_relay")
        self.connected = True

    def disconnect(self):
        self.calls.append("disconnect")
        self._maybe_fail("disconnect")
        self.connected = False

    def ensure_chrome_in_split_tunnel(self):
        self.calls.append("chrome_in")
        self._maybe_fail("chrome_in")
        self.excluded = True

    def ensure_chrome_not_in_split_tunnel(self):
        self.calls.append("chrome_out")
        self._maybe_fail("chrome_out")
        self.excluded = False


def journal_for(root):
    return MachinePolicyJournal(Path(root, "machine_policy"))


def begin(journal, workspace, **overrides):
    values = dict(
        origin_workspace=workspace,
        origin_session_relative_path="session 1 28.07.2026",
        origin_manifest_relative_path=(
            "sc sessions/session 1 28.07.2026/runs/r/manifest.json"
        ),
        task_id="task-1",
        platform_key="stripchat",
        session_id="session 1 28.07.2026",
        run_id="run-1",
        generation_id="gen-1",
        target=MachinePolicyTarget(relay_code="ie"),
        initial=MachinePolicySnapshot(
            "connected", "ie-dub-wg-103", True, utc_now_iso()
        ),
    )
    values.update(overrides)
    return journal.begin(**values)


class LeaseTests(unittest.TestCase):
    def test_second_holder_gets_a_safe_busy_error(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root, "machine_policy")
            first = MachinePolicyLease(runtime_dir=runtime)
            first.acquire()
            try:
                second = MachinePolicyLease(runtime_dir=runtime)
                with self.assertRaises(MachinePolicyError) as caught:
                    second.acquire()
                self.assertEqual(caught.exception.code, "machine_lease_busy")
            finally:
                first.release()

    def test_lease_is_reacquirable_after_release(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root, "machine_policy")
            with MachinePolicyLease(runtime_dir=runtime):
                pass
            with MachinePolicyLease(runtime_dir=runtime):
                pass

    def test_lease_path_is_machine_scoped_not_workspace_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root, "machine_policy")
            first = MachinePolicyLease(runtime_dir=runtime)
            second = MachinePolicyLease(runtime_dir=runtime)
            self.assertEqual(first._lease.path, second._lease.path)


class JournalTests(unittest.TestCase):
    def test_begin_writes_journal_and_pointer_before_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            document = begin(journal, root)
            self.assertEqual(document["status"], "pending")
            self.assertEqual(document["manifest_sync_status"], "pending")
            active = journal.load_active()
            self.assertEqual(
                active["transaction_id"], document["transaction_id"]
            )
            path = (
                journal.transactions_dir
                / f"{document['transaction_id']}.json"
            )
            self.assertTrue(path.is_file())

    def test_journal_contains_no_raw_output_or_urls(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            document = begin(journal, root)
            journal.record_transition(
                document["transaction_id"],
                action="connect_relay",
                intended="connected",
                observed=MachinePolicySnapshot(
                    "connected", "ie-dub-wg-103", True, utc_now_iso()
                ),
            )
            raw = (
                journal.transactions_dir
                / f"{document['transaction_id']}.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn("stripchat.com/", raw)
            self.assertNotIn("Visible location", raw)

    def test_clear_active_only_clears_its_own_transaction(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            first = begin(journal, root)
            second = begin(journal, root)
            journal.clear_active(first["transaction_id"])
            self.assertEqual(
                journal.load_active()["transaction_id"],
                second["transaction_id"],
            )


class RestorationTests(unittest.TestCase):
    def test_both_targets_are_attempted_independently(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            document = begin(journal, root)
            vpn = FakeVpn(connected=False, excluded=False)
            vpn.fail.add("ensure_connected_relay")
            controller = MachinePolicyController(vpn, journal)
            results = controller.restore(
                document["transaction_id"], MachinePolicyTarget("ie")
            )
            self.assertFalse(results["ok"])
            self.assertFalse(results["vpn"]["ok"])
            self.assertTrue(
                results["chrome"]["ok"],
                "a VPN failure must not skip Chrome restoration",
            )
            self.assertIn("chrome_in", vpn.calls)

    def test_successful_restoration_is_journaled(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            document = begin(journal, root)
            controller = MachinePolicyController(FakeVpn(), journal)
            results = controller.restore(
                document["transaction_id"], MachinePolicyTarget("ie")
            )
            self.assertTrue(results["ok"])
            stored = journal.load_transaction(document["transaction_id"])
            self.assertEqual(stored["status"], "restored")
            self.assertEqual(len(stored["restoration_attempts"]), 1)

    def test_persistence_failure_prevents_reported_success(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            document = begin(journal, root)

            def explode(*args, **kwargs):
                raise OSError("journal is unwritable")

            journal.record_restoration_attempt = explode
            controller = MachinePolicyController(FakeVpn(), journal)
            results = controller.restore(
                document["transaction_id"], MachinePolicyTarget("ie")
            )
            self.assertFalse(results["ok"])
            self.assertEqual(
                results["error_code"], "restoration_persist_failed"
            )


class RecoveryTests(unittest.TestCase):
    def _origin_workspace(self, root):
        workspace = Path(root, "checkout")
        manifest_dir = workspace / "sc sessions" / "session 1 28.07.2026" / "runs" / "r"
        manifest_dir.mkdir(parents=True)
        manifest = {
            "schema_version": 2,
            "manifest_revision": 1,
            "restoration": {"required": False, "state": "pending"},
        }
        (manifest_dir / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return workspace

    def test_short_path_journal_is_owned_and_mirrors_the_same_checkout(self):
        with tempfile.TemporaryDirectory(prefix="modelscraper-long-recovery-") as root:
            workspace = self._origin_workspace(root).resolve()
            alias = windows_short_path(workspace)
            journal = MachinePolicyJournal(Path(root, "machine_policy"), workspace=alias)
            document = begin(journal, alias)
            reopened = MachinePolicyJournal(journal.runtime_dir, workspace=workspace)
            self.assertTrue(reopened.owns_document(document))
            result = MachinePolicyController(
                FakeVpn(connected=False, excluded=False), reopened
            ).recover_pending(origin_lease_factory=lambda origin: mock.MagicMock())
            self.assertEqual(result["manifest_sync_status"], "synced")
            self.assertTrue(result["recovered"])
            self.assertIsNone(reopened.load_active())
            manifest = json.loads((workspace / document["origin_manifest_relative_path"]).read_text())
            self.assertEqual(manifest["restoration"]["state"], "restored")

    def test_recovery_restores_and_mirrors_the_origin_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = self._origin_workspace(root)
            journal = journal_for(root)
            begin(journal, workspace)
            vpn = FakeVpn(connected=False, excluded=False)
            controller = MachinePolicyController(vpn, journal)

            class NullLease:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            result = controller.recover_pending(
                origin_lease_factory=lambda origin: NullLease()
            )
            self.assertTrue(result["recovered"])
            self.assertEqual(result["manifest_sync_status"], "synced")
            self.assertTrue(vpn.connected)
            self.assertTrue(vpn.excluded)
            self.assertIsNone(journal.load_active())
            manifest = json.loads(
                (
                    workspace
                    / "sc sessions"
                    / "session 1 28.07.2026"
                    / "runs"
                    / "r"
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["restoration"]["state"], "restored")

    def test_missing_origin_still_repairs_the_machine(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            begin(journal, Path(root, "gone"))
            vpn = FakeVpn(connected=False, excluded=False)
            controller = MachinePolicyController(vpn, journal)
            result = controller.recover_pending(
                origin_lease_factory=lambda origin: None
            )
            self.assertTrue(result["recovered"])
            self.assertEqual(result["manifest_sync_status"], "origin_missing")
            self.assertTrue(vpn.connected)
            self.assertIsNone(journal.load_active())

    def test_busy_origin_defers_only_the_mirror(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = self._origin_workspace(root)
            journal = journal_for(root)
            document = begin(journal, workspace)
            controller = MachinePolicyController(
                FakeVpn(connected=False, excluded=False), journal
            )

            def busy(origin):
                raise MachinePolicyError("workspace_lease_busy")

            result = controller.recover_pending(origin_lease_factory=busy)
            self.assertTrue(result["recovered"])
            self.assertEqual(result["manifest_sync_status"], "origin_busy")
            stored = journal.load_transaction(document["transaction_id"])
            self.assertEqual(stored["status"], "restored_manifest_pending")

    def test_failed_global_restoration_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            begin(journal, root)
            vpn = FakeVpn(connected=False, excluded=False)
            vpn.fail.add("ensure_connected_relay")
            controller = MachinePolicyController(vpn, journal)
            result = controller.recover_pending()
            self.assertFalse(result["recovered"])
            self.assertEqual(
                result["error_code"], "machine_policy_recovery_required"
            )
            self.assertIsNotNone(
                journal.load_active(),
                "an unrepaired transaction stays active",
            )

    def test_path_escape_in_origin_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            workspace = self._origin_workspace(root)
            journal = journal_for(root)
            begin(
                journal,
                workspace,
                origin_manifest_relative_path="../outside/manifest.json",
            )
            controller = MachinePolicyController(
                FakeVpn(connected=False, excluded=False), journal
            )
            result = controller.recover_pending(
                origin_lease_factory=lambda origin: None
            )
            self.assertEqual(result["manifest_sync_status"], "invalid_origin")

    def test_nothing_pending_returns_none(self):
        with tempfile.TemporaryDirectory() as root:
            journal = journal_for(root)
            controller = MachinePolicyController(FakeVpn(), journal)
            self.assertIsNone(controller.recover_pending())


class PublicEditionIsolationTests(unittest.TestCase):
    def test_other_public_checkout_also_requires_its_own_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root, "runtime")
            first = Path(root, "first-checkout")
            second = Path(root, "second-checkout")
            begin(MachinePolicyJournal(runtime), first)
            journal = MachinePolicyJournal(runtime, workspace=second)
            vpn = FakeVpn()
            result = MachinePolicyController(vpn, journal).recover_pending()
            self.assertEqual(result["error_code"], "machine_policy_foreign_workspace")
            self.assertEqual(vpn.calls, [])

    def test_foreign_pending_policy_cannot_mutate_machine_or_original_files(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = Path(root, "shared-runtime")
            personal_workspace = Path(root, "personal-checkout")
            public_workspace = Path(root, "public-checkout")
            personal_workspace.mkdir()
            public_workspace.mkdir()
            personal = MachinePolicyJournal(runtime)
            document = begin(personal, personal_workspace)
            transaction = personal.transactions_dir / f"{document['transaction_id']}.json"
            # Simulate an original-edition journal, which has no public marker.
            document.pop("runtime_namespace")
            transaction.write_text(json.dumps(document), encoding="utf-8")
            before = {p: p.read_bytes() for p in runtime.rglob("*") if p.is_file()}

            public = MachinePolicyJournal(runtime, workspace=public_workspace)
            vpn = FakeVpn()
            controller = MachinePolicyController(vpn, public)
            result = controller.recover_pending(
                origin_lease_factory=lambda origin: self.fail("must not lease foreign origin")
            )
            self.assertEqual(result["error_code"], "machine_policy_foreign_workspace")
            self.assertEqual(controller._mirror_origin(document, None), "invalid_origin")
            with self.assertRaises(MachinePolicyError):
                controller.restore(document["transaction_id"], MachinePolicyTarget("ie"))
            with self.assertRaises(MachinePolicyError):
                public.clear_active(document["transaction_id"])
            with self.assertRaises(MachinePolicyError):
                public.set_manifest_sync(document["transaction_id"], "synced")
            with self.assertRaises(MachinePolicyError):
                begin(public, public_workspace)
            self.assertEqual(vpn.calls, [])
            self.assertEqual(
                {p: p.read_bytes() for p in runtime.rglob("*") if p.is_file()}, before
            )

    def test_own_public_transaction_can_be_restored(self):
        with tempfile.TemporaryDirectory() as root:
            public = MachinePolicyJournal(Path(root, "runtime"), workspace=root)
            document = begin(public, root)
            vpn = FakeVpn(connected=False, excluded=False)
            result = MachinePolicyController(vpn, public).restore(
                document["transaction_id"], MachinePolicyTarget("ie")
            )
            self.assertTrue(result["ok"])
            self.assertEqual(vpn.calls, ["ensure_connected_relay", "chrome_in"])

    def test_default_machine_lock_remains_shared_with_original_edition(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": root}):
                first = MachinePolicyLease()
                self.assertEqual(
                    first._lease.path,
                    Path(root, "ModelScraper", "runtime", "machine_policy", "locks", "machine-policy.lock"),
                )
                with first:
                    with self.assertRaises(MachinePolicyError):
                        MachinePolicyLease(runtime_dir=default_runtime_dir()).acquire()

    def test_unreadable_shared_pointer_blocks_default_public_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": root}):
                public = MachinePolicyJournal()
                public.runtime_dir.mkdir(parents=True)
                public.active_path.write_text("incomplete", encoding="utf-8")
                vpn = FakeVpn()
                with self.assertRaises(MachinePolicyError):
                    MachinePolicyController(vpn, public).recover_pending()
                self.assertEqual(vpn.calls, [])


class ObservationTests(unittest.TestCase):
    def test_observation_normalizes_state_and_relay(self):
        snapshot = observe_policy(FakeVpn())
        self.assertEqual(snapshot.vpn_state, "connected")
        self.assertEqual(snapshot.relay_code, "ie-dub-wg-103")
        self.assertTrue(snapshot.chrome_excluded)

    def test_unreachable_cli_is_unknown_not_a_crash(self):
        class BrokenVpn(FakeVpn):
            def status(self):
                raise RuntimeError("cli missing")

            def split_tunnel_apps(self):
                raise RuntimeError("cli missing")

        snapshot = observe_policy(BrokenVpn())
        self.assertEqual(snapshot.vpn_state, "unknown")
        self.assertIsNone(snapshot.chrome_excluded)

    def test_workspace_id_is_stable_and_path_normalized(self):
        self.assertEqual(
            workspace_id(r"C:\Repo\ModelScraper"),
            workspace_id(r"c:\repo\modelscraper"),
        )


if __name__ == "__main__":
    unittest.main()
