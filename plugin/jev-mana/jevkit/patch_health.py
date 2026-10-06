"""Thin adapter: patch-owner states into the one decision-health ledger and its one alert callback.

The patch owner keeps its own deterministic parser (e.g. the bot_config_scan result). It hands
this module an ``{id: ENUM}`` map; everything else (validation, canonical ordering, dedup,
the cross-process lock, the single alert callback, 0600 files) is the shared ``HealthLedger``.
There is deliberately no second sender and no independent alert route here.

No network, no model. ``home`` is the caller's EXISTING Hermes home, so no profile leaks.
Delivery is at-least-once with a stable ``idempotency_key``; the host callback must dedup real
external sends by that key.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from .decision_health import (HEALTHY, PATCH_STATES, REPORT_SCHEMA, UNKNOWN, HealthLedger, effective,
                              sanitize_patch_states)

__all__ = ["PATCH_STATES", "patch_health_report", "merge_patch_states", "observe_patch_states"]


def merge_patch_states(report: Mapping, patch_states: Any, *, pending_blocks: bool = False) -> Dict[str, Any]:
    """A copy of a full health report with canonical ``patch_states`` and the effective status."""
    states = sanitize_patch_states(patch_states)
    status, bad = effective(report.get("status"), states, pending_blocks)
    merged = {**report, "patch_states": states, "status": status}
    merged["patch"] = {"consequential": dict(sorted(bad.items())),
                       "pending": sorted(k for k, v in states.items() if v == "PENDING")}
    return merged


def patch_health_report(patch_states: Any, *, pending_blocks: bool = False) -> Dict[str, Any]:
    """A canonical, patch-only health report: unhealthy if any state is consequential, healthy only
    when at least one state is known and all are APPLIED/RETIRED, otherwise unknown."""
    states = sanitize_patch_states(patch_states)
    base_status = HEALTHY if states else UNKNOWN
    base = {"schema": REPORT_SCHEMA, "status": base_status, "enabled": None, "unhealthy_points": [],
            "unknown_points": [], "points": {}, "probes": [], "counts": {}}
    return merge_patch_states(base, states, pending_blocks=pending_blocks)


def observe_patch_states(home, patch_states: Any, *, now: float, alert: Optional[Callable[[Dict[str, Any]], Any]] = None,
                         target: str = None, sinks: Sequence[Callable] = (), base_report: Mapping = None,
                         pending_blocks: bool = False, lock_timeout: float = 30.0,
                         subdir: str = "jev-health") -> Dict[str, Any]:
    """Record patch states (alone, or merged into ``base_report``) in the caller's home ledger.

    Returns the ledger result: ``changed``, ``previous``, ``current``, ``alert`` (None, "unhealthy"
    or "recovered"), ``delivered``, ``key`` (the idempotency key), ``suppressed`` and, on a lock
    timeout, ``error == "lock_timeout"`` with nothing delivered. ``alert`` fails by raising or
    returning False; failure keeps the alert owed and the retry carries the same key.
    """
    report = (merge_patch_states(base_report, patch_states, pending_blocks=pending_blocks) if base_report is not None
              else patch_health_report(patch_states, pending_blocks=pending_blocks))
    return HealthLedger(home, subdir, lock_timeout).observe(report, now=now, alert=alert, target=target, sinks=sinks,
                                                           pending_blocks=pending_blocks)
