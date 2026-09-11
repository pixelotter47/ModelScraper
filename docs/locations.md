# Location selection and evidence

`location_profiles.py` owns an immutable `LocationPolicy`. `app_settings.py`
persists the editable values. `country_choices.py` provides the ISO country/territory
catalog shared by desktop, Web and validation.

`targetCountries` is a comma-separated list of ISO alpha-2 codes. It is normalized,
deduplicated and validated. `targetLocationTerms` contains optional literal words
or place names. Matching is case-insensitive, accent-insensitive and bounded by
word boundaries. Aliases are independent alternatives: country **or** alias may
match. With both fields empty, additional profile matching is disabled; the
availability snapshots remain unrestricted.

Chaturbate's reported country, typed location and language are kept separate.
Matching a language never establishes a country. A location alias does not replace
a contradictory platform country code. These fields describe public profile
claims, not independently verified identity or physical residence.

The same matcher is used by browser and HTTP collection. It emits bounded evidence
with `country_code` / `location_term` reasons and a policy fingerprint. A session's
`location_policy.json` binds its snapshots to the selected policy. A populated
session with unknown or different policy cannot execute under a new selection.

Master compilation only promotes profile evidence with a matching session/policy
fingerprint. VPN-side profile evidence additionally needs a restriction result
for the same URL in the same session. Display flags are evaluated against the
current policy each time the master is read; historical evidence stays stored.
Independent restriction records are not discarded when the profile filter changes.

Other platform adapters do not currently expose equivalent automatic country
evidence. They retain network comparison and manual country tagging. Add new
metadata extraction inside its platform adapter rather than hardcoding countries
into common workflow modules.

## VPN and network declarations

Profile location and VPN location are independent. `vpn_providers.py` defines
manual and Mullvad capabilities. Manual is the default and has no controller.
The operator prepares the network before each Step 1, 2 or 4. Manual Stripchat
records `source: operator_declared` and `vpn_state: unknown` with the declared
route and step identity. This is never reported as a measured VPN connection.
Step 3 compares existing artifacts without a network recheck in manual mode.

Stripchat binds provider and relay configuration to its run digest. Changing them
requires a fresh run/session rather than mixing manual declarations and automatic
observations. Full Auto needs the optional automatic Mullvad integration.
