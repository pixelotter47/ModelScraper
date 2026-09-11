"""Any-VPN mode never commands a VPN; automatic MFC routing stays guarded."""

import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from app_settings import AppSettings
from machine_policy import MachinePolicyJournal, MachinePolicyLease
from mullvad_vpn import MullvadVpnController
from test_machine_policy import begin as begin_policy
from test_modelscraper_service import _FakeVpn, _make_service
from vpn_providers import create_controller, provider
from workflow_types import OutcomeStatus, StepName, StepOutcome


def make_service(root, mode="manual", vpn=None):
    service, runners = _make_service(str(root), vpn=vpn)
    service.update_settings({"vpnProvider": mode})
    if mode == "manual":
        service._vpn_factory = Mock(side_effect=AssertionError("Manual mode called a VPN factory"))
    return service, runners


def finish(service, started):
    assert started
    assert service.wait_for_current_task(timeout=5)


def manual_mfc_pair(service):
    assert service.set_platform("MyFreeCams")
    assert service.create_session()
    finish(service, service.start_vpn_list(manual_network_confirmed=True))
    finish(service, service.start_local_list(manual_network_confirmed=True))


def test_new_install_defaults_to_manual_and_never_constructs_a_controller(tmp_path):
    settings = AppSettings(tmp_path)
    assert provider(settings).key == "manual"
    factory = Mock()
    with pytest.raises(RuntimeError, match="Manual VPN"):
        create_controller(settings, mullvad_factory=factory)
    factory.assert_not_called()


def test_manual_full_auto_and_unconfirmed_steps_are_refused_without_vpn(tmp_path):
    service, runners = make_service(tmp_path)
    assert not service.start_full_auto_flow()
    assert not service.start_vpn_list()
    assert not service.start_local_list()
    assert not service.start_verify()
    assert not service.verify_master_list()
    assert not service.verify_master_list_sync()
    with pytest.raises(RuntimeError, match="Full Auto"):
        service.run_full_auto_flow_sync(
            runners["Chaturbate"], "Chaturbate", headless=True, start_minimized=True
        )
    service._vpn_factory.assert_not_called()
    assert not any(call[0].startswith(("step_", "verify_")) for call in runners["Chaturbate"].calls)


def test_manual_mfc_uses_current_session_reference_pair_without_vpn(tmp_path):
    service, runners = make_service(tmp_path)
    manual_mfc_pair(service)
    finish(service, service.start_verify(manual_network_confirmed=True))
    calls = [call[0] for call in runners["MyFreeCams"].calls]
    assert calls.count("capture_live_reference") == 1
    assert calls.count("capture_local_reference") == 1
    assert calls.count("verify_live_models") == 1
    assert service._manual_mfc_references == {}
    service._vpn_factory.assert_not_called()


@pytest.mark.parametrize("invalid", ["expired", "missing", "other_session", "local_before_vpn", "future"])
def test_manual_mfc_rejects_invalid_reference_pairs_without_publication(tmp_path, invalid):
    service, runners = make_service(tmp_path)
    manual_mfc_pair(service)
    refs = service._manual_mfc_references
    if invalid == "expired":
        refs["vpn_at"] = time.monotonic() - 121
    elif invalid == "missing":
        service._manual_mfc_references = {}
    elif invalid == "other_session":
        service.create_session()
    elif invalid == "local_before_vpn":
        refs["local_at"] = refs["vpn_at"] - 1
    else:
        refs["vpn_at"] = time.monotonic() + 100
    runner = runners["MyFreeCams"]
    runner.verify_live_models = Mock()
    before = runner.get_master_list_models()
    finish(service, service.start_verify(manual_network_confirmed=True))
    runner.verify_live_models.assert_not_called()
    assert runner.get_master_list_models() == before
    service._vpn_factory.assert_not_called()


def test_manual_mfc_does_not_reuse_reference_pair_after_restart(tmp_path):
    first, _ = make_service(tmp_path)
    manual_mfc_pair(first)
    second, runners = make_service(tmp_path)
    second.set_platform("MyFreeCams")
    assert second._set_session(first._runners["MyFreeCams"].session_folder)
    runner = runners["MyFreeCams"]
    runner.verify_live_models = Mock()
    finish(second, second.start_verify(manual_network_confirmed=True))
    runner.verify_live_models.assert_not_called()
    second._vpn_factory.assert_not_called()


def test_provider_changes_invalidate_manual_reference_pair(tmp_path):
    service, runners = make_service(tmp_path)
    manual_mfc_pair(service)
    service.update_settings({"vpnProvider": "mullvad"})
    service.update_settings({"vpnProvider": "manual"})
    assert service._manual_mfc_references == {}
    runner = runners["MyFreeCams"]
    runner.verify_live_models = Mock()
    finish(service, service.start_verify(manual_network_confirmed=True))
    runner.verify_live_models.assert_not_called()
    service._vpn_factory.assert_not_called()


def test_next_controller_reads_another_process_latest_relay_and_provider(tmp_path):
    service, _ = make_service(tmp_path, mode="mullvad")
    service._vpn_factory = lambda: MullvadVpnController(command_runner=Mock())
    external = AppSettings(tmp_path)
    external.set("vpnRelayLocation", "de")
    assert service._new_vpn_controller().relay_location == "de"
    external.set("vpnProvider", "manual")
    service._vpn_factory = Mock()
    with pytest.raises(RuntimeError, match="Manual VPN"):
        service._new_vpn_controller()
    service._vpn_factory.assert_not_called()


def test_manual_record_reconciliation_never_instantiates_a_vpn_controller(tmp_path):
    service, _ = make_service(tmp_path)
    shared = MachinePolicyJournal()
    begin_policy(MachinePolicyJournal(shared.runtime_dir), tmp_path / "foreign-checkout")
    result = service.reconcile_machine_policy_record()
    assert result["error_code"] == "machine_policy_foreign_workspace"
    service._vpn_factory.assert_not_called()


def test_settings_gateway_refreshes_idle_provider_but_freezes_busy_state(tmp_path):
    service, _ = make_service(tmp_path)
    service._manual_mfc_references = {"session": "synthetic-session", "vpn_at": 1}
    external = AppSettings(tmp_path)
    external.set("vpnProvider", "mullvad")
    service._busy = True
    assert service.get_settings()["vpnProvider"] == "manual"
    assert service._manual_mfc_references
    service._busy = False
    assert service.get_settings()["vpnProvider"] == "mullvad"
    assert service._manual_mfc_references == {}


def test_real_stripchat_adapter_is_configured_for_manual_before_dispatch(tmp_path):
    from stripchat_adapter import StripchatWorkflowAdapter

    service, _ = make_service(tmp_path)
    adapter = StripchatWorkflowAdapter(tmp_path / "sc sessions")
    adapter.create_session()

    def snapshot(prepared, options, progress):
        assert adapter.network_provider == "manual"
        assert adapter.config_overrides["target_relay_prefix"] == ""
        context = prepared.context
        return StepOutcome.succeeded(
            run_id=context.run_id, generation_id=context.generation_id,
            session_id=context.session_id, platform="stripchat", step=StepName.VPN_SNAPSHOT,
        )

    adapter.run_vpn_snapshot = Mock(side_effect=snapshot)
    outcome = service._run_hardened_step_sync(
        adapter, StepName.VPN_SNAPSHOT, manual_network_confirmed=True,
    )
    assert outcome.status is OutcomeStatus.SUCCEEDED
    adapter.run_vpn_snapshot.assert_called_once()
    service._vpn_factory.assert_not_called()


class AutomaticVpn(_FakeVpn):
    def ensure_connected_ireland(self):
        self.calls.append("restore_relay")


def automatic_mfc(root):
    vpn = AutomaticVpn()
    service, runners = make_service(root, mode="mullvad", vpn=vpn)
    service.set_platform("MyFreeCams")
    service.create_session()
    return service, runners["MyFreeCams"], vpn


def test_automatic_mfc_restores_after_partial_capture_failure_and_never_verifies(tmp_path):
    service, runner, vpn = automatic_mfc(tmp_path)
    runner.capture_local_reference = Mock(side_effect=RuntimeError("Synthetic local capture failure"))
    runner.verify_live_models = Mock()
    before = runner.get_master_list_models()
    finish(service, service.start_verify())
    runner.verify_live_models.assert_not_called()
    assert runner.get_master_list_models() == before
    assert vpn.calls == ["remove_chrome_from_split_tunnel", "connect_ireland", "disconnect",
                         "restore_relay", "add_chrome_to_split_tunnel"]
    outcome = service._last_outcome
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.error_code == "mfc_verification_failed"
    assert outcome.restoration.status is OutcomeStatus.SUCCEEDED


def test_automatic_mfc_holds_machine_lease_through_capture_and_restore(tmp_path):
    service, runner, vpn = automatic_mfc(tmp_path)
    original = runner.capture_live_reference

    def capture():
        from machine_policy import MachinePolicyError
        with pytest.raises(MachinePolicyError):
            MachinePolicyLease().acquire()
        return original()

    runner.capture_live_reference = capture
    runner.verify_live_models = Mock(return_value=StepOutcome.succeeded(
        run_id="synthetic-run", generation_id="synthetic-generation", platform="MyFreeCams", step=StepName.VERIFY,
    ))
    finish(service, service.start_verify())
    assert service._last_outcome.status is OutcomeStatus.SUCCEEDED
    assert vpn.calls[0] == "remove_chrome_from_split_tunnel"
    assert vpn.calls[-2:] == ["restore_relay", "add_chrome_to_split_tunnel"]


def test_automatic_mfc_refuses_foreign_policy_without_capture_or_restoration(tmp_path):
    service, runner, vpn = automatic_mfc(tmp_path)
    shared = MachinePolicyJournal()
    begin_policy(MachinePolicyJournal(shared.runtime_dir), Path(tmp_path, "foreign-checkout"))
    runner.capture_live_reference = Mock()
    runner.verify_live_models = Mock()
    finish(service, service.start_verify())
    runner.capture_live_reference.assert_not_called()
    runner.verify_live_models.assert_not_called()
    assert vpn.calls == []
    assert service._last_outcome.error_code == "machine_policy_foreign_workspace"
    assert service._last_outcome.restoration is None
