import os
import subprocess
from dataclasses import dataclass
from urllib.parse import quote

from mullvad_vpn import find_chrome_executable


@dataclass(frozen=True)
class ExternalSearchProvider:
    label: str
    url_template: str
    menu_enabled: bool = True
    enabled: bool = True


EXTERNAL_SEARCH_PROVIDERS = {
    "camwhores": ExternalSearchProvider(
        label="CamWhores",
        url_template="https://camwhores.tv/search/{query}/",
    ),
    "archivebate": ExternalSearchProvider(
        label="ArchiveBate",
        url_template="https://archivebate.com/profile/{query}",
    ),
    "curbate": ExternalSearchProvider(
        label="Curbate",
        url_template="https://curbate.tv/search?q={query}",
    ),
    "ecamrips": ExternalSearchProvider(
        label="eCamRips",
        url_template="https://www.ecamrips.com/model/en/{query}/",
    ),
    "recurbate": ExternalSearchProvider(
        label="Recurbate",
        url_template="https://recu.me/performer/{query}",
    ),
    "chaturflix": ExternalSearchProvider(
        label="Chaturflix",
        url_template="https://chaturflix.cam/performer/{query}",
    ),
    "camwhoresbay": ExternalSearchProvider(
        label="Camwhores Bay",
        url_template="https://www.camwhoresbay.com/search/?q={query}",
    ),
    "bestcam": ExternalSearchProvider(
        label="BestCam",
        url_template="https://bestcam.tv/search?search={query}",
    ),
    "livecamrips": ExternalSearchProvider(
        label="LiveCamRips",
        url_template="https://www.livecamrips.to/search/{query}/1",
    ),
    "onscreens": ExternalSearchProvider(
        label="OnScreens",
        url_template="https://www.onscreens.me/search?q={query}",
    ),
    "showcamrips": ExternalSearchProvider(
        label="ShowCamRips",
        url_template="https://showcamrips.com/model/en/{query}/",
    ),
    "cloudbate": ExternalSearchProvider(
        label="CloudBate",
        url_template="https://www.cloudbate.com/search/?q={query}",
    ),
    "webpussi": ExternalSearchProvider(
        label="WebPussi",
        url_template="https://www.webpussi.com/search/?q={query}",
    ),
    "webcamrips": ExternalSearchProvider(
        label="WebcamRips",
        url_template="https://webcamrips.to/?s={query}",
    ),
    "cumcams": ExternalSearchProvider(
        label="CumCams",
        url_template="https://cumcams.cc/performer/{query}",
    ),
    "camsmut": ExternalSearchProvider(
        label="CamSmut",
        url_template="https://camsmut.com/search?q={query}",
    ),
    "recordbate": ExternalSearchProvider(
        label="RecordBate",
        url_template="https://recordbate.com/performer/{query}",
    ),
    "camgirlfinder": ExternalSearchProvider(
        label="CamGirlFinder",
        url_template=(
            "https://camgirlfinder.net/models"
            "?model={query}&platform=&gender="
        ),
        menu_enabled=False,
    ),
}


def _registry(providers):
    """Resolve the provider registry, defaulting to the built-in one.

    Callers that hold user settings pass their own registry; everything else
    keeps working against the shipped providers, with no config file involved.
    """
    return EXTERNAL_SEARCH_PROVIDERS if providers is None else providers


def get_external_search_menu_providers(providers=None):
    """Return the ordered provider metadata exposed in the search popover."""
    return [
        {"id": provider_id, "label": provider.label}
        for provider_id, provider in _registry(providers).items()
        if provider.menu_enabled and provider.enabled
    ]


def build_external_search_url(provider_id, model_name, providers=None):
    """Build a provider search URL from a validated provider and model name."""
    registry = _registry(providers)
    provider_key = (provider_id or "").strip()
    provider = registry.get(provider_key) or registry.get(provider_key.lower())
    if provider is None:
        raise ValueError(f"Unknown external search provider: {provider_id!r}")

    query = (model_name or "").strip()
    if not query:
        raise ValueError("Model name cannot be empty.")

    return provider.url_template.format(query=quote(query, safe=""))


def build_all_external_search_urls(model_name, providers=None):
    """Build URLs for every provider shown in the external-search menu."""
    return [
        build_external_search_url(provider["id"], model_name, providers)
        for provider in get_external_search_menu_providers(providers)
    ]


def _resolve_chrome(chrome_path):
    executable = chrome_path or find_chrome_executable()
    if not executable or not os.path.isfile(executable):
        raise FileNotFoundError(f"Chrome executable not found: {executable}")
    return executable


def launch_urls_in_chrome(
    urls,
    *,
    chrome_path=None,
    process_launcher=subprocess.Popen,
):
    """Open every URL as a new Chrome tab in a single invocation."""
    targets = [str(url) for url in urls if str(url or "").strip()]
    if not targets:
        raise ValueError("No URL to open.")
    executable = _resolve_chrome(chrome_path)
    process_launcher(
        [executable, "--new-tab", *targets],
        close_fds=True,
    )
    return targets


def launch_external_search(
    provider_id,
    model_name,
    *,
    chrome_path=None,
    process_launcher=subprocess.Popen,
    providers=None,
):
    """Open a model search in a new Chrome tab and return the opened URL."""
    url = build_external_search_url(provider_id, model_name, providers)
    launch_urls_in_chrome(
        [url],
        chrome_path=chrome_path,
        process_launcher=process_launcher,
    )
    return url


def launch_all_external_searches(
    model_name,
    *,
    chrome_path=None,
    process_launcher=subprocess.Popen,
    providers=None,
):
    """Open every menu provider for a model in one Chrome invocation."""
    urls = build_all_external_search_urls(model_name, providers)
    return launch_urls_in_chrome(
        urls,
        chrome_path=chrome_path,
        process_launcher=process_launcher,
    )
