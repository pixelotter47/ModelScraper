# Public release boundary

The public edition is maintained as an independent source tree with a new Git
history. Do not merge or push a private predecessor's history into this repository.
Removing a file from the latest checkout does not remove it from old commits;
see [GitHub's sensitive-data guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).

Excluded local data includes session roots, master/manual/blacklist files, run
manifests, collected profiles, browser cookies, caches, downloaded media, logs,
page dumps, `.env` files, credentials, personal configuration and development
notes. `.gitignore` prevents common accidental additions; the release scanner
also requires an explicit source-file manifest.

All displayed model records come from data collected or entered on the machine
running the application. New installations do not ship a model database. External
search actions deliberately open a selected provider with the selected handle;
their preferences can be changed locally.

Persistent browser state belongs to this public checkout. The machine-wide VPN
mutex remains shared because changing a VPN affects every application. Foreign
pending recovery is refused instead of altering another checkout's manifest.

Before making a repository public, inspect the committed file list, commit author
identity and every branch/tag being pushed. Publish only the public repository.
Do not ZIP a personal working directory or upload a `.git` directory. The helper
`scripts/build_public_archive.py` exports only checked committed source files.

If reporting a bug, replace real handles and paths with synthetic values and
remove credentials, cookies and IP addresses. If a secret is accidentally shared,
revoke or rotate it before trying to clean repository history.
