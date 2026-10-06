"""Portable, deterministic health for every registered decision point. No model call, no classifier.

Everything the host owns is injected (registration record, install manifest, config and
plugin loaders, smoke callbacks, receipt activity, alert adapter); nothing here knows a
path, profile, fleet target or serving process. Reading is the default: probes never
write, patch, restart, schedule or message. Only ``HealthLedger`` writes, to one local
state/receipt file per Hermes home, and only when the caller asks it to.

Statuses: ``healthy`` (positively verified), ``unhealthy`` (a probe failed),
``unknown`` (no evidence supplied: never claimed healthy), ``not_applicable``.
Overall adds ``off`` (config verified off).

Auto-fallback contract (``HealthGuard``), exact and tested:

* The unhealthy-point set is computed here; the parent supplies it as a callable.
* A request whose point is in the set is NOT scanned, admitted, sent or charged against
  the turn budget. Its receipt is ``evaluation == reason == "health_failed"``,
  ``adequate False``, ``recommendation None``, ``applied None``, ``fallback "original"``,
  ``latency_ms 0``, in the request's batch position. The host then takes its literal
  original branch with unchanged inputs and model. There is never a bespoke fallback lane.
* Requests for healthy points go through the wrapped evaluator unchanged and in order.
* Off (no evaluator) means no guard: ``HealthGuard.wrap(None, ...)`` is ``None``, so a
  missing point costs no scan, deadline or receipt while off.
* If the unhealthy-set callable raises, every request falls back (fail closed to original).
* Malformed batches are passed to the wrapped evaluator so it raises exactly as it would.
* ``verdict(point)`` gives generic hooks ``{"usable": False, "fallback": "original", "reason":
  "health_failed"}`` for an unhealthy point (the parent wires it); health never runs the action.
* Unverified registration reachability is unhealthy; other missing evidence (manifest, config,
  callbacks, activity) stays ``unknown`` and is not in the set.
* Cost: the periodic script is local only (file reads, imports, and ``patch --dry-run``): no
  LLM, no Jev, no network, no agent or subagent. Only the explicit ``smoke`` option makes ONE
  batched Jev request for all points and reports measured tokens.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import logging
import math
import os
import re
import time
import uuid
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from . import client
from .decisions import REGISTRY

try:  # stdlib, whichever the platform has
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

log = logging.getLogger("jevkit.health")

HEALTHY, UNHEALTHY, UNKNOWN, NOT_APPLICABLE, OFF = "healthy", "unhealthy", "unknown", "not_applicable", "off"


def points() -> tuple:
    """The registry as it is NOW (never a frozen list): a point added upstream is probed too."""
    return tuple(REGISTRY)


def __getattr__(name):  # POINT_IDS stays importable but is always current, never a hardcoded count
    if name == "POINT_IDS":
        return points()
    raise AttributeError(name)


REASON = "health_failed"
REGISTRATION_SCHEMA = "jev-decision-registration/1"
REPORT_SCHEMA = "jev-decision-health/1"
# full: must be healthy for a point to be healthy. fail_only: only a failure counts against a
# point. info: reported, never gating (a Jev outage already falls back per request).
GATING = {"registration": "full", "hashes": "full", "config": "full", "plugin": "fail_only",
          "patch": "fail_only", "smoke": "fail_only", "flow": "fail_only", "jev": "info"}
_RANK = {UNHEALTHY: 3, UNKNOWN: 2, HEALTHY: 1, NOT_APPLICABLE: 0}


def _p(status: str, reason: str = "", **extra) -> Dict[str, Any]:
    return {"status": status, "reason": reason, **extra}


def _probe(name: str, status: str, reason: str = "", points: Mapping[str, Mapping] = None, **detail) -> Dict[str, Any]:
    """A probe result. ``points`` overrides the probe-wide status for named points."""
    return {"name": name, "gating": GATING[name], "status": status, "reason": reason,
            "points": {k: dict(v) for k, v in (points or {}).items()}, **detail}


# ---------------------------------------------------------------- inputs

class HealthInputs:
    """Everything the probes may use. Every field is optional; absent evidence is ``unknown``."""

    def __init__(self, *, registration: Mapping = None, importer: Callable[[str], Any] = None,
                 manifest: Mapping = None, roots: Mapping[str, Any] = None,
                 config_loader: Callable[[], Mapping] = None, tuning_loader: Callable[[], Mapping] = None,
                 settings_factory: Callable[[Mapping, Mapping], Any] = None,
                 plugin_loader: Callable[[], Any] = None,
                 patch: Mapping = None, runner: Callable = None,
                 callbacks: Mapping[str, Mapping[str, Callable]] = None,
                 activity: Mapping[str, Mapping] = None,
                 smoke: bool = False, smoke_ask: Callable = None, window_seconds: float = 3600.0):
        self.registration, self.importer = registration, importer or importlib.import_module
        self.manifest, self.roots = manifest, dict(roots or {})
        self.config_loader, self.tuning_loader, self.settings_factory = config_loader, tuning_loader, settings_factory
        self.plugin_loader, self.patch, self.runner = plugin_loader, patch, runner or _run
        self.callbacks, self.activity = callbacks, activity
        # smoke is the ONLY network path: one batched Jev request, explicit (boot/update/on demand).
        # The periodic script leaves it False and makes no network call at all.
        self.smoke, self.smoke_ask, self.window_seconds = smoke, smoke_ask, float(window_seconds)


# ---------------------------------------------------------------- probes

def probe_registration(inputs: HealthInputs) -> Dict[str, Any]:
    """Runtime registration record + import + signature for every registered point.

    The record comes from the host runtime, or from the install manifest's ``registration`` key.
    Without either, reachability is unknown, and unknown reachability is unhealthy: the point
    falls back to its original branch rather than being assumed wired.

    A point is healthy only if its record names a target that imports, resolves to a
    callable, and whose signature accepts exactly the recorded parameter names. Registry
    names alone prove nothing and are never reported as reachable.
    """
    record = inputs.registration
    if record is None and isinstance(inputs.manifest, Mapping):
        record = inputs.manifest.get("registration")
    if not isinstance(record, Mapping):
        return _probe("registration", UNHEALTHY, "reachability_unknown:no_registration_record",
                      {pid: _p(UNHEALTHY, "reachability_unknown") for pid in points()})
    if record.get("schema") != REGISTRATION_SCHEMA or not isinstance(record.get("points"), Mapping):
        return _probe("registration", UNHEALTHY, "invalid_registration_record")
    out = {}
    for pid in points():
        out[pid] = _check_target(record["points"].get(pid), inputs.importer)
    bad = [k for k, v in out.items() if v["status"] != HEALTHY]
    return _probe("registration", UNHEALTHY if bad else HEALTHY, f"{len(bad)}_failed" if bad else "",
                  out, extra_points=sorted(set(record["points"]) - set(points())))


def _check_target(entry: Any, importer: Callable) -> Dict[str, Any]:
    if not isinstance(entry, Mapping):
        return _p(UNHEALTHY, "not_registered")
    target, names = entry.get("target"), entry.get("signature")
    if (not isinstance(target, str) or target.count(":") != 1 or not all(target.split(":"))
            or not isinstance(names, list) or not all(isinstance(n, str) for n in names)):
        return _p(UNHEALTHY, "invalid_entry")
    module, attr = target.split(":")
    try:
        obj = importer(module)
    except (Exception, SystemExit) as error:  # a broken import is the finding
        return _p(UNHEALTHY, "import_failed:" + type(error).__name__)
    for part in attr.split("."):
        obj = getattr(obj, part, None)
    if not callable(obj):
        return _p(UNHEALTHY, "target_missing")
    try:
        inspect.signature(obj).bind(**{n: None for n in names})
    except (TypeError, ValueError):
        return _p(UNHEALTHY, "signature_mismatch")
    return _p(HEALTHY)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def probe_hashes(inputs: HealthInputs) -> Dict[str, Any]:
    """Hash drift of installed files against the install manifest.

    Manifest groups: ``core_files`` (root ``roots['core']``), ``plugin_files_verified_not_packaged``
    (root ``roots['plugin']``) and any ``files`` list with its own ``group``. An entry may name
    the ``points`` it backs; otherwise drift in it taints every point. No manifest or no root
    is unknown. Files are only read.
    """
    manifest = inputs.manifest
    if not isinstance(manifest, Mapping):
        return _probe("hashes", UNKNOWN, "no_manifest")
    groups = (("core", manifest.get("core_files")), ("plugin", manifest.get("plugin_files_verified_not_packaged")))
    checked, drift, missing, unrooted = 0, [], [], 0
    tainted: Dict[str, str] = {}
    all_tainted = ""
    for group, entries in groups:
        for entry in entries or ():
            if not isinstance(entry, Mapping) or not isinstance(entry.get("path"), str) \
                    or not isinstance(entry.get("installed_sha256"), str):
                return _probe("hashes", UNHEALTHY, "invalid_manifest")
            root = inputs.roots.get(group)
            if root is None:
                unrooted += 1
                continue
            base = Path(root).resolve()
            path = (base / entry["path"]).resolve()
            if base != path and base not in path.parents:
                return _probe("hashes", UNHEALTHY, "manifest_path_escapes_root")
            checked += 1
            try:
                ok = path.is_file() and _sha256(path) == entry["installed_sha256"]
                reason = "drift" if path.is_file() else "missing"
            except OSError:
                ok, reason = False, "unreadable"
            if ok:
                continue
            (missing if reason == "missing" else drift).append(entry["path"])
            scope = [x for x in entry.get("points") or () if x in REGISTRY]
            if scope:
                for pid in scope:
                    tainted.setdefault(pid, reason + ":" + entry["path"])
            else:
                all_tainted = all_tainted or reason + ":" + entry["path"]
    if checked == 0:
        return _probe("hashes", UNKNOWN, "no_rooted_entries" if unrooted else "empty_manifest")
    if all_tainted:
        return _probe("hashes", UNHEALTHY, all_tainted, checked=checked, drift=sorted(drift), missing=sorted(missing))
    points = {pid: _p(UNHEALTHY, why) for pid, why in tainted.items()}
    if tainted:
        return _probe("hashes", UNHEALTHY, f"{len(tainted)}_points_tainted", points,
                      checked=checked, drift=sorted(drift), missing=sorted(missing))
    return _probe("hashes", UNKNOWN if unrooted else HEALTHY, "unrooted_entries" if unrooted else "",
                  checked=checked, drift=[], missing=[])


_TUNING_KEYS = ("exclude", "margin", "timeout_seconds", "turn_budget_seconds", "receipts")


def validate_tuning(tuning: Any) -> List[str]:
    """Problems with a tuning mapping, by the same bounds the normal config loader enforces."""
    if not isinstance(tuning, Mapping):
        return ["tuning_not_mapping"]
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)  # noqa: E731
    problems = []
    exclude = tuning.get("exclude") or []
    if not isinstance(exclude, list) or any(not isinstance(x, str) for x in exclude):
        problems.append("exclude_invalid")
    elif any(x not in REGISTRY for x in exclude):
        problems.append("exclude_unknown_point")
    for key, low, high in (("margin", 0.01, 1.0), ("timeout_seconds", 0.0, 60.0), ("turn_budget_seconds", 0.0, 300.0)):
        value = tuning.get(key)
        if key in tuning and (not num(value) or not (low <= value <= high if key == "margin" else low < value <= high)):
            problems.append(key + "_invalid")
    if "receipts" in tuning and not isinstance(tuning["receipts"], bool):
        problems.append("receipts_invalid")
    return problems


def probe_config(inputs: HealthInputs) -> Dict[str, Any]:
    """The real config and effective tuning, via the host's normal loaders.

    ``jev.decisions`` on but the host's effective settings disabled (plugin disabled, bad
    tuning) is unhealthy: the operator believes it is on and it is silently off.
    """
    if inputs.config_loader is None:
        return _probe("config", UNKNOWN, "no_config_loader", enabled=None, excluded=[])
    try:
        config = inputs.config_loader()
        tuning = inputs.tuning_loader() if inputs.tuning_loader else {}
    except Exception as error:  # noqa: BLE001
        return _probe("config", UNHEALTHY, "config_unloadable:" + type(error).__name__, enabled=None, excluded=[])
    if not isinstance(config, Mapping):
        return _probe("config", UNHEALTHY, "config_not_mapping", enabled=None, excluded=[])
    jev = config.get("jev", {})
    raw = jev.get("decisions", "off") if isinstance(jev, Mapping) else None
    if raw not in ("on", "off"):
        return _probe("config", UNHEALTHY, "invalid_decisions_value", enabled=None, excluded=[])
    problems = validate_tuning(tuning if tuning is not None else {})
    if inputs.settings_factory is None:
        return _probe("config", UNKNOWN, "no_settings_factory", enabled=None, excluded=[], problems=problems,
                      raw=raw)
    try:
        settings = inputs.settings_factory(config, tuning if tuning is not None else {})
        enabled = getattr(settings, "enabled", None)
        excluded = sorted(x for x in getattr(settings, "exclude", ()) if x in REGISTRY)
    except Exception as error:  # noqa: BLE001
        return _probe("config", UNHEALTHY, "settings_error:" + type(error).__name__, enabled=None, excluded=[])
    if not isinstance(enabled, bool):
        return _probe("config", UNHEALTHY, "settings_invalid", enabled=None, excluded=[])
    hard = [x for x in problems if x != "exclude_unknown_point"]
    if raw == "on" and not enabled:
        return _probe("config", UNHEALTHY, "on_but_effective_off", enabled=False, excluded=excluded, problems=problems)
    if hard and raw == "on":
        return _probe("config", UNHEALTHY, "invalid_tuning", enabled=enabled, excluded=excluded, problems=problems)
    return _probe("config", HEALTHY, "", enabled=enabled, excluded=excluded, problems=problems)


def probe_plugin(inputs: HealthInputs) -> Dict[str, Any]:
    """Run the host's own plugin load in-process (it should register against a throwaway context)."""
    if inputs.plugin_loader is None:
        return _probe("plugin", UNKNOWN, "no_plugin_loader")
    try:
        inputs.plugin_loader()
    except Exception as error:  # noqa: BLE001
        return _probe("plugin", UNHEALTHY, "plugin_load_failed:" + type(error).__name__)
    return _probe("plugin", HEALTHY)


def _run(argv: Sequence[str], cwd: str, timeout: float) -> int:
    return subprocess.run(list(argv), cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=timeout, check=False).returncode


def probe_patch(inputs: HealthInputs) -> Dict[str, Any]:
    """Is the packaged patch applied? Dry-run ``patch --dry-run`` only; no file is ever changed.

    ``inputs.patch`` is ``{"path", "root", "strip"=1, "timeout"=20}``. No patch or no patch file
    is ``not_applicable``. Reverse applies cleanly: installed. Forward applies cleanly: not
    installed. Neither: the tree drifted from the patch.
    """
    spec = inputs.patch
    if not isinstance(spec, Mapping) or not spec.get("path") or not Path(str(spec["path"])).is_file():
        return _probe("patch", NOT_APPLICABLE, "no_patch")
    root = spec.get("root")
    if not root or not Path(str(root)).is_dir():
        return _probe("patch", UNKNOWN, "no_patch_root")
    # -N: a reversed or already-applied patch is a failure, never silently flipped to succeed.
    base = ["patch", "--dry-run", "-N", f"-p{int(spec.get('strip', 1))}", "-d", str(root), "-i", str(Path(spec["path"]).resolve())]
    try:
        timeout = float(spec.get("timeout", 20))
        if inputs.runner(base[:2] + ["-R"] + base[2:], str(root), timeout) == 0:
            return _probe("patch", HEALTHY, "applied")
        if inputs.runner(base, str(root), timeout) == 0:
            return _probe("patch", UNHEALTHY, "not_applied")
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return _probe("patch", UNKNOWN, "patch_check_error:" + type(error).__name__)
    return _probe("patch", UNHEALTHY, "drifted")


_OFF_ZERO = ("evaluations", "network", "receipts")


def probe_smoke(inputs: HealthInputs) -> Dict[str, Any]:
    """Per-point off/on smoke through host-supplied callbacks (synthetic, offline by contract).

    ``callbacks[point]['off']()`` returns counters: ``evaluations``, ``network``, ``receipts``
    all 0 and ``identical`` True (schemas and provider payload unchanged). ``callbacks[point]['on']()``
    returns a list of decision receipts for that point, each ``decision_receipt.v1`` with a
    ``fallback`` that is ``original`` or ``none``. A point without callbacks is unknown.
    """
    if not isinstance(inputs.callbacks, Mapping):
        return _probe("smoke", UNKNOWN, "no_callbacks")
    out = {}
    for pid in points():
        pair = inputs.callbacks.get(pid)
        if not isinstance(pair, Mapping) or not callable(pair.get("off")) or not callable(pair.get("on")):
            out[pid] = _p(UNKNOWN, "no_callback")
            continue
        out[pid] = _smoke_point(pid, pair)
    worst = max((v["status"] for v in out.values()), key=_RANK.get)
    return _probe("smoke", worst, "", out)


def _smoke_point(pid: str, pair: Mapping) -> Dict[str, Any]:
    try:
        off = pair["off"]()
        if (not isinstance(off, Mapping) or off.get("identical") is not True
                or any(off.get(k) != 0 for k in _OFF_ZERO)):
            return _p(UNHEALTHY, "off_not_inert")
    except Exception as error:  # noqa: BLE001
        return _p(UNHEALTHY, "off_smoke_error:" + type(error).__name__)
    try:
        receipts = pair["on"]()
        if not isinstance(receipts, (list, tuple)) or not receipts:
            return _p(UNHEALTHY, "on_no_receipt")
        for item in receipts:
            if (not isinstance(item, Mapping) or item.get("schema") != "decision_receipt.v1"
                    or item.get("point_id") != pid or item.get("fallback") not in ("original", "none")):
                return _p(UNHEALTHY, "on_bad_receipt")
    except Exception as error:  # noqa: BLE001
        return _p(UNHEALTHY, "on_smoke_error:" + type(error).__name__)
    return _p(HEALTHY)


def probe_flow(inputs: HealthInputs, enabled: Optional[bool], excluded: Iterable[str]) -> Dict[str, Any]:
    """Receipts flowing, judged only against an enabled, eligible workload.

    ``activity[point] = {"eligible": n, "receipts": m}`` counted by the host over the recent
    window; an optional ``eligible_age_s`` (age of the newest eligible item) older than
    ``window_seconds`` means nothing eligible happened recently. Only an OBSERVED, recent,
    eligible workload with zero receipts is unhealthy. A quiet session is not unhealthy; off or
    excluded points are not applicable; no activity data is unknown. Passive: reads counters only.
    """
    if enabled is not True:
        return _probe("flow", NOT_APPLICABLE, "not_enabled")
    if not isinstance(inputs.activity, Mapping):
        return _probe("flow", UNKNOWN, "no_activity")
    skip, out = set(excluded), {}
    for pid in points():
        row = inputs.activity.get(pid)
        if pid in skip:
            out[pid] = _p(NOT_APPLICABLE, "excluded")
        elif not isinstance(row, Mapping) or not all(isinstance(row.get(k), int) and not isinstance(row.get(k), bool)
                                                       and row.get(k) >= 0 for k in ("eligible", "receipts")):
            out[pid] = _p(UNKNOWN, "no_activity")
        elif row["eligible"] == 0 or (isinstance(row.get("eligible_age_s"), (int, float))
                                       and row["eligible_age_s"] > inputs.window_seconds):
            out[pid] = _p(NOT_APPLICABLE, "no_eligible_workload")
        elif row["receipts"] == 0:
            out[pid] = _p(UNHEALTHY, "no_receipts_flowing")
        else:
            out[pid] = _p(HEALTHY)
    worst = max((v["status"] for v in out.values()), key=_RANK.get)
    return _probe("flow", worst, "", out)


def _smoke_question(point) -> Dict[str, Any]:
    kind = point.types[0]
    if kind == "choice":
        return {"type": "choice", "instructions": "Which option does the synthetic health note name?",
                "criteria": {"a": "the first option", "b": "the second option"}}
    if kind == "score":
        return {"type": "score", "instructions": "How severe is the synthetic health note?",
                "criteria": ["none", "low", "medium", "high", "severe"]}
    return {"type": "noul", "instructions": "The synthetic health note says the check passed."}


def probe_jev_smoke(inputs: HealthInputs) -> Dict[str, Any]:
    """On demand only: ONE batched Jev request covering every registered point. No LLM.

    Synthetic text, no workload data. Never part of the periodic script. Reports measured
    tokens (``input_tokens``/``output_tokens`` once per batch) as a measurement, not a promise.
    A point is ``usable`` if its answer parsed against its own question; it is informational and
    never marks a point unhealthy (an outage already falls back per request).
    """
    if not inputs.smoke:
        return _probe("jev", NOT_APPLICABLE, "not_requested")
    from .decisions import Evaluator  # local: the periodic path never builds an evaluator
    ids = points()
    calls = []
    ask = inputs.smoke_ask or client.ask

    def counted(*a, **k):
        calls.append(1)
        return ask(*a, **k)
    evaluator = Evaluator(ask=counted, deadline=10.0, budget=30.0)
    batch = [{"point": pid, "question": _smoke_question(REGISTRY[pid]),
              "candidate_source": REGISTRY[pid].candidate_source} for pid in ids]
    try:
        out = evaluator.evaluate("Synthetic health check. The check passed.", batch, admission=lambda: True)
    except Exception as error:  # noqa: BLE001
        return _probe("jev", UNHEALTHY, "smoke_error:" + type(error).__name__, requests=len(calls))
    usable = {r["point_id"]: r["evaluation"] == "jev" for r in out}
    reason = {r["point_id"]: r["reason"] for r in out}
    head = out[0] if out else {}
    ok = bool(out) and all(usable.values())
    return _probe("jev", HEALTHY if ok else UNHEALTHY, "" if ok else "smoke_unusable",
                  requests=len(calls), batch_size=len(batch), input_tokens=head.get("input_tokens"),
                  output_tokens=head.get("output_tokens"), latency_ms=head.get("latency_ms"),
                  usable=usable, failures={k: v for k, v in reason.items() if not usable[k]})


# ---------------------------------------------------------------- roll-up

def assess(inputs: HealthInputs) -> Dict[str, Any]:
    """Run every probe and roll up. Pure: same inputs, same report."""
    config = probe_config(inputs)
    probes = [probe_registration(inputs), probe_hashes(inputs), config, probe_plugin(inputs),
              probe_patch(inputs), probe_smoke(inputs),
              probe_flow(inputs, config.get("enabled"), config.get("excluded", [])), probe_jev_smoke(inputs)]
    rolled = {}
    for pid in points():
        status, reasons = HEALTHY, []
        for probe in probes:
            gate = probe["gating"]
            # A probe that names points is scoped to them; one that names none speaks for all.
            seen = (probe["points"].get(pid) or _p(HEALTHY)) if probe["points"] else _p(probe["status"], probe["reason"])
            if gate == "info" or seen["status"] == NOT_APPLICABLE:
                continue
            if seen["status"] == UNHEALTHY:
                reasons.append(f"{probe['name']}:{seen['reason']}")
                status = UNHEALTHY
            elif gate == "full" and seen["status"] != HEALTHY:
                reasons.append(f"{probe['name']}:{seen['reason'] or UNKNOWN}")
                status = status if status == UNHEALTHY else UNKNOWN
        rolled[pid] = {"status": status, "reasons": reasons}
    unhealthy = sorted(p for p, v in rolled.items() if v["status"] == UNHEALTHY)
    unknown = sorted(p for p, v in rolled.items() if v["status"] == UNKNOWN)
    enabled = config.get("enabled")
    if enabled is False and config["status"] == HEALTHY:
        overall = OFF
    else:
        overall = UNHEALTHY if unhealthy or config["status"] == UNHEALTHY else (UNKNOWN if unknown else HEALTHY)
    by = lambda name: next(p for p in probes if p["name"] == name)  # noqa: E731
    reach = by("registration")["points"]
    smoke = by("smoke")["points"]
    verified = [p for p in points() if reach.get(p, {}).get("status") == HEALTHY
                and smoke.get(p, {}).get("status") == HEALTHY]
    counts = {s: sum(1 for v in rolled.values() if v["status"] == s) for s in (HEALTHY, UNHEALTHY, UNKNOWN)}
    report = {"schema": REPORT_SCHEMA, "status": overall, "enabled": enabled,
              "unhealthy_points": unhealthy, "unknown_points": unknown, "counts": counts,
              "reachability": {"verified_points": verified,
                               "full": len(verified) == len(points()),
                               "basis": "registration+import+signature+smoke; registry names alone prove nothing"},
              "points": rolled, "probes": probes}
    report["summary"] = summarize(report)
    return report


def summarize(report: Mapping) -> str:
    lines = [f"jev decision health: {report['status']}"
             f" (enabled={report['enabled']}; {report['counts'][HEALTHY]} healthy,"
             f" {report['counts'][UNHEALTHY]} unhealthy, {report['counts'][UNKNOWN]} unknown of {len(points())})"]
    for probe in report["probes"]:
        lines.append(f"  {probe['name']:<13}{probe['status']}" + (f"  {probe['reason']}" if probe["reason"] else ""))
    for pid in report["unhealthy_points"]:
        lines.append(f"  UNHEALTHY {pid}: " + "; ".join(report["points"][pid]["reasons"]))
    lines.append(f"  full reachability: {'yes' if report['reachability']['full'] else 'not verified'}"
                 f" ({len(report['reachability']['verified_points'])}/{len(points())} points)")
    return "\n".join(lines)


# ---------------------------------------------------------------- auto-fallback wrapper

class HealthGuard:
    """Evaluator wrapper that sends unhealthy points straight to the original branch."""

    _KEYS = frozenset({"point", "question", "candidate_source"})

    def __init__(self, inner, unhealthy: Callable[[], Iterable[str]]):
        if inner is None or not callable(unhealthy) or not hasattr(inner, "evaluate"):
            raise ValueError("guard_requires_evaluator_and_callable")
        self.inner, self.unhealthy = inner, unhealthy

    @classmethod
    def wrap(cls, inner, unhealthy: Callable[[], Iterable[str]]):
        """``None`` in (decisions off), ``None`` out: no guard exists while off."""
        return None if inner is None else cls(inner, unhealthy)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def verdict(self, point: str) -> Dict[str, Any]:
        """What a generic hook needs per point: ``usable`` False plus the original branch, or True.

        Health only reports; it never executes, retries or authorizes an action. The parent's
        hook takes the literal original branch when ``usable`` is False.
        """
        down = self._down()
        bad = down is None or point in down
        return {"usable": not bad, "fallback": "original" if bad else "none",
                "reason": REASON if bad else None}

    def _down(self):
        try:
            return frozenset(self.unhealthy())
        except Exception:  # noqa: BLE001 - cannot tell: fail closed to the original branch
            return None

    def evaluate(self, state: Any, requests: Sequence[Mapping], *, admission: Callable[[], bool]) -> list:
        well_formed = (isinstance(requests, (list, tuple)) and 1 <= len(requests) <= 32
                       and all(isinstance(r, Mapping) and set(r) == self._KEYS and isinstance(r["point"], str)
                               and r["point"] in REGISTRY for r in requests))
        if not well_formed:
            return self.inner.evaluate(state, requests, admission=admission)
        down = self._down()
        failed = [down is None or r["point"] in down for r in requests]
        if not any(failed):
            return self.inner.evaluate(state, requests, admission=admission)
        live = [r for r, bad in zip(requests, failed) if not bad]
        done = iter(self.inner.evaluate(state, live, admission=admission) if live else ())
        return [self._receipt(r) if bad else next(done) for r, bad in zip(requests, failed)]

    @staticmethod
    def _receipt(request: Mapping) -> Dict[str, Any]:
        return {"schema": "decision_receipt.v1", "point_id": request["point"],
                "candidate_source": request["candidate_source"], "evaluation": REASON,
                "adequate": False, "recommendation": None, "applied": None,
                "fallback": REGISTRY[request["point"]].fallback, "reason": REASON,
                "reading": None, "margin": None, "latency_ms": 0, "confidence": None,
                "probabilities": None, "input_tokens": None, "output_tokens": None, "batch_size": None}


# ---------------------------------------------------------------- transitions, one state file per home

PATCH_STATES = ("APPLIED", "MISSING", "CONFLICT", "FAILED", "UNKNOWN", "RETIRED", "PENDING")
_PATCH_BAD = frozenset({"MISSING", "CONFLICT", "FAILED", "UNKNOWN"})
_SEVERITY = ("RETIRED", "APPLIED", "PENDING", "UNKNOWN", "MISSING", "FAILED", "CONFLICT")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_PATCH_STATES = 256
_REASON_RE = re.compile(r"[a-z0-9_]{1,40}")


def safe_id(value: Any) -> str:
    """An identifier safe to persist and send: a plain token, else an opaque hash (never the text)."""
    if isinstance(value, str) and _ID_RE.match(value):
        return value
    raw = value if isinstance(value, str) else type(value).__name__
    return "id-" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:12]


def sanitize_patch_states(raw: Any) -> Dict[str, str]:
    """Exact ID -> enum map, sorted. Anything else is UNKNOWN, never carried through as text."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        return {"patch-states-invalid": "UNKNOWN"}
    out: Dict[str, str] = {}
    for key, value in raw.items():
        sid = safe_id(key)
        state = value if isinstance(value, str) and value in PATCH_STATES else "UNKNOWN"
        if sid not in out or _SEVERITY.index(state) > _SEVERITY.index(out[sid]):
            out[sid] = state
    ordered = sorted(out.items())
    if len(ordered) > MAX_PATCH_STATES:
        ordered = ordered[:MAX_PATCH_STATES - 1] + [("patch-states-truncated", "UNKNOWN")]
    return dict(sorted(dict(ordered).items()))


def effective(status: Any, states: Mapping[str, str], pending_blocks: bool = False):
    """Overall status and the consequential (alert-relevant) patch entries.

    UNKNOWN/FAILED/MISSING/CONFLICT are unhealthy. RETIRED/APPLIED change nothing. PENDING never
    claims healthy (healthy becomes unknown) and is unhealthy only when ``pending_blocks``.
    """
    status = status if status in (HEALTHY, UNHEALTHY, UNKNOWN, OFF, NOT_APPLICABLE) else UNKNOWN
    bad = {k: v for k, v in states.items() if v in _PATCH_BAD or (pending_blocks and v == "PENDING")}
    if status == UNHEALTHY or bad:
        return UNHEALTHY, bad
    if status == HEALTHY and any(v == "PENDING" for v in states.values()):
        return UNKNOWN, bad
    return status, bad


class LockTimeout(Exception):
    """The ledger lock was not acquired in time; nothing was read, delivered or written."""


class _FileLock:
    """Portable stdlib cross-process lock with a bounded wait (flock, msvcrt, or O_EXCL fallback)."""

    def __init__(self, path: Path, timeout: float):
        self.path, self.timeout, self.fd, self.excl = Path(path), float(timeout), None, None

    def _try(self) -> bool:
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif msvcrt is not None:
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                self.excl = str(self.path) + ".x"
                os.close(os.open(self.excl, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            try:  # a fallback lock older than any sane hold is a crashed holder's
                if time.time() - os.path.getmtime(self.excl) > max(60.0, self.timeout * 2):
                    os.unlink(self.excl)
            except OSError:
                pass
            return False
        except OSError:
            return False

    def __enter__(self):
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.timeout
        while not self._try():
            if time.monotonic() >= deadline:
                os.close(self.fd)
                raise LockTimeout(str(self.timeout))
            time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        try:
            if fcntl is not None:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            elif msvcrt is not None:
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            elif self.excl:
                os.unlink(self.excl)
        finally:
            os.close(self.fd)


class HealthLedger:
    """Status-change dedup with a local state file and receipt log under one Hermes home.

    ``<home>/<subdir>/state.json`` holds the last status, unhealthy set, canonical patch states
    and last delivered alert signature; ``events.jsonl`` (the dashboard source) gets a line only
    on a change or an owed alert. Both are mode 0600 and hold only fixed-vocabulary fields:
    registry/patch identifiers and enums, never raw stderr, paths, secrets or free text.

    Serialization: ``observe`` holds one cross-process lock (``.lock``, bounded wait) across
    read, compare, deliver and write, so two processes cannot both deliver one transition. A
    lock timeout delivers and writes nothing (the next run retries).

    Delivery is at-least-once with a stable idempotency key. Intent (``pending`` + key) is
    persisted BEFORE the callback runs and cleared after it succeeds; a crash or failed callback
    leaves it pending and the retry reuses the SAME key. Exactly-once external delivery cannot be
    proven from here, so the host adapter must dedup real sends by ``event["idempotency_key"]``.
    A callback fails by raising or returning ``False``; failure is never recorded as delivered.

    A repeat of the same status, unhealthy set and patch map changes nothing but a counter.
    An alert is owed when the alert signature (unhealthy points + consequential patch entries)
    differs from the last delivered one, or on recovery after a delivered one.
    """

    def __init__(self, home, subdir: str = "jev-health", lock_timeout: float = 30.0):
        self.dir = Path(home) / subdir
        self.state_path, self.events_path = self.dir / "state.json", self.dir / "events.jsonl"
        self.lock_path, self.lock_timeout = self.dir / ".lock", lock_timeout

    def read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def unhealthy_points(self) -> frozenset:
        return frozenset(x for x in self.read().get("unhealthy_points", ()) if x in REGISTRY)

    @staticmethod
    def _signature(status: str, points: Sequence[str], bad: Mapping[str, str], report_status: Any) -> Optional[str]:
        if status != UNHEALTHY:
            return None
        listed = list(points) if report_status == UNHEALTHY else []
        return json.dumps(listed) if not bad else json.dumps({"points": listed, "patches": dict(sorted(bad.items()))},
                                                              sort_keys=True)

    def observe(self, report: Mapping, *, now: float, alert: Callable[[Dict[str, Any]], Any] = None,
                target: str = None, sinks: Sequence[Callable[[Dict[str, Any]], Any]] = (),
                pending_blocks: bool = False, lock_timeout: float = None) -> Dict[str, Any]:
        """Record one report. ``alert`` is the host adapter (None: dashboard + log only).

        ``report`` needs ``status`` and ``unhealthy_points``; optional ``patch_states`` is an
        ID -> enum map (see ``PATCH_STATES``), validated and canonicalised here.
        """
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = _FileLock(self.lock_path, self.lock_timeout if lock_timeout is None else lock_timeout)
            with lock:
                return self._observe_locked(report, now, alert, target, sinks, pending_blocks)
        except LockTimeout:
            return {"changed": False, "previous": None, "current": None, "alert": None, "delivered": None,
                    "suppressed": 0, "key": None, "error": "lock_timeout"}

    def _observe_locked(self, report, now, alert, target, sinks, pending_blocks) -> Dict[str, Any]:
        prior = self.read()
        states = sanitize_patch_states(report.get("patch_states"))
        status, bad = effective(report.get("status"), states, pending_blocks)
        raw_points = report.get("unhealthy_points")
        points = sorted({safe_id(x) for x in raw_points}) if isinstance(raw_points, (list, tuple, set, frozenset)) else []
        sig = self._signature(status, points, bad, report.get("status"))
        current = {"status": status, "unhealthy_points": points, "patch_states": states}
        changed = any(prior.get(k, {} if k == "patch_states" else None) != current[k]
                      for k in ("status", "unhealthy_points", "patch_states"))
        delivered = prior.get("alerted_signature")
        owed = "unhealthy" if sig and sig != delivered else ("recovered" if not sig and delivered else None)
        result = {"changed": changed, "previous": prior.get("status"), "current": status, "alert": owed,
                  "delivered": None, "suppressed": 0, "key": None}
        state = {**current, "since": now if changed else prior.get("since", now), "last_seen": now,
                 "suppressed": 0 if changed else int(prior.get("suppressed", 0)) + 1,
                 "alerted_signature": delivered, "seq": int(prior.get("seq") or 0),
                 "ledger_id": prior.get("ledger_id") or uuid.uuid4().hex, "pending": None}
        event = None
        if owed:
            old = prior.get("pending") if isinstance(prior.get("pending"), dict) else {}
            if old.get("kind") == owed and old.get("signature") == sig and isinstance(old.get("key"), str):
                key = old["key"]  # a retry of the same alert instance: same key, host dedups
            else:
                state["seq"] += 1
                key = hashlib.sha256(f"{state['ledger_id']}|{state['seq']}|{owed}|{sig}".encode()).hexdigest()[:32]
            state["pending"] = {"key": key, "kind": owed, "signature": sig}
            result["key"] = key
            event = {"schema": "jev-decision-health-event/1", "ts": now, "kind": owed, "target": target,
                     "key": key, "idempotency_key": key, "status": status, "unhealthy_points": points,
                     "patch_states": dict(sorted(bad.items())),
                     "reasons": {p: self._reason_tokens(report, p) for p in points},
                     "summary": self._machine_summary(owed, status, points, bad)}
            self._write(state)  # intent first: a crash after delivery retries with the same key
            ok = self._deliver(event, alert, sinks)
            result["delivered"] = ok
            if ok:
                state["alerted_signature"], state["pending"] = sig, None
        result["suppressed"] = state["suppressed"] if not changed else 0
        if changed or owed:
            self._append({"ts": now, "changed": changed, "previous": prior.get("status"), "status": status,
                          "unhealthy_points": points, "patch_states": states, "alert": owed,
                          "delivered": result["delivered"], "key": result["key"]})
        self._write(state)
        return result

    @staticmethod
    def _reason_tokens(report: Mapping, point: str) -> list:
        """Category tokens only: ``hashes:drift:some/path`` becomes ``drift``. No path or message leaves."""
        try:
            rows = report["points"][point]["reasons"]
            parts = (r.split(":") for r in rows if isinstance(r, str))
            return sorted({t for p in parts for t in [p[1] if len(p) > 1 else p[0]] if _REASON_RE.fullmatch(t)})
        except (KeyError, TypeError, AttributeError, IndexError):
            return []

    @staticmethod
    def _machine_summary(kind: str, status: str, points: Sequence[str], bad: Mapping[str, str]) -> str:
        return (f"jev-health {kind} status={status} points={','.join(points) or '-'}"
                f" patches={','.join(f'{k}:{v}' for k, v in sorted(bad.items())) or '-'}")

    @staticmethod
    def _deliver(event: Dict[str, Any], alert: Optional[Callable], sinks: Sequence[Callable]) -> bool:
        (log.warning if event["kind"] == "unhealthy" else log.info)("%s", event["summary"])
        for sink in sinks:
            try:
                sink(dict(event))
            except Exception:  # noqa: BLE001 - a broken extra sink never blocks the ledger
                log.exception("jev health sink failed")
        if alert is None:
            return True
        try:
            return alert(dict(event)) is not False
        except Exception:  # noqa: BLE001 - stays owed; retried next observation with the same key
            log.exception("jev health alert adapter failed")
            return False

    def _append(self, row: Mapping) -> None:
        fd = os.open(self.events_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as handle:
            os.fchmod(handle.fileno(), 0o600) if hasattr(os, "fchmod") else None
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    def _write(self, state: Mapping) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-")  # created 0600
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            finally:
                raise


# ---------------------------------------------------------------- offline selftest

def selftest() -> Dict[str, Any]:
    """Synthetic, offline: fixture hash math, roll-up counts and transition duplicate suppression."""
    failures: List[str] = []
    was, log.disabled = log.disabled, True  # the synthetic alerts are not real

    def check(name: str, ok: bool) -> None:
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        entries = []
        for i in range(5):
            data = f"fixture-{i}".encode()
            (root / f"f{i}.py").write_bytes(data)
            entries.append({"path": f"f{i}.py", "installed_sha256": hashlib.sha256(data).hexdigest()})
        (root / "f1.py").write_bytes(b"tampered")
        (root / "f3.py").unlink()
        got = probe_hashes(HealthInputs(manifest={"core_files": entries}, roots={"core": root}))
        check("hash_drift_math", got["status"] == UNHEALTHY and got["checked"] == 5
              and got["drift"] == ["f1.py"] and got["missing"] == ["f3.py"])
        check("no_manifest_unknown", probe_hashes(HealthInputs())["status"] == UNKNOWN)
        empty = assess(HealthInputs())
        check("nothing_supplied_never_healthy", empty["status"] == UNHEALTHY
              and empty["unhealthy_points"] == sorted(points())
              and empty["counts"] == {HEALTHY: 0, UNHEALTHY: len(points()), UNKNOWN: 0}
              and empty["reachability"]["full"] is False and HealthInputs().smoke is False)
        led = HealthLedger(tmp)
        def rep(status, pts=()):
            return {"status": status, "unhealthy_points": list(pts), "summary": status,
                    "points": {p: {"reasons": ["x"]} for p in pts}}
        seq = [(rep(HEALTHY), None), (rep(UNHEALTHY, ["a"]), "unhealthy"), (rep(UNHEALTHY, ["a"]), None),
               (rep(UNHEALTHY, ["a", "b"]), "unhealthy"), (rep(HEALTHY), "recovered"), (rep(HEALTHY), None)]
        sent: List[str] = []
        owed = [led.observe(r, now=float(i), alert=lambda e: sent.append(e["kind"]))["alert"]
                for i, (r, _) in enumerate(seq)]
        check("transition_dedup", owed == [w for _, w in seq] and sent == ["unhealthy", "unhealthy", "recovered"])
        check("repeat_counts_only", led.read()["suppressed"] == 1)
    log.disabled = was
    return {"passed": not failures, "failed": failures}
