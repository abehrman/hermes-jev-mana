# hermes-jev-mana

## What it is

A plugin and patch set for a Hermes install that adds a harness-wide decision registry. At each instrumented semantic fork, code constructs the allowed state and candidates, Jev evaluates Choice/Score/Noul, and the host applies an adequate answer. The LLM runs only for generative work, bounded candidate drafting where code cannot enumerate alternatives, or an inadequate or error result. Deterministic lookups, calculations, and permission checks remain code-owned.

Governing boundary: Jev chooses; the host validates; existing policy and Adam authorize. Privacy denial never opens a cloud fallback.

## How the jev.decisions switch works

The master switch lives in the main Hermes `config.yaml` and is set with `hermes config set jev.decisions on|off`.

- **off**: original Hermes behavior. No Jev calls, no receipts, no added latency.
- **on**: Jev decides at each decision point that is not excluded in `jev_config.yaml`.

If Jev is unavailable or its answer is inadequate, the host falls back to the original code path. Privacy and authorization rules stay with the host and are never overridden.

## Configuration

Configuration is provided via `jev_config.yaml`. An example is at `config/jev_config.example.yaml`. The registry is profile-scoped and versioned, loaded once at session/task admission.

## Health check

Health check components:

- `plugin/jev-mana/health_cli.py`
- `plugin/jev-mana/health_smoke.py`
- `plugin/jev-mana/decision_health_runtime.py`
- `plugin/jev-mana/jevkit/decision_health.py`
- `plugin/jev-mana/jevkit/patch_health.py`

Metrics are tracked by point, profile/surface, model version, and source class, including coverage, adequacy/abstention/error rates, confidence and margin distributions, Jev vs LLM agreement, latency, and protected export checks.

## Install

The installer (`tools/jev_mana_install.py`) targets a Hermes install. A restart of the gateway is done by the user.

## Repo layout

```
.gitignore
LICENSE
NOTICE
config/jev_config.example.yaml
patches/jev-decisions.patch
patches/jev-decisions.test.patch
plugin/jev-mana/__init__.py
plugin/jev-mana/dashboard/README.md
plugin/jev-mana/dashboard/contract.json
plugin/jev-mana/dashboard/dist/index.js
plugin/jev-mana/dashboard/dist/style.css
plugin/jev-mana/dashboard/manifest.json
plugin/jev-mana/dashboard/plugin_api.py
plugin/jev-mana/decision_health_runtime.py
plugin/jev-mana/decision_hooks.py
plugin/jev-mana/decision_points.py
plugin/jev-mana/health_cli.py
plugin/jev-mana/health_smoke.py
plugin/jev-mana/jevkit/__init__.py
plugin/jev-mana/jevkit/choose.py
plugin/jev-mana/jevkit/client.py
plugin/jev-mana/jevkit/decision_adapters.py
plugin/jev-mana/jevkit/decision_health.py
plugin/jev-mana/jevkit/decision_kpis.py
plugin/jev-mana/jevkit/decision_workflows.py
plugin/jev-mana/jevkit/decisions.py
plugin/jev-mana/jevkit/patch_health.py
plugin/jev-mana/jevkit/privacy.py
plugin/jev-mana/plugin.yaml
tests/test_builder.py
tests/test_installer.py
tools/build_patch_from_snapshot.py
tools/jev_conflict_helper.py
tools/jev_mana_install.py
tools/pinned-upstream.txt
```

## Status

This project is unreviewed and not published.
