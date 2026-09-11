from pathlib import Path
from unittest import mock

import pytest

from machine_policy import MachinePolicyError, MachinePolicyJournal, MachinePolicyLease
from manual_workflow import run_manual_adapter_step
from platform_workflow import AdapterContractError, WorkflowOptions
from runtime_lock import WorkflowBusyError, WorkflowLease
from stripchat_adapter import StripchatWorkflowAdapter
from stripchat_core import HardenedStripchatRunner
from stripchat_store import StripchatRunStore
from test_stripchat_core import FakeApiClient, ScriptedBrowserProvider, snapshot_result
from workflow_types import OutcomeStatus, StepName, StepOutcome


STEPS = (StepName.VPN_SNAPSHOT, StepName.LOCAL_SNAPSHOT, StepName.COMPARE, StepName.VERIFY)
BLOCKED = "https://stripchat.com/Synthetic-Blocked/"


def adapter_for(tmp_path, *, shadow=False):
    root = tmp_path / "sc sessions"
    adapter = StripchatWorkflowAdapter(root, shadow=shadow)
    adapter.configure_network("manual")
    adapter.create_session()
    return adapter


def execute(tmp_path, adapter, step, *, confirmed=True, **overrides):
    values = dict(
        manual_network_confirmed=confirmed,
        machine_lease_factory=lambda: MachinePolicyLease(runtime_dir=tmp_path / "machine"),
        workspace_lease_factory=lambda workspace, **kwargs: WorkflowLease(
            workspace, runtime_dir=tmp_path / "workspace-locks", **kwargs
        ),
        journal=MachinePolicyJournal(tmp_path / "machine", workspace=tmp_path),
    )
    values.update(overrides)
    return run_manual_adapter_step(
        tmp_path, adapter, step, WorkflowOptions(headless=False, mode="manual"), **values
    )


def scripted_api(_self, route):
    return FakeApiClient(snapshot_result(
        ("Synthetic-Visible", "Synthetic-Blocked") if route == "vpn" else ("Synthetic-Visible",)
    ))


def test_all_four_manual_steps_use_typed_publication_without_any_vpn_commands(tmp_path):
    adapter = adapter_for(tmp_path)
    provider = ScriptedBrowserProvider({BLOCKED: {
        "ready_state": "complete", "account_notice_texts": ("Account Hidden",),
    }})
    with mock.patch("mullvad_vpn.MullvadVpnController", side_effect=AssertionError("VPN created")), mock.patch(
        "stripchat_core.HardenedStripchatRunner._api_client", scripted_api
    ), mock.patch.object(adapter, "_provider_factory", return_value=provider), mock.patch.object(
        adapter, "record_restoration", side_effect=AssertionError("restoration called")
    ):
        results = [execute(tmp_path, adapter, step, confirmed=step is not StepName.COMPARE) for step in STEPS]
    assert [result.status for result in results] == [OutcomeStatus.SUCCEEDED] * 4
    assert len({result.run_id for result in results}) == 1
    for result, step in zip(results, STEPS):
        assert result.restoration is None
        assert len(result.steps) == 1
        assert result.steps[0].step is step
        assert result.steps[0].session_id == result.session_id
    assert provider.observed == [BLOCKED]
    store = adapter._require_store()
    context = store.load_active()
    manifest = store.load_manifest(context)
    assert manifest["publication"]["state"] == "projected"
    assert Path(adapter.session_path, "FINAL_BLOCKED.txt").read_text(encoding="utf-8").strip() == BLOCKED
    attestations = manifest["network_attestations"]
    assert set(attestations) == {step.value for step in STEPS if step is not StepName.COMPARE}
    for step, route in ((StepName.VPN_SNAPSHOT, "vpn"), (StepName.LOCAL_SNAPSHOT, "local"), (StepName.VERIFY, "local")):
        attestation = attestations[step.value]
        assert attestation["source"] == "operator_declared"
        assert attestation["observed"]["vpn_state"] == "unknown"
        assert attestation["declared_route"] == route
    assert adapter._manual_network_declaration is None


@pytest.mark.parametrize("step", [StepName.VPN_SNAPSHOT, StepName.LOCAL_SNAPSHOT, StepName.VERIFY])
@pytest.mark.parametrize("confirmed", [False, 1, "true"])
def test_network_steps_require_explicit_boolean_confirmation_before_prepare(tmp_path, step, confirmed):
    adapter = adapter_for(tmp_path)
    with mock.patch.object(adapter, "prepare_step") as prepare:
        result = execute(tmp_path, adapter, step, confirmed=confirmed)
    assert result.status is OutcomeStatus.FAILED
    assert result.error_code == "manual_network_confirmation_required"
    prepare.assert_not_called()


@pytest.mark.parametrize("alter", [
    {"source": "observed"}, {"vpn_state": "connected"}, {"declared_route": "local"},
    {"operator_confirmed": False}, {"operator_confirmed": 1}, {"run_id": "other"},
    {"step": StepName.LOCAL_SNAPSHOT.value}, {"source": None},
])
def test_runner_rejects_unknown_state_without_matching_operator_declaration(tmp_path, alter):
    adapter = adapter_for(tmp_path)
    prepared = adapter.prepare_step(StepName.VPN_SNAPSHOT)
    adapter.declare_manual_network(prepared, StepName.VPN_SNAPSHOT, confirmed=True)
    adapter._manual_network_declaration.update(alter)
    with mock.patch("stripchat_core.HardenedStripchatRunner._api_client") as api:
        result = adapter.run_vpn_snapshot(prepared, WorkflowOptions(), lambda _event: None)
    assert result.status is OutcomeStatus.FAILED
    assert result.error_code == "manual_network_confirmation_required"
    api.assert_not_called()
    assert "network_attestations" not in adapter._require_store().load_manifest(prepared.context)


def test_manual_comparison_uses_only_existing_snapshot_artifacts(tmp_path):
    adapter = adapter_for(tmp_path)
    with mock.patch("stripchat_core.HardenedStripchatRunner._api_client", scripted_api):
        for step in STEPS[:2]:
            assert execute(tmp_path, adapter, step).status is OutcomeStatus.SUCCEEDED
    with mock.patch("stripchat_core.HardenedStripchatRunner._api_client", side_effect=AssertionError("network")), mock.patch(
        "stripchat_core.HardenedStripchatRunner._observe_policy", side_effect=AssertionError("network observation")
    ):
        assert execute(tmp_path, adapter, StepName.COMPARE, confirmed=False).status is OutcomeStatus.SUCCEEDED


@pytest.mark.parametrize("foreign", [False, True])
def test_pending_machine_journal_is_refused_without_recovery(tmp_path, foreign):
    adapter = adapter_for(tmp_path)
    journal = mock.Mock()
    journal.load_active.return_value = {"status": "pending"}
    if foreign:
        journal.require_owned_document.side_effect = MachinePolicyError("machine_policy_foreign_workspace")
    with mock.patch.object(adapter, "prepare_step") as prepare:
        result = execute(tmp_path, adapter, StepName.VPN_SNAPSHOT, journal=journal)
    assert result.error_code == ("machine_policy_foreign_workspace" if foreign else "machine_policy_recovery_required")
    prepare.assert_not_called()
    journal.begin.assert_not_called()
    journal.clear_active.assert_not_called()


def test_shared_machine_and_workspace_leases_are_held_during_the_step(tmp_path):
    adapter = adapter_for(tmp_path)
    original = adapter.run_vpn_snapshot

    def checked(*args):
        with pytest.raises(MachinePolicyError):
            MachinePolicyLease(runtime_dir=tmp_path / "machine").acquire()
        with pytest.raises(WorkflowBusyError):
            WorkflowLease(tmp_path, runtime_dir=tmp_path / "workspace-locks").acquire()
        return original(*args)

    with mock.patch.object(adapter, "run_vpn_snapshot", side_effect=checked), mock.patch(
        "stripchat_core.HardenedStripchatRunner._api_client", scripted_api
    ):
        assert execute(tmp_path, adapter, StepName.VPN_SNAPSHOT).status is OutcomeStatus.SUCCEEDED


@pytest.mark.parametrize("bad_result", ["none", "wrong_identity"])
def test_invalid_adapter_result_cannot_report_success(tmp_path, bad_result):
    adapter = adapter_for(tmp_path)

    def run(prepared, *_args):
        if bad_result == "none":
            return None
        return StepOutcome.succeeded(
            run_id="wrong", generation_id=prepared.context.generation_id,
            session_id=prepared.context.session_id, platform=adapter.spec.key,
            step=StepName.VPN_SNAPSHOT,
        )

    with mock.patch.object(adapter, "run_vpn_snapshot", side_effect=run):
        result = execute(tmp_path, adapter, StepName.VPN_SNAPSHOT)
    assert result.status is OutcomeStatus.FAILED
    assert result.error_code in ("invalid_step_outcome", "step_run_mismatch")
    assert adapter._manual_network_declaration is None


def test_incomplete_snapshot_does_not_publish_or_make_comparison_ready(tmp_path):
    adapter = adapter_for(tmp_path)
    with mock.patch("stripchat_core.HardenedStripchatRunner._api_client", return_value=FakeApiClient(
        snapshot_result((), OutcomeStatus.INCOMPLETE, "api_snapshot_unstable")
    )):
        result = execute(tmp_path, adapter, StepName.VPN_SNAPSHOT)
    assert result.status is OutcomeStatus.INCOMPLETE
    assert result.error_code == "api_snapshot_unstable"
    assert not Path(adapter.session_path, "sc_vpn_list.txt").exists()
    assert execute(tmp_path, adapter, StepName.COMPARE, confirmed=False).status is OutcomeStatus.FAILED


def test_incomplete_verification_preserves_prior_final_and_checkpoint(tmp_path):
    adapter = adapter_for(tmp_path)
    final = Path(adapter.session_path, "FINAL_BLOCKED.txt")
    final.write_text("https://stripchat.com/Synthetic-Previous/\n", encoding="utf-8")
    provider = ScriptedBrowserProvider({BLOCKED: {"navigation_error": "navigation_timeout", "current_url": ""}})
    with mock.patch("stripchat_core.HardenedStripchatRunner._api_client", scripted_api), mock.patch.object(
        adapter, "_provider_factory", return_value=provider
    ):
        for step in STEPS[:3]:
            assert execute(tmp_path, adapter, step).status is OutcomeStatus.SUCCEEDED
        result = execute(tmp_path, adapter, StepName.VERIFY)
    assert result.status is OutcomeStatus.INCOMPLETE
    assert final.read_text(encoding="utf-8") == "https://stripchat.com/Synthetic-Previous/\n"
    store = adapter._require_store()
    assert store.load_checkpoint(store.load_active())["summary"]["unknown_transient"] == 1


def test_provider_change_invalidates_resume_and_relay_is_configured(tmp_path):
    adapter = adapter_for(tmp_path)
    with mock.patch("stripchat_core.HardenedStripchatRunner._api_client", scripted_api):
        assert execute(tmp_path, adapter, StepName.VPN_SNAPSHOT).status is OutcomeStatus.SUCCEEDED
    old_digest = adapter._require_store().config["digest_sha256"]
    adapter.configure_network("mullvad", "nl")
    assert adapter._require_store().config["digest_sha256"] != old_digest
    assert adapter._runner(lambda _event: None).target_relay_prefix == "nl"
    with pytest.raises(AdapterContractError):
        adapter.prepare_step(StepName.LOCAL_SNAPSHOT)


def test_manual_provider_rejects_hidden_network_recheck(tmp_path):
    with pytest.raises(ValueError, match="manual comparison"):
        StripchatRunStore(tmp_path, config_overrides={"vpn_provider": "manual", "local_recheck": True})


def test_cancelled_manual_step_does_not_start_network_work(tmp_path):
    adapter = adapter_for(tmp_path)
    adapter.request_stop()
    with mock.patch.object(adapter, "run_vpn_snapshot") as network:
        result = execute(tmp_path, adapter, StepName.VPN_SNAPSHOT)
    assert result.status is OutcomeStatus.CANCELLED
    assert result.steps[0].step is StepName.VPN_SNAPSHOT
    assert result.restoration is None
    network.assert_not_called()
