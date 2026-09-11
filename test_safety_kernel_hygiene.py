"""Source hygiene for the platform-neutral safety kernel."""

import os
import re
import unittest

KERNEL_MODULES = (
    "platform_contracts.py",
    "platform_workflow.py",
    "workflow_store.py",
    "workflow_verifier.py",
    "master_repository.py",
    "workflow_types.py",
    "storage_utils.py",
)

FORBIDDEN_IMPORT = re.compile(
    r"^\s*(?:from|import)\s+(ctb_|stripchat_|mfc|xhamsterlive|legacy_stripchat)",
    re.MULTILINE,
)

ROOT = os.path.dirname(os.path.abspath(__file__))


class KernelIsolationTests(unittest.TestCase):
    def test_kernel_modules_never_import_platform_code(self):
        for name in KERNEL_MODULES:
            with self.subTest(module=name):
                source = open(
                    os.path.join(ROOT, name), "r", encoding="utf-8"
                ).read()
                match = FORBIDDEN_IMPORT.search(source)
                self.assertIsNone(
                    match,
                    f"{name} imports platform code: {match and match.group(0)!r}",
                )


class ProgressEventProducerScanTests(unittest.TestCase):
    """No production constructor may pass an OutcomeStatus into ProgressEvent."""

    def test_no_production_progress_event_uses_outcome_status(self):
        offenders = []
        for entry in sorted(os.listdir(ROOT)):
            if not entry.endswith(".py") or entry.startswith("test_"):
                continue
            source = open(
                os.path.join(ROOT, entry), "r", encoding="utf-8"
            ).read()
            if "ProgressEvent(" not in source:
                continue
            for match in re.finditer(
                r"ProgressEvent\((?:[^()]|\([^()]*\))*\)", source, re.DOTALL
            ):
                if "status=OutcomeStatus" in match.group(0):
                    offenders.append(entry)
                    break
        self.assertEqual(
            offenders,
            [],
            f"OutcomeStatus used in ProgressEvent constructors: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
