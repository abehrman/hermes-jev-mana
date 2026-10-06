# hermes-jev-mana Operator Guide

Hermes Agent plugin plus core patch that lets Jev make decisions at fixed decision points.

## Install

```
python3 tools/jev_mana_install.py [flags]
```

Always run `--check` first. It reports what would change and writes nothing.

| Flag | Meaning |
|---|---|
| `--check` | Report only, write nothing |
| `--uninstall` | Restore checkout, remove manifest |
| `--off` | Install but leave `jev.decisions` off |
| `--profile NAME` | Target a profile (repeatable) |
| `--all-profiles` | Target every profile |
| `--checkout PATH` | Hermes checkout path |
| `--hermes-home PATH` | Hermes home directory |
| `--hermes-bin PATH` | Hermes binary path |
| `--dashboard-port N` | Dashboard port (default 9119) |
| `--allow-no-health` | Accept an install when the bundle has no runnable health command |
| `--bundle-root PATH` | Bundle root directory |

Install runs: backup, apply the patch series from `bundle.json`, configure, health check. A failure while applying or configuring rolls back. An unhealthy health check does not roll back: the install stays, the decision points fall back to the original Hermes code, and the installer exits non-zero.

The installer never restarts the gateway. It prints `hermes gateway restart` for the owner to run.

On success it writes `<home>/jev-mana/manifest.json` containing the sha256 of each installed file.

## Turn on/off

```
hermes config set jev.decisions on
hermes config set jev.decisions off
```

Set in `config.yaml`. `off` restores original Hermes behavior with no Jev calls.

## Tuning

Per-profile file: `<profile home>/jev_config.yaml`. Copied from `config/jev_config.example.yaml` only if the file is absent.

| Key | Default | Notes |
|---|---|---|
| `use_global` | `true` | Profile files start from `~/.hermes/jev_config.yaml`, then apply their own values |
| `include` | | Profile files only: re-enable points the global file excluded |
| `margin` | `0.10` | Near-tie threshold |
| `timeout_seconds` | `1.0` | Per-call timeout |
| `turn_budget_seconds` | `5.0` | Budget per turn |
| `receipts` | `true` | Record receipts |
| `exclude` | | Points that run the original Hermes code (adds to global exclusions) |

Fallback rule: any Jev failure, timeout, near-tie, or excluded point runs the original Hermes code.

### Decision points

`ask.closed`, `ask.decision_gate`, `route.session`, `route.effort`, `skill.pick`, `context.pick`, `tool.plan`, `search.pick`, `gui.step`, `approval.triage` (classifies only, never approves), `code.approach` (LLM still writes code).

## Health

```
python plugin/jev-mana/health_cli.py --home H --checkout C [options]
```

Run with the Hermes interpreter (requires `ruamel`).

| Option | Meaning |
|---|---|
| `--json` | JSON output |
| `--record` | Write ledger and alert on status change |
| `--smoke` | One batched Jev call covering all points |
| `--trigger T` | `periodic`, `boot`, `update`, or `on_demand` |

Health also lists prerequisites. `admission_authority` is unmet when no source-label authority is installed. Then every remote decision stays on the original Hermes code. Setting `admit_unlabeled: true` in `jev_config.yaml` admits unlabeled content; the local privacy scan still applies. This is a privacy choice for the owner, and the shipped default leaves it off.

Probes: registration, hashes, config, plugin, smoke, flow. The `flow` probe reports unknown until at least one decision has occurred.

## Uninstall

```
python3 tools/jev_mana_install.py --uninstall
```

Restores the checkout to its pre-install state and removes `<home>/jev-mana/manifest.json`.

## Limitations

- Tested against upstream HEAD, not the pinned commit in `tools/pinned-upstream.txt`.
- Health proves reachability, not per-point behavior.
- `--hermes-bin` may be the installed shell launcher; the installer follows it and runs health through Hermes' own runtime.
- The gateway must be restarted by the owner after install.
