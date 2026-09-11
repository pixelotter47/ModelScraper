"""Small VPN integration boundary; manual operation has no VPN dependency."""

from dataclasses import dataclass


@dataclass(frozen=True)
class VpnProvider:
    key: str
    label: str
    automatic: bool


VPN_PROVIDERS = {
    "manual": VpnProvider("manual", "Manual / any VPN", False),
    "mullvad": VpnProvider("mullvad", "Mullvad (automatic)", True),
}


def provider(settings):
    key = settings.get("vpnProvider", "manual")
    try:
        return VPN_PROVIDERS[key]
    except (KeyError, TypeError) as exc:
        raise ValueError("Unsupported VPN provider.") from exc


def create_controller(settings, *, mullvad_factory=None):
    if not provider(settings).automatic:
        raise RuntimeError("Manual VPN mode uses Steps 1–4; no automatic controller is available.")
    if mullvad_factory is None:
        from mullvad_vpn import MullvadVpnController

        mullvad_factory = MullvadVpnController
    return mullvad_factory()
