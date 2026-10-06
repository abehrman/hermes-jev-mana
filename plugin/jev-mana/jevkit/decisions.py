"""Typed decision-point evaluator. Never executes, authorizes, or drafts candidates.

Host consumers own source admission, exact bindings, durable consumption and
fallback. Only admitted public state reaches the existing privacy/client boundary.
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Sequence

from . import client, privacy


@dataclass(frozen=True)
class Point:
    id: str
    types: tuple
    candidate_source: str
    fallback: str = "original"
    margin: float = 0.15
    extra_sources: tuple = ()  # other honest labels this point may carry, e.g. a drafted plan

    @property
    def sources(self) -> tuple:
        return (self.candidate_source,) + tuple(self.extra_sources)


REGISTRY = {
    p.id: p for p in (
        Point("ask.closed", ("choice", "noul", "score"), "typed_user_options", "original",
              extra_sources=("code_enum",)),   # the code-enumerated Yes/No domain
        Point("ask.decision_gate", ("noul",), "code_enum", "original"),
        Point("route.session", ("choice",), "code_enum", "original"),
        Point("route.effort", ("choice",), "code_enum", "original"),
        Point("skill.pick", ("choice", "noul"), "source_records", "original"),
        Point("context.pick", ("noul", "score"), "source_records", "original"),
        Point("tool.plan", ("choice",), "code_enum", "original", extra_sources=("bounded_llm_draft",)),
        Point("search.pick", ("choice", "noul"), "source_records", "original"),
        Point("gui.step", ("choice",), "code_enum", "original"),
        Point("approval.triage", ("choice", "noul"), "code_enum", "original"),
        Point("code.approach", ("choice", "score"), "code_enum", "original", extra_sources=("bounded_llm_draft",)),
        Point("work.executor", ("choice",), "code_enum", "original"),
    )
}


MARGIN = 0.15
NOUL_LOW, NOUL_HIGH = 0.2, 0.8
_EPS = 1e-9
_SOURCES = ("code_enum", "source_records", "typed_user_options", "bounded_llm_draft")


def _number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _unit(value: Any) -> float:
    if not _number(value) or not -_EPS <= value <= 1 + _EPS:
        raise ValueError("not_unit")
    return min(1.0, max(0.0, float(value)))


def _optional_unit(value: Any):
    return None if value is None else _unit(value)


def _count(value: Any):
    return int(value) if _number(value) and value >= 0 and value == int(value) else None


def _mass(raw: Any, keys: Sequence[str], *, sparse: bool) -> Dict[str, float]:
    """A finite probability mass over ``keys`` summing to one. Sparse leaves absent keys at 0."""
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("no_distribution")
    found = set(raw)
    if not (found <= set(keys) if sparse else found == set(keys)):
        raise ValueError("distribution_keys")
    mass = {key: _unit(raw[key]) for key in keys if key in raw}
    if abs(sum(mass.values()) - 1.0) > 0.01 + 1e-12:
        raise ValueError("distribution_mass")
    return {key: mass.get(key, 0.0) for key in keys}


def _margin(values: Sequence[float]) -> float:
    ranked = sorted(values, reverse=True)
    return ranked[0] - (ranked[1] if len(ranked) > 1 else 0.0)


def adequate(point: Point, question: Mapping, answer: Mapping, margin: float = None) -> Dict:
    """Fixed uncertainty rule, not comparison/calibration against an LLM.

    Reads the answer strictly against the question asked. Malformed or contradictory
    answers raise ValueError; the evaluator turns that into an invalid, non-adequate batch.
    Choice value: option id. Score value: the modal rubric level (an int), and the score
    must round to it. Noul value: bool. Missing score levels count as zero mass.
    """
    kind = question["type"]
    floor = point.margin if margin is None else margin
    if not isinstance(answer, Mapping) or answer.get("type") != kind:
        raise ValueError("answer_type")
    if kind == "noul":
        value = _unit(answer.get("noul"))
        usable = value <= NOUL_LOW + _EPS or value >= NOUL_HIGH - _EPS
        return {"adequate": usable, "value": value >= NOUL_HIGH - _EPS,
                "margin": abs(2 * value - 1), "reading": {"type": "noul", "noul": value},
                "numbers": {"confidence": None, "probabilities": [value]}}
    if kind == "choice":
        options = list(question["criteria"])
        mass = _mass(answer.get("probabilities"), options, sparse=False)
        picked = answer.get("choice")
        if not isinstance(picked, str) or picked not in mass:
            raise ValueError("choice_not_offered")
        if mass[picked] < max(mass.values()) - _EPS:
            raise ValueError("choice_not_argmax")
        margin = _margin(list(mass.values()))
        return {"adequate": margin >= floor - _EPS, "value": picked, "margin": margin,
                "reading": {"type": "choice", "choice": picked, "probabilities": mass},
                "numbers": {"confidence": _optional_unit(answer.get("confidence")),
                            "probabilities": [mass[o] for o in options]}}
    levels = [str(i) for i in range(len(question["criteria"]))]
    raw = answer.get("probabilities")
    score = answer.get("score")
    if not _number(score) or not -0.5 <= score <= len(levels) - 0.5:
        raise ValueError("score_off_rubric")
    if not raw:
        # A score without its spread cannot be judged for uncertainty.
        return {"adequate": False, "value": None, "margin": None,
                "reading": {"type": "score", "score": float(score), "probabilities": {}},
                "numbers": {"confidence": _optional_unit(answer.get("confidence")), "probabilities": None}}
    mass = _mass({str(k): v for k, v in raw.items()} if isinstance(raw, Mapping) else raw,
                 levels, sparse=True)
    mean = sum(i * mass[str(i)] for i in range(len(levels)))
    if abs(mean - score) > max(0.02, 0.005 * (sum(range(len(levels))) + 1)) + 1e-12:
        raise ValueError("score_matches_distribution")
    top = max(range(len(levels)), key=lambda i: mass[str(i)])
    margin = _margin(list(mass.values()))
    usable = margin >= floor - _EPS and int(math.floor(score + 0.5)) == top
    return {"adequate": usable, "value": top, "margin": margin,
            "reading": {"type": "score", "score": float(score),
                        "probabilities": {int(k): v for k, v in mass.items() if v}},
            "numbers": {"confidence": _optional_unit(answer.get("confidence")),
                        "probabilities": [mass[k] for k in levels]}}


class Evaluator:
    """One instance per host turn. Independent ready requests share a batch.

    The deadline is checked on return as well as passed to the network client.
    A late answer is never adequate. No automatic retry or fallback model calls.
    What is scanned is exactly what is sent: a JSON snapshot taken once, so later
    mutation of the caller's objects (even by ask or admission) cannot change the
    outbound text. Every failure path returns non-adequate receipts; nothing but
    invalid caller arguments raises.
    """

    def __init__(self, *, ask: Callable = None, clock: Callable = None,
                 budget: float = 5.0, deadline: float = 1.0, margin: float = None,
                 exclude: Sequence[str] = ()):
        if not _number(budget) or not _number(deadline) or budget <= 0 or deadline <= 0:
            raise ValueError("invalid_budget")
        if margin is not None and (not _number(margin) or not 0 < margin <= 1):
            raise ValueError("invalid_margin")
        self.margin = margin  # host config override of the fixed 0.15; None keeps each point's own
        self.exclude = frozenset(exclude)
        self.ask = ask or client.ask
        self.clock = clock or time.monotonic
        self.remaining = float(budget)
        self.deadline = float(deadline)

    @classmethod
    def from_settings(cls, settings: Any, **kw) -> "Evaluator":
        """Build from the host's frozen settings object (duck-typed), or None when disabled.

        Reads enabled, margin, timeout_seconds, turn_budget_seconds, exclude. A disabled
        host gets no evaluator at all, so nothing can evaluate, send or time anything.
        """
        if getattr(settings, "enabled", False) is not True:
            return None
        return cls(margin=settings.margin, deadline=settings.timeout_seconds,
                   budget=settings.turn_budget_seconds, exclude=settings.exclude, **kw)

    def evaluate(self, state: Any, requests: Sequence[Mapping], *,
                 admission: Callable[[], bool]) -> list:
        """Return recommendations/fallback readings, never applied receipts.

        admission is host-owned and rechecked immediately at the send boundary.
        The complete state AND every question are scanned, without clipping.
        recommendation is a candidate index (choice option position, score level) or,
        for noul, a bool. candidate_source must equal the point's registered source.
        """
        if not isinstance(requests, (list, tuple)) or not 1 <= len(requests) <= 32:
            raise ValueError("invalid_batch")
        questions, points, base = {}, [], []
        for index, request in enumerate(requests):
            if not isinstance(request, Mapping) or set(request) != {"point", "question", "candidate_source"}:
                raise ValueError("invalid_request_fields")
            point = REGISTRY.get(request["point"]) if isinstance(request["point"], str) else None
            if point is None:
                raise ValueError("unknown_point")
            question = client.check_question("q" + str(index), request["question"])
            if question["type"] not in point.types:
                raise ValueError("invalid_point_type")
            if request["candidate_source"] not in point.sources:
                raise ValueError("invalid_candidate_source")
            questions["q" + str(index)] = question
            points.append(point)
            base.append({"schema": "decision_receipt.v1", "point_id": point.id,
                         "candidate_source": request["candidate_source"],
                         "evaluation": "invalid", "adequate": False,
                         "recommendation": None, "applied": None,
                         "fallback": point.fallback, "reason": "invalid",
                         "reading": None, "margin": None, "latency_ms": 0,
                         "confidence": None, "probabilities": None, "input_tokens": None,
                         "output_tokens": None, "batch_size": None})
        skipped = [i for i, point in enumerate(points) if point.id in self.exclude]
        if skipped:
            live = [i for i in range(len(points)) if i not in skipped]
            merged = self._fallback([base[i] for i in skipped], "excluded")
            if live:
                done = self.evaluate(state, [requests[i] for i in live], admission=admission)
                merged = merged + done
            order = skipped + live
            result = [None] * len(points)
            for position, item in zip(order, merged):
                result[position] = item
            return result
        try:
            outbound = json.dumps({"state": state, "questions": questions},
                                  ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            return base
        if not callable(admission):
            return self._fallback(base, "local_privacy_skip")
        try:
            blocked = (privacy.is_sensitive(outbound)
                       or privacy.redact(outbound, len(outbound) + 1) != outbound)
        except Exception:
            blocked = True
        if blocked or not self._admitted(admission):
            return self._fallback(base, "local_privacy_skip")
        timeout = min(self.remaining, self.deadline)
        if not timeout > 0:
            return self._fallback(base, "budget_skip")
        snapshot = json.loads(outbound)
        started = self._now()
        try:
            if not self._admitted(admission):
                return self._fallback(base, "local_privacy_skip")
            response = self.ask(snapshot["state"], snapshot["questions"], timeout=timeout, retries=0)
            elapsed = self._charge(started, timeout)
            if elapsed > timeout:
                return self._fallback(base, "budget_timeout", elapsed)
            if not self._admitted(admission):
                return self._fallback(base, "privacy_revoked", elapsed)
            # Validate the entire batch before accepting any result.
            answers = response["answers"]
            if not isinstance(answers, Mapping) or set(answers) != set(questions):
                raise ValueError("answer_set")
            readings = [adequate(point, questions["q" + str(i)], answers["q" + str(i)], self.margin)
                        for i, point in enumerate(points)]
            model = response.get("jev_model")
            model = model if isinstance(model, str) else None
            usage = response.get("usage") if isinstance(response.get("usage"), Mapping) else {}
            spent = {"input_tokens": _count(response.get("input_tokens")),
                     "output_tokens": _count(usage.get("output_tokens")), "batch_size": len(base)}
            for item, reading, question in zip(base, readings, questions.values()):
                value = reading["value"]
                # Receipt stores candidate position, never arbitrary user option IDs.
                if question["type"] == "choice":
                    value = list(question["criteria"]).index(value)
                item.update(evaluation="jev", adequate=reading["adequate"],
                            recommendation=value if reading["adequate"] else None,
                            reading=reading["reading"], margin=reading["margin"],
                            reason="usable" if reading["adequate"] else "uncertainty",
                            fallback="none" if reading["adequate"] else item["fallback"],
                            latency_ms=round(elapsed * 1000), model=model, **reading["numbers"], **spent)
            return base
        except client.JevError:
            return self._fallback(base, "outage", self._charge(started, timeout))
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
            return self._fallback(base, "invalid", self._charge(started, timeout))
        except Exception:
            return self._fallback(base, "outage", self._charge(started, timeout))

    def _now(self):
        try:
            now = self.clock()
        except Exception:
            return None
        return float(now) if _number(now) else None

    def _charge(self, started, timeout):
        """Charge elapsed time; an unreadable or non-monotonic clock counts as a blown deadline."""
        now = self._now()
        elapsed = (now - started) if now is not None and started is not None else None
        if elapsed is None or not math.isfinite(elapsed) or elapsed < 0:
            elapsed = timeout * 2
        self.remaining -= elapsed
        return elapsed

    @staticmethod
    def _admitted(admission):
        try:
            return admission() is True
        except Exception:
            return False

    @staticmethod
    def _fallback(base, reason, elapsed=0.0):
        return [{**item, "evaluation": reason, "reason": reason,
                 "latency_ms": round(elapsed * 1000)} for item in base]


def receipt(item: Mapping) -> Dict:
    """Strict public-safe diagnostic, without outbound state or option names."""
    fields = ("schema", "point_id", "candidate_source", "evaluation", "adequate",
              "recommendation", "applied", "fallback", "reason", "margin", "latency_ms",
              "confidence", "probabilities", "input_tokens", "output_tokens", "batch_size")
    return {field: item[field] for field in fields if field in item}
