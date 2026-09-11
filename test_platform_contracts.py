"""Contracts for platform identity, capabilities, and path containment."""

import tempfile
import unittest
import uuid
from pathlib import Path

from platform_contracts import (
    PlatformArtifactNames,
    PlatformCapabilities,
    PlatformContractError,
    PlatformSpec,
    RunIdentity,
    validate_session_containment,
    validate_session_id,
)


def _spec(key="testplatform"):
    return PlatformSpec(
        key=key,
        display_name="Test Platform",
        session_root_name="test sessions",
        url_canonicalizer=lambda value: value,
        canonicalizer_version="1",
        classifier_version="1",
        api_contract_version="1",
        artifacts=PlatformArtifactNames(
            vpn="vpn.txt",
            local="local.txt",
            candidates="candidates.txt",
            metadata="metadata.json",
            debug="debug.txt",
            final="FINAL_BLOCKED.txt",
            master_json="MASTER_BLOCKED_DATA.json",
            master_txt="MASTER_BLOCKED.txt",
            master_meta="MASTER_BLOCKED_META.json",
        ),
        capabilities=PlatformCapabilities(
            persistent_runs=True,
            resumable_verification=True,
            adaptive_api_snapshots=True,
            machine_policy_required=True,
            typed_outcomes=True,
            master_verification=False,
            waiting_for_user_events=True,
        ),
    )


class PlatformSpecTests(unittest.TestCase):
    def test_valid_spec_accepts_lowercase_key(self):
        self.assertEqual(_spec("stripchat").key, "stripchat")

    def test_mixed_case_or_empty_keys_are_rejected(self):
        for bad in ("Stripchat", "", "STRIPCHAT", "strip chat", "strip/chat"):
            with self.subTest(key=bad), self.assertRaises(
                PlatformContractError
            ):
                _spec(bad)


class RunIdentityTests(unittest.TestCase):
    def _identity(self, **overrides):
        values = {
            "platform_key": "stripchat",
            "session_id": "session 7 28.07.2026",
            "run_id": str(uuid.uuid4()),
            "generation_id": str(uuid.uuid4()),
        }
        values.update(overrides)
        return RunIdentity(**values)

    def test_valid_identity_passes(self):
        self._identity().validate()

    def test_component_rejections(self):
        cases = {
            "empty platform": {"platform_key": ""},
            "display platform": {"platform_key": "Stripchat"},
            "empty session": {"session_id": ""},
            "separator session": {"session_id": "a/b"},
            "backslash session": {"session_id": "a\\b"},
            "dotdot session": {"session_id": "..session"},
            "drive session": {"session_id": "C:evil"},
            "control session": {"session_id": "bad\x01name"},
            "trailing dot": {"session_id": "session."},
            "trailing space": {"session_id": "session "},
            "non uuid run": {"run_id": "not-a-uuid"},
            "uppercase uuid run": {
                "run_id": str(uuid.uuid4()).upper()
            },
            "non uuid generation": {"generation_id": "12345"},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label), self.assertRaises(
                PlatformContractError
            ):
                self._identity(**overrides).validate()

    def test_unknown_platform_rejected_when_known_set_given(self):
        with self.assertRaises(PlatformContractError):
            self._identity().validate(known_platforms=("chaturbate",))

    def test_as_key_is_stable_and_not_a_path(self):
        identity = self._identity()
        self.assertEqual(
            identity.as_key(),
            "|".join(
                (
                    identity.platform_key,
                    identity.session_id,
                    identity.run_id,
                    identity.generation_id,
                )
            ),
        )

    def test_round_trip_through_dict(self):
        identity = self._identity()
        self.assertEqual(
            RunIdentity.from_dict(identity.to_dict()), identity
        )


class SessionContainmentTests(unittest.TestCase):
    def test_contained_session_resolves(self):
        with tempfile.TemporaryDirectory() as root:
            session = Path(root, "session 1")
            session.mkdir()
            resolved = validate_session_containment(session, root)
            self.assertEqual(resolved, session.resolve())

    def test_escape_and_root_itself_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(PlatformContractError):
                validate_session_containment(root, root)
            with self.assertRaises(PlatformContractError):
                validate_session_containment(
                    Path(root, "..", "outside"), root
                )

    def test_session_id_validation_rejects_windows_traps(self):
        for bad in ("trailing.", "trailing ", "a/b", "a\\b", "..", "C:x"):
            with self.subTest(value=bad), self.assertRaises(
                PlatformContractError
            ):
                validate_session_id(bad)


if __name__ == "__main__":
    unittest.main()
