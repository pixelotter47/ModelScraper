# Contributor instructions

This is the public source edition of ModelScraper, licensed GPL-3.0-only.

- Preserve platform separation and the shared workflow contracts.
- Keep profile-country evidence separate from observed access restrictions.
- Test with synthetic records and temporary state. Do not use live accounts,
  change a VPN or launch scraping as part of automated tests.
- Never commit sessions, cookies, logs, screenshots, model lists, credentials,
  local settings or personal notes. See `PRIVACY.md`.
- Review new public source files before adding them to `PUBLIC_FILES.txt`.
- Use the local `.venv`; run targeted pytest tests and required shared checks.
- Inspect the diff and stage only files belonging to the task. Create a focused
  local commit after verified changes. Do not push or publish without authorization.
