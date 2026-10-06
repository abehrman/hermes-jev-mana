"""``select_agent_step`` evaluator hook for the jev-mana plugin.

Standalone: the parent plugin registers it, nothing here changes tools or schemas::

    from .decision_hooks import register_decision_hooks
    register_decision_hooks(ctx, log=_log)     # inside register(ctx), only when jev.decisions is on

The Hermes core consumer (``agent/decision_native.py``) calls ``select_agent_step`` once per native
step with a batch of typed requests. This hook only reads them with ``jevkit.decisions.Evaluator``
and returns the typed readings. It never executes, authorizes, drafts or contacts a model lane;
the host validates and applies. Remote evaluation happens only for a request the host labels with
an admitted source AND whose own ``admission()`` is True at the send boundary. Any other source
label gets a local, non-adequate reading and no network call.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

try:
    from .jevkit import decisions
except ImportError:  # running from the portable repository layout
    from jevkit import decisions  # type: ignore
try:
    from .jevkit import decision_health
except ImportError:
    from jevkit import decision_health  # type: ignore
try:
    from . import decision_points
except ImportError:  # imported as a top-level module in tests
    import decision_points  # type: ignore

HOOK = "select_agent_step"
RECEIPT_HOOK = "agent_decision_receipt"
ADMITTED_SOURCES = frozenset({"hermes.core.native"})
_MAX_TURNS = 64

_LOCK = threading.Lock()
_EVALUATORS: "OrderedDict[tuple, Any]" = OrderedDict()
_ASK: Optional[Callable] = None   # test seam: replaces the network client


def _local(requests, reason: str) -> List[Dict[str, Any]]:
    out = []
    for item in requests:
        point = decisions.REGISTRY.get(item.get("point")) if isinstance(item, dict) else None
        out.append({"schema": "decision_receipt.v1", "point_id": item.get("point") if isinstance(item, dict) else None,
                    "candidate_source": item.get("candidate_source") if isinstance(item, dict) else None,
                    "evaluation": reason, "adequate": False, "recommendation": None, "applied": None,
                    "fallback": "original", "reason": reason, "reading": None, "margin": None,
                    "latency_ms": 0, "fixed_fallback": point.fallback if point else None})
    return out


def _ledger():
    from hermes_constants import get_hermes_home
    return decision_health.HealthLedger(get_hermes_home())


def _evaluator(session_id, turn_id, budget, deadline, margin):
    key = (session_id, turn_id)
    with _LOCK:
        evaluator = _EVALUATORS.get(key)
        if evaluator is None:
            evaluator = decisions.Evaluator(ask=_ASK, budget=budget, deadline=deadline, margin=margin)
            # Unhealthy points answer health_failed (original branch) without scan, send or charge.
            evaluator = decision_health.HealthGuard.wrap(evaluator, _ledger().unhealthy_points)
            _EVALUATORS[key] = evaluator
            while len(_EVALUATORS) > _MAX_TURNS:
                _EVALUATORS.popitem(last=False)
        return evaluator


def on_select_agent_step(*, requests=None, state=None, admission=None, source=None, session_id="",
                         turn_id="", budget=5.0, deadline=1.0, margin=None, **_ignored):
    """Return one reading per request, in order; ``None`` when the batch is malformed."""
    if not isinstance(requests, (list, tuple)) or not requests:
        return None
    if source not in ADMITTED_SOURCES or not callable(admission) or not session_id or not turn_id:
        return _local(requests, "local_privacy_skip")      # unknown source stays local
    try:
        # Core sends generic descriptors; the wording for each point lives here.
        typed = (list(requests) if all(isinstance(r, dict) and "question" in r for r in requests)
                 else decision_points.requests(list(requests), state))
        evaluator = _evaluator(session_id, turn_id, budget, deadline, margin)
        return evaluator.evaluate(state, typed, admission=admission)
    except (ValueError, TypeError, KeyError):
        return None                                        # host treats this as no usable reading


def _configured_lanes() -> list:
    """Operator lanes from ``lanes:`` in the profile's jev_config.yaml, else the global file's. Each entry is
    ``{provider, model, description}``; the host drops any entry whose provider is not the configured one."""
    from agent.decision_config import _read_tuning
    from hermes_cli.profiles import _get_default_hermes_home
    from hermes_constants import get_hermes_home
    root, home = _get_default_hermes_home(), get_hermes_home()
    for base in ((home, root) if home != root else (root,)):
        value = _read_tuning(base / "jev_config.yaml").get("lanes")
        if value is not None:
            return value if isinstance(value, list) else []
    return []


def on_decision_candidates(*, point=None, context=None, **_ignored):
    """route.session: the lanes the operator listed under ``lanes:`` in jev_config.yaml, as public
    descriptions. Nothing is invented: no lanes configured, no proposals (the host then records a
    ``prerequisite_missing`` receipt and keeps the original lane)."""
    if point != "route.session":
        return None
    try:
        lanes = _configured_lanes()
    except Exception:
        return None
    out = []
    for lane in lanes[:16]:
        if (isinstance(lane, dict) and all(isinstance(lane.get(k), str) and lane[k].strip()
                                           for k in ("provider", "model", "description"))):
            out.append({k: lane[k] for k in ("provider", "model", "description")})
    return out


def register_decision_hooks(ctx, log: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
    """Register the evaluator hook and, optionally, a public-receipt logger."""
    ctx.register_hook(HOOK, on_select_agent_step)
    ctx.register_hook("decision_candidates", on_decision_candidates)
    try:
        from . import decision_health_runtime
    except ImportError:
        import decision_health_runtime  # type: ignore
    ctx.register_hook("decision_health_check", decision_health_runtime.on_health_check)
    if log is not None:
        def on_receipt(*, receipt=None, **_ignored):
            if isinstance(receipt, dict):
                log({"decision": receipt})
        ctx.register_hook(RECEIPT_HOOK, on_receipt)
