# Contributing

Open an issue describing a reproducible problem or submit a focused pull request.
Use invented handles and temporary directories in examples and tests. Include the
affected platform, expected behavior and a minimal sanitized reproduction.
Never attach session folders or browser evidence containing real user data.

## Module map

| Area | Files |
| --- | --- |
| Desktop and Web entry points | `gui_app.py`, `web_app.py`, `ui/`, `web_static/` |
| Shared service and preferences | `modelscraper_service.py`, `app_settings.py` |
| Country/profile matching | `location_profiles.py`, `country_choices.py` |
| Platform definitions and workflow contracts | `platform_contracts.py`, `platform_workflow.py` |
| Chaturbate | `ctb_core.py`, `ctb_api.py`, `ctb_classifier.py`, `ctb_store.py`, `ctb_verifier.py` |
| Stripchat | `stripchat_adapter.py`, `stripchat_core.py`, `stripchat_*` |
| MyFreeCams and XHamsterLive | `mfcscrape.py`, `mfc_verifier.py`, `xhamsterlive_core.py`, `legacy_stripchat_family.py` |
| Shared persistence and verification | `workflow_*`, `master_repository.py`, `storage_utils.py` |
| Browser and VPN state | `public_runtime.py`, `temp_profile.py`, `machine_policy.py`, `mullvad_vpn.py` |
| VPN integrations and manual steps | `vpn_providers.py`, `manual_workflow.py` |

Keep platform extraction inside the relevant adapter. Common workflow modules
must not import a particular platform. Country evidence, selected profile terms,
and observed access restrictions are separate concepts.

## Development

Run `scripts/bootstrap.ps1` to create `.venv` and install the project with test
dependencies. Run targeted `python -m pytest` tests while editing and the full
suite before proposing changes to shared workflow/state code. Tests must not
contact live sites, connect a VPN, or use persistent personal profiles.

For a new country metadata adapter, preserve the platform's reported country,
record its source, and keep unknown values unknown. Do not infer nationality
from a language or convert a page restriction into a specific country block
without evidence. Add tests for mismatched country/text and incomplete snapshots.

## Public source boundary

`PUBLIC_FILES.txt` lists every permitted release file. Review a new source file
before adding it to the manifest. Stage only task-related source paths, then run:

```powershell
python scripts/check_public_tree.py
git diff --cached --check
```

The checker scans staged bytes by default. `--ref HEAD` scans committed bytes.
After committing, `python scripts/build_public_archive.py` creates a checked ZIP
from the commit only. It never copies the working directory's sessions or Git
history. Generated ZIPs stay ignored.

## License

By contributing code, you agree that your contribution is distributed under
GPL-3.0-only. Preserve applicable third-party notices.
