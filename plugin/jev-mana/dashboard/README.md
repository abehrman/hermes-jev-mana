# Jev page for the standard Hermes dashboard

`jev-mana` contributes a **Jev** tab to the normal Hermes dashboard (`hermes dashboard`), the same
way the bundled kanban plugin does: `manifest.json` + `plugin_api.py` + `dist/`. There is no separate server. Auth, remote access and the mobile layout are Hermes' own.

## Setup (any machine, zero or more profiles)

1. Install the plugin as usual (`python3 install.py`; it lands in `<hermes root>/plugins/jev-mana/`
   with its `jevkit/` beside it, which is where `plugin_api.py` looks).
2. User plugins must be enabled before their backend loads: `hermes plugins enable jev-mana`
   (or add `jev-mana` to `plugins.enabled`). Hermes ignores the API of a plugin that is not enabled.
3. Open the dashboard and choose **Jev** (`/jev`). A fresh home with no profiles and no receipts
   shows **Off**, zero calls and "no health report yet": that is the correct empty state.

Nothing else is needed: homes and profiles come from `hermes_constants.get_default_hermes_root()` and
`hermes_cli.profiles.list_profiles()`; there are no machine paths, profile names or network
dependencies in the plugin. The legacy routing page (`jev dashboard`) is kept and links here, using Hermes' own `dashboard.public_url` config key when set, else the `hermes dashboard` default `127.0.0.1:9119`.

## What it shows and never shows

Read-only. `GET /api/plugins/jev-mana/summary` and `/health` read the files listed in
`contract.json` and return numbers, booleans and fixed enum words. Recent decisions carry no id,
text or option name; a receipt's own strings are never echoed (unknown point ids become `other`, the
profile is shown only if Hermes lists it). The page never calls Jev and never runs a health check.

## Costs

| What | Cost |
| --- | --- |
| periodic health script | local only: 0 LLM, 0 Jev |
| boot / post-update smoke | 1 batched Jev call, 0 LLM (made by the producer, never this page) |
| status change | one operator turn |
| unchanged status | silent |

## Health banner

A separate health producer writes `<hermes root>/jev-health/health.json` (the `jev-decision-health/1`
report, or the ledger's `state.json`). The page only reads it, drops free text, and marks it stale after
24 h. The parent can instead call `plugin_api.set_health_provider(fn)`. Health cost model:
periodic local script (zero LLM, zero Jev); passive API-failure receipts; boot, update or on-demand
smoke is one batched Jev call with no LLM, made by the health producer, never by this page. Status
change alerts are the producer's callback (default: log only); this page sends nothing.

## Contract

`contract.json` is the registry of endpoints, files read, response keys, enums and the accepted
health fields. `tests/test_decision_kpis.py` checks the live API against it.

## Receipt rules this page enforces

* 12 points (`work.executor` added); `health_failed` is a fallback reason with a per-point breakdown in the banner.
* Create vs maintain counts use only the receipt's own `activity_class` (`create`, `maintain`; any other value is `other`; no text is kept).
* A batch's tokens are counted once (call id, else 1/`batch_size` per receipt; see `contract.json`).
* A recent receipt shows an id only if it is a canonical version-4 UUID; the chosen candidate is a number.
* Patch status is the producer's local `patch --dry-run` result from `health.json`; the dashboard runs no subprocess.
