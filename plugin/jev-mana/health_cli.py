#!/usr/bin/env python3
"""jev-mana health: deterministic, local, no model.

    health_cli.py [--home DIR] [--checkout DIR] [--json] [--record] [--smoke]

Default (periodic, boot-free) run: file reads, imports and ``patch --dry-run``. ZERO LLM calls, ZERO Jev
calls, no network and no spawned agent. ``--smoke`` (boot, post-update or on demand, never periodic) adds
exactly ONE batched Jev request covering every registered point, with no LLM. ``--record`` writes the status
ledger and, only on a status CHANGE, calls the single configured alert command (``alert_command`` in
jev_config.yaml, an argv list that receives the event JSON on stdin); an unchanged status stays silent.

Exit: 0 healthy or off, 1 unhealthy, 3 unknown or unmet prerequisites only, 2 usage.
Run it with the Hermes interpreter so the checkout's modules import (the installer prints the exact command).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent


def _manifest(home: Path) -> Dict[str, Any]:
    try:
        value = json.loads((home / "jev-mana" / "manifest.json").read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _plugin_loader():
    spec = importlib.util.spec_from_file_location("jev_mana_plugin", HERE / "__init__.py",
                                                  submodule_search_locations=[str(HERE)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    hooks_module = importlib.import_module("jev_mana_plugin.decision_hooks")

    class _Ctx:
        def __init__(self):
            self.hooks = []

        def register_hook(self, name, fn):
            self.hooks.append(name)

    ctx = _Ctx()
    hooks_module.register_decision_hooks(ctx)
    want = {"select_agent_step", "decision_candidates", "decision_health_check"}
    if not want <= set(ctx.hooks):
        raise RuntimeError("hooks_not_registered")


def _alert_for(tuning: Dict[str, Any]):
    argv = tuning.get("alert_command")
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) and a for a in argv)):
        return None

    def alert(event: Dict[str, Any]) -> bool:
        try:
            done = subprocess.run(argv, input=json.dumps(event), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0
    return alert


def _prerequisites(config: Dict[str, Any], tuning: Dict[str, Any]) -> List[Dict[str, str]]:
    from agent import decision_config
    out = []

    def add(name, ok, detail):
        out.append({"name": name, "status": "met" if ok else "unmet", "detail": detail})

    plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
    add("plugin_enabled", decision_config.PLUGIN in (plugins.get("enabled") or []),
        "run `hermes plugins enable jev-mana`")
    add("jev_cli", shutil.which("jev") is not None,
        "install the jev command and run `jev setup-key` yourself; without a key every point runs the original branch")
    try:
        import agent.jev_source  # noqa: F401
        authority = True
    except ImportError:
        authority = False
    add("admission_authority", authority or tuning.get("admit_unlabeled") is True,
        "no source-label authority is installed; set admit_unlabeled: true in jev_config.yaml to admit unlabeled "
        "content (local privacy scan still applies), otherwise every remote decision stays on the original branch")
    execs = decision_config.merge_executors(decision_config.DEFAULT_EXECUTORS, tuning.get("executors") or {})
    from agent import decision_executor
    for name, entry in sorted(execs.items()):
        if entry.get("type") == "hermes":
            continue
        add(f"executor_{name}", decision_executor._program(entry) is not None,
            f"{entry.get('type')} command not found on PATH; sign in yourself; the original Hermes path runs until it is")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", help="Hermes home (default: HERMES_HOME or ~/.hermes)")
    ap.add_argument("--checkout", help="Hermes checkout (default: the one the installer recorded)")
    ap.add_argument("--json", action="store_true", help="print the full JSON report")
    ap.add_argument("--record", action="store_true", help="write the ledger and alert on a status change")
    ap.add_argument("--smoke", action="store_true", help="ONE batched Jev call for all points (boot/update/on demand)")
    ap.add_argument("--trigger", default=None, help="label for the report: periodic|boot|update|on_demand")
    args = ap.parse_args(argv)

    home = Path(args.home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser().resolve()
    os.environ["HERMES_HOME"] = str(home)
    manifest = _manifest(home)
    checkout = args.checkout or manifest.get("checkout")
    if not checkout or not Path(checkout, "hermes_cli").is_dir():
        print("jev-mana health: Hermes checkout not found; pass --checkout", file=sys.stderr)
        return 2
    sys.path.insert(0, str(checkout))
    sys.path.insert(0, str(HERE))
    import decision_health_runtime as rt
    import health_smoke
    from jevkit import decision_health as dh

    config = rt._config()
    try:
        tuning = rt._tuning()
    except Exception:
        tuning = {}
    smoke_ask = None
    if args.smoke:
        from jevkit import client
        smoke_ask = client.ask
    extra = {"plugin_loader": _plugin_loader, "callbacks": health_smoke.callbacks()}
    if args.record:
        alert = _alert_for(tuning)
        if alert:
            extra["alert"] = alert
    result = rt.run(args.trigger or ("on_demand" if args.smoke else "periodic"), home, smoke_ask=smoke_ask,
                    extra=extra)
    report = result["report"]
    report["prerequisites"] = _prerequisites(config, tuning)
    unmet = [p["name"] for p in report["prerequisites"] if p["status"] == "unmet"]
    if args.json:
        print(json.dumps(report, sort_keys=True, default=str, indent=2))
    else:
        print(report["summary"])
        for p in report["prerequisites"]:
            print(f"  prerequisite {p['name']:<22}{p['status']}" + ("" if p["status"] == "met" else f"  ({p['detail']})"))
    status = report["status"]
    if status in (dh.HEALTHY, dh.OFF):
        return 3 if (unmet and report.get("enabled")) else 0
    return 1 if status == dh.UNHEALTHY else 3


if __name__ == "__main__":
    sys.exit(main())
