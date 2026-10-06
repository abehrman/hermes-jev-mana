"""Decision health for this Hermes home: registration records, triggers and the report file.

``REGISTRATION`` names the real core callsite each point runs through (module:function plus the
keyword names it is called with); ``decision_health.probe_registration`` imports and binds every
one, so a renamed or missing seam is unhealthy and its point falls back to the original branch.
A name in this table is a claim the decision tests exercise, not proof by itself.

Triggers: ``boot``/``update``/``on_demand`` may add ONE batched Jev smoke request (no LLM);
``periodic`` (``python -m decision_health_runtime --periodic``) is local only: file reads and
imports, no network, no model, no agent. Output: ``<home>/jev-health/state.json`` and
``events.jsonl`` (status changes only, via ``HealthLedger``) and the full ``health.json`` report.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

import importlib

try:
    from .jevkit import decision_health as dh
except ImportError:
    from jevkit import decision_health as dh  # type: ignore

_TURN = ["agent", "user_text", "ext_prefetch_cache", "plugin_user_context", "turn_id"]
_ROUTE = ["kwargs", "task", "model_pinned", "effort_pinned", "session_id"]
REGISTRATION = {"schema": dh.REGISTRATION_SCHEMA, "points": {
    "ask.closed": {"target": "agent.decision_native:yes_no_source", "signature": ["agent", "turn"]},
    "ask.decision_gate": {"target": "agent.decision_native:select_native_step", "signature": ["agent", "s", "run_phase"]},
    "route.session": {"target": "agent.decision_sources:route_kwargs", "signature": _ROUTE},
    "route.effort": {"target": "agent.decision_sources:route_kwargs", "signature": _ROUTE},
    "skill.pick": {"target": "agent.decision_sources:apply_turn_context", "signature": _TURN},
    "context.pick": {"target": "agent.decision_sources:apply_turn_context", "signature": _TURN},
    "code.approach": {"target": "agent.decision_sources:apply_turn_context", "signature": _TURN},
    "tool.plan": {"target": "agent.decision_native:search_followup_source", "signature": ["agent", "turn"]},
    "search.pick": {"target": "agent.decision_sources:search_pick",
                    "signature": ["agent", "tool_name", "args", "result", "tool_call_id"]},
    "gui.step": {"target": "jevkit.decision_workflows:gui_choose", "signature": ["request", "choose"]},
    "approval.triage": {"target": "agent.decision_sources:approval_triage",
                        "signature": ["command", "description", "pattern_key", "session_key"]},
    "work.executor": {"target": "agent.decision_native:executor_source", "signature": ["agent", "turn"]},
}}


def importer(module: str):
    """Import a registered target. ``jevkit.*`` is the plugin's own vendored package, resolved from the plugin
    when this runs inside Hermes and from sys.path when it runs as a standalone script."""
    if module.startswith("jevkit.") and __package__:
        return importlib.import_module(f"{__package__}.{module}")
    return importlib.import_module(module)


def _config() -> Dict[str, Any]:
    from hermes_cli.config import load_config_readonly
    cfg = dict(load_config_readonly())
    jev = dict(cfg.get("jev") or {})
    if jev.get("decisions") is True:        # YAML 1.1 reads a bare `on` as true
        jev["decisions"] = "on"
    elif jev.get("decisions") is False:
        jev["decisions"] = "off"
    cfg["jev"] = jev
    return cfg


def _tuning() -> Dict[str, Any]:
    from agent.decision_config import _read_tuning, merge_tuning
    from hermes_cli.profiles import _get_default_hermes_home
    from hermes_constants import get_hermes_home
    root, home = _get_default_hermes_home(), get_hermes_home()
    return merge_tuning(_read_tuning(root / "jev_config.yaml"),
                        _read_tuning(home / "jev_config.yaml") if home != root else {})


def state_dir() -> Path:
    """``<default Hermes home>/jev-mana``: where the installer records what it installed."""
    from hermes_cli.profiles import _get_default_hermes_home
    return Path(_get_default_hermes_home()) / "jev-mana"


def install_inputs(state: Path = None) -> Dict[str, Any]:
    """HealthInputs keywords derived from the installer's manifest: hash roots, the patch to dry-run and the
    manifest itself. Empty when nothing is recorded (those probes then report ``unknown``, never healthy)."""
    state = Path(state) if state is not None else state_dir()
    try:
        manifest = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    out: Dict[str, Any] = {"manifest": manifest, "roots": {}}
    if isinstance(manifest.get("checkout"), str):
        out["roots"]["core"] = manifest["checkout"]
    if isinstance(manifest.get("plugin_dir"), str):
        out["roots"]["plugin"] = manifest["plugin_dir"]
    patch = manifest.get("patch")
    if isinstance(patch, str) and isinstance(manifest.get("checkout"), str):
        out["patch"] = {"path": str(state / patch), "root": manifest["checkout"]}
    return out


def run(trigger: str, home, *, smoke_ask=None, extra=None) -> Dict[str, Any]:
    """Assess, record a status change, and write the full report. Network only when smoke_ask.

    ``extra`` carries the install-aware inputs (manifest, roots, patch, plugin_loader, callbacks, activity,
    config/tuning loaders) that the standalone health command supplies; in-process triggers pass none and
    those probes stay ``unknown`` instead of being claimed healthy."""
    from agent.decision_config import from_config
    extra = dict(extra or {})
    alert = extra.pop("alert", None)
    kw = dict(registration=REGISTRATION, importer=importer, config_loader=_config, tuning_loader=_tuning,
              settings_factory=from_config, smoke=smoke_ask is not None, smoke_ask=smoke_ask)
    kw.update({**install_inputs(), **extra})
    inputs = dh.HealthInputs(**kw)
    report = dh.assess(inputs)
    report["trigger"] = trigger
    ledger = dh.HealthLedger(home)
    observed = ledger.observe(report, now=time.time(), alert=alert)
    ledger.dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ledger.dir, prefix=".health-")
    with os.fdopen(fd, "w") as handle:
        json.dump(report, handle, sort_keys=True, default=str)
    os.replace(tmp, ledger.dir / "health.json")
    return {"status": report["status"], "unhealthy_points": report["unhealthy_points"], "observed": observed,
            "report": report}


def on_health_check(*, trigger: str = "on_demand", **_ignored):
    """``decision_health_check`` hook (core: agent.decision_health_hook). Registered only when on."""
    from hermes_constants import get_hermes_home
    smoke = None
    if trigger in ("boot", "update", "on_demand"):
        try:
            from .jevkit import client
        except ImportError:
            from jevkit import client  # type: ignore
        smoke = client.ask
    result = run(trigger, get_hermes_home(), smoke_ask=smoke)
    result.pop("report", None)
    return result


if __name__ == "__main__":  # periodic, no agent: zero network and zero model calls
    from hermes_constants import get_hermes_home
    print(json.dumps(run("periodic", get_hermes_home(), smoke_ask=None)))
    sys.exit(0)
