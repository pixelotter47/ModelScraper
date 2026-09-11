"""Characterization fence for the legacy Stripchat flow.

These tests pin the behavior the hardening plan replaces. They document the
current defects on purpose: legacy steps return ``None``, the coordinator
coerces that into success, and master publication rejects every Stripchat URL.
When a phase changes one of these behaviors, the matching test here must be
updated in the same commit so the fence stays truthful.
"""

import json
import os
import re
import tempfile
import unittest
from unittest import mock

import ctb_core
from ctb_store import MasterRepository
from stripchat_core import StripchatPaths, StripchatRunner
from workflow_coordinator import WorkflowCoordinator
from workflow_types import OutcomeStatus, StepName


STRIPCHAT_URL_GRAMMAR = re.compile(
    r"^https://stripchat\.com/[A-Za-z0-9_@-]{1,64}/$"
)


class StripchatLegacyPathsTests(unittest.TestCase):
    def test_paths_use_sc_prefixed_filenames(self):
        paths = StripchatPaths(os.path.join("base", "session"))
        self.assertTrue(paths.vpn_list.endswith("sc_vpn_list.txt"))
        self.assertTrue(paths.local_list.endswith("sc_local_list.txt"))
        self.assertTrue(paths.candidates.endswith("sc_candidates.txt"))
        self.assertTrue(paths.debug.endswith("sc_debug_log.txt"))
        self.assertTrue(paths.metadata.endswith("sc_metadata.json"))
        self.assertTrue(paths.verified.endswith("FINAL_BLOCKED.txt"))

    def test_model_and_api_url_construction(self):
        runner = StripchatRunner("")
        self.assertEqual(
            runner._model_url("Synthetic-Model@1"),
            "https://stripchat.com/Synthetic-Model@1/",
        )
        self.assertEqual(
            runner._api_models_url(500, 400),
            "https://go.stripchat.com/api/models?limit=500&offset=400",
        )

    def test_platform_identity_fields(self):
        self.assertEqual(StripchatRunner.platform_name, "Stripchat")
        self.assertEqual(StripchatRunner.site_domain, "stripchat.com")
        self.assertEqual(StripchatRunner.api_host, "go.stripchat.com")
        self.assertEqual(StripchatRunner.profile_dir_name, "uc_profile_sc")


class StripchatLegacyFalseSuccessTests(unittest.TestCase):
    """The legacy defect: every normal step returns ``None``."""

    def test_step1_and_step2_return_none_even_when_empty(self):
        with tempfile.TemporaryDirectory() as base_dir:
            runner = StripchatRunner(base_dir)
            runner.set_session(base_dir)
            with mock.patch.object(
                runner, "fetch_models_all", return_value=set()
            ):
                self.assertIsNone(runner.step_vpn_list())
                self.assertIsNone(runner.step_local_list())
            self.assertFalse(os.path.exists(runner.paths.vpn_list))
            self.assertFalse(os.path.exists(runner.paths.local_list))

    def test_step3_returns_none_when_inputs_missing(self):
        with tempfile.TemporaryDirectory() as base_dir:
            runner = StripchatRunner(base_dir)
            runner.set_session(base_dir)
            self.assertIsNone(runner.compare_lists())

    def test_step4_returns_none_when_candidates_missing(self):
        with tempfile.TemporaryDirectory() as base_dir:
            runner = StripchatRunner(base_dir)
            runner.set_session(base_dir)
            self.assertIsNone(runner.verify_candidates())

    def test_legacy_coercion_turns_none_into_success(self):
        coerced = WorkflowCoordinator._coerce(
            None, "run", "generation", "Stripchat", StepName.VPN_SNAPSHOT
        )
        self.assertIs(coerced.status, OutcomeStatus.SUCCEEDED)


class StripchatMasterPublicationDefectTests(unittest.TestCase):
    """P0: the shared master repository rejects every Stripchat URL."""

    def test_master_repository_rejects_stripchat_urls_before_writing(self):
        with tempfile.TemporaryDirectory() as base_dir:
            repository = MasterRepository(base_dir)
            with self.assertRaises(ValueError):
                repository.publish(
                    [{"name": "https://stripchat.com/Synthetic-Model@1/"}]
                )
            self.assertFalse(os.path.exists(repository.json_path))
            self.assertFalse(os.path.exists(repository.txt_path))

    def test_compile_master_list_raises_for_stripchat_finals(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session = os.path.join(base_dir, "session 1 22.01.2026")
            os.makedirs(session)
            with open(
                os.path.join(session, "FINAL_BLOCKED.txt"),
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write("https://stripchat.com/Synthetic-Model@1/\n")
            runner = StripchatRunner(base_dir)
            with self.assertRaises(Exception):
                runner.compile_master_list()
            self.assertFalse(
                os.path.exists(os.path.join(base_dir, "MASTER_BLOCKED_DATA.json"))
            )

    def test_manual_add_appends_manual_list_despite_master_failure(self):
        with tempfile.TemporaryDirectory() as base_dir:
            logs = []
            runner = StripchatRunner(base_dir, logger=logs.append)
            runner.add_manual_to_master("Synthetic-Model@1")
            manual_path = runner.get_manual_list_path()
            self.assertTrue(os.path.exists(manual_path))
            with open(manual_path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
            self.assertEqual(len(lines), 1)
            self.assertTrue(
                any(line.startswith("[ERROR]") for line in logs),
                "master rebuild failure must be logged",
            )


class StripchatDestructiveStepFourTests(unittest.TestCase):
    """Step 4 publishes and deletes evidence even when the browser dies."""

    def test_browser_failure_still_replaces_final_and_deletes_intermediates(self):
        with tempfile.TemporaryDirectory() as base_dir:
            session = os.path.join(base_dir, "session 1 22.01.2026")
            os.makedirs(session)
            runner = StripchatRunner(base_dir)
            runner.set_session(session)
            for path in (
                runner.paths.vpn_list,
                runner.paths.local_list,
                runner.paths.candidates,
                runner.paths.debug,
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("https://stripchat.com/Synthetic-Model@1/\n")
            with open(runner.paths.metadata, "w", encoding="utf-8") as handle:
                handle.write('{"passes": 2, "counts": {}}')
            with open(runner.paths.verified, "w", encoding="utf-8") as handle:
                handle.write("https://stripchat.com/Previous-Final@1/\n")

            with mock.patch.object(
                ctb_core, "_uc_chrome", side_effect=RuntimeError("boom")
            ), mock.patch.object(
                runner, "compile_master_list", return_value=0
            ) as compile_mock:
                self.assertIsNone(runner.verify_candidates())

            with open(runner.paths.verified, "r", encoding="utf-8") as handle:
                final_lines = [line for line in handle if line.strip()]
            self.assertEqual(
                final_lines,
                [],
                "prior final is replaced by an empty file after browser loss",
            )
            for path in (
                runner.paths.vpn_list,
                runner.paths.local_list,
                runner.paths.candidates,
                runner.paths.debug,
                runner.paths.metadata,
            ):
                self.assertFalse(
                    os.path.exists(path), "intermediate evidence is deleted"
                )
            compile_mock.assert_called_once()


class StripchatMasterInventoryGoldenTests(unittest.TestCase):
    """JSON/TXT inventory parity using disposable synthetic fixtures."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.master_dir = temporary.name
        handles = ("A", "Synthetic-Model@1", "synthetic_model_2", "S" * 64)
        urls = [f"https://stripchat.com/{handle}/" for handle in handles]
        with open(os.path.join(self.master_dir, "MASTER_BLOCKED_DATA.json"), "w", encoding="utf-8") as handle:
            json.dump([{"name": url} for url in urls], handle)
        with open(os.path.join(self.master_dir, "MASTER_BLOCKED.txt"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(sorted(urls)) + "\n")

    def test_synthetic_master_matches_intended_grammar(self):
        master_dir = self.master_dir
        json_path = os.path.join(master_dir, "MASTER_BLOCKED_DATA.json")
        txt_path = os.path.join(master_dir, "MASTER_BLOCKED.txt")
        with open(json_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIsInstance(data, list)
        json_urls = [entry.get("name") for entry in data]
        with open(txt_path, "r", encoding="utf-8") as handle:
            txt_urls = [line.strip() for line in handle if line.strip()]

        rejected = sum(
            1
            for url in json_urls
            if not (isinstance(url, str) and STRIPCHAT_URL_GRAMMAR.fullmatch(url))
        )
        self.assertEqual(rejected, 0, f"{rejected} URLs fail the grammar")
        self.assertEqual(len(json_urls), len(set(json_urls)))
        self.assertEqual(len(txt_urls), len(set(txt_urls)))
        self.assertEqual(set(json_urls), set(txt_urls))
        folded = {}
        collisions = 0
        for url in set(json_urls):
            key = url.lower()
            if key in folded and folded[key] != url:
                collisions += 1
            folded[key] = url
        self.assertEqual(collisions, 0)


if __name__ == "__main__":
    unittest.main()
