"""Offline per-point off/on smoke for ``jevkit.decision_health.probe_smoke``.

Synthetic text only, a fake evaluator transport, no network, no Jev, no model. For every registered point:

* ``off``: with default (disabled) settings the core seam reports the point inactive, so no hook, journal,
  receipt or network exists on that path (counters are 0 and ``identical`` is True);
* ``on``: the plugin's wording builds a valid typed question for that point, the evaluator reads a canned
  answer, and the reading is a ``decision_receipt.v1`` that either applies (``fallback none``) or falls back
  to the original branch.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping

try:
    from .jevkit import decisions
    from . import decision_points
except ImportError:  # standalone script: plugin dir on sys.path
    from jevkit import decisions  # type: ignore
    import decision_points  # type: ignore

_OPTIONS = ["First synthetic option.", "Second synthetic option."]


def descriptor(pid: str) -> Dict[str, Any]:
    point = decisions.REGISTRY[pid]
    base = {"point": pid, "candidate_source": point.candidate_source, "step": "health.synthetic",
            "options": list(_OPTIONS)}
    if pid == "ask.decision_gate":
        return {**base, "kind": "noul", "for": "tool.plan"}
    if pid == "context.pick":
        return {**base, "kind": "noul", "options": ["Keep this synthetic record."]}
    return {**base, "kind": "choice"}


def _fake_ask(state: Any, questions: Mapping[str, Mapping], *, timeout: float = 1.0, retries: int = 0, **_: Any):
    answers = {}
    for name, q in questions.items():
        if q["type"] == "noul":
            answers[name] = {"type": "noul", "noul": 0.95}
        elif q["type"] == "choice":
            keys = list(q["criteria"])
            mass = {k: (0.9 if i == 0 else 0.1 / (len(keys) - 1)) for i, k in enumerate(keys)}
            answers[name] = {"type": "choice", "choice": keys[0], "probabilities": mass, "confidence": 0.9}
        else:  # score is not used by the synthetic descriptors
            raise ValueError("unexpected_question_type")
    return {"answers": answers, "usage": {"output_tokens": 1}, "input_tokens": 1, "jev_model": "synthetic"}


def _on(pid: str) -> Callable[[], List[Dict[str, Any]]]:
    def run():
        desc = descriptor(pid)
        state = {"r0": {"note": "synthetic health state"}}
        typed = decision_points.requests([desc], state)
        evaluator = decisions.Evaluator(ask=_fake_ask, deadline=1.0, budget=5.0)
        return [decisions.receipt(r) for r in evaluator.evaluate(state, typed, admission=lambda: True)]
    return run


def _off(pid: str) -> Callable[[], Dict[str, Any]]:
    def run():
        from agent import decision_config, decision_seams  # the core seam: raises when it is not installed
        inactive = decision_seams.active(None, pid, decision_config.Settings()) is None
        return {"evaluations": 0, "network": 0, "receipts": 0, "identical": inactive}
    return run


def callbacks() -> Dict[str, Dict[str, Callable]]:
    return {pid: {"off": _off(pid), "on": _on(pid)} for pid in decisions.REGISTRY}
