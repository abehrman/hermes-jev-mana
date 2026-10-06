"""Typed workflow builders for gui.step, code.approach and approval.triage.

A builder turns host-owned typed inputs into a ``Workflow``: the public question and state
Jev may see, and the exact candidate values the host may later execute. Jev only picks an
index into those values (Choice), reads a rubric (Score, weights applied here in code) or
answers one yes/no (Noul). It never writes an argument, a selector, a command or code.

Nothing here executes, authorizes or sends. The host's ``Consumer`` owns the durable claim,
the revalidation and the effect; the ``Evaluator`` owns the fixed 0.15 choice margin and the
0.2/0.8 Noul bands. A source the host cannot call public stays local: the workflow then
carries no state and no candidate descriptions at all.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from . import choose, client, decisions, privacy

GUI_POINT = "gui.step"
CODE_POINT = "code.approach"
APPROVAL_POINT = "approval.triage"

# ── shared ───────────────────────────────────────────────────────────────────

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


class WorkflowRejected(ValueError):
    """The typed input cannot become a public question. The host stays local and takes its fallback."""


@dataclass(frozen=True)
class Workflow:
    """Everything ``Consumer.run`` needs except the host's effect and revision reader."""

    point: str
    step: str
    revision: str
    state: Any
    question: Dict[str, Any]
    candidate_source: str
    values: List[Dict[str, Any]]
    validate: Callable[[str, Any], bool]
    authorize: Callable[[str, Any], bool]
    local_only: bool = False
    wrap_evaluate: Callable[[Callable], Callable] = field(default=lambda evaluate_fn: evaluate_fn)

    def admit(self, host_admit: Callable[[], bool]) -> Callable[[], bool]:
        """Host admission AND this workflow's own source label; a local-only source is never admitted."""
        def admitted() -> bool:
            return (not self.local_only) and host_admit() is True
        return admitted

    def run_args(self) -> Dict[str, Any]:
        return {"point": self.point, "step": self.step, "revision": self.revision, "state": self.state,
                "question": self.question, "candidate_source": self.candidate_source,
                "values": self.values}


def _step(step: Any) -> str:
    if not isinstance(step, str) or not _ID.fullmatch(step):
        raise WorkflowRejected("step must be an opaque host-minted id")
    return step


def _json_safe(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


# ── gui.step ─────────────────────────────────────────────────────────────────

ADMITTED_SOURCES = frozenset({"dom", "accessibility"})  # code-produced element trees only
ELEMENT_VERBS = frozenset({"click", "type"})
PLAIN_VERBS = frozenset({"scroll", "wait", "reobserve", "abstain"})
_GUI_FIELDS = {"id", "verb", "element_id", "args", "description", "observation_revision"}


@dataclass(frozen=True)
class Element:
    id: str
    role: str
    label: str
    interactive: bool = True


@dataclass(frozen=True)
class Observation:
    """One code-produced observation. ``source`` is the host's label, never inferred from content."""

    revision: str
    source: str
    elements: Tuple[Element, ...] = ()


LOCAL_ACTIONS = (
    {"id": "reobserve", "verb": "reobserve", "element_id": None, "args": {},
     "description": "Observe the screen again before choosing."},
    {"id": "abstain", "verb": "abstain", "element_id": None, "args": {},
     "description": "Take no action; the goal cannot be advanced safely."},
)


def gui_validator(current: Callable[[], Observation], allowed: Optional[Callable[[Mapping], bool]] = None):
    """(validate, authorize) for gui.step values, reading the host's live observation each time.

    validate: exact shape, bound to the CURRENT observation revision, and an element action's
    target must exist now and be interactive. authorize: validate again, then the host's own
    target/verb allowlist. A revision that moved after the table was built fails both.
    """
    def validate(point: str, value: Any) -> bool:
        if point != GUI_POINT or not isinstance(value, Mapping) or set(value) != _GUI_FIELDS:
            return False
        observation = current()
        if value["observation_revision"] != observation.revision:
            return False
        if not isinstance(value["id"], str) or not _ID.fullmatch(value["id"]):
            return False
        if not isinstance(value["args"], Mapping) or not _json_safe(value["args"]):
            return False
        verb = value["verb"]
        if value["id"] in ("reobserve", "abstain") and verb != value["id"]:
            return False
        if verb in PLAIN_VERBS:
            return value["element_id"] is None
        if verb not in ELEMENT_VERBS or observation.source not in ADMITTED_SOURCES:
            return False
        return any(e.id == value["element_id"] and e.interactive for e in observation.elements)

    def authorize(point: str, value: Any) -> bool:
        return validate(point, value) and (allowed is None or allowed(value) is True)

    return validate, authorize


def build_gui_step(*, step: str, goal: str, observation: Observation, actions: Sequence[Mapping],
                   history: Sequence[Mapping] = (), allowed: Optional[Callable[[Mapping], bool]] = None,
                   current: Optional[Callable[[], Observation]] = None) -> Workflow:
    """A gui.step workflow from a code-built action table.

    ``actions`` are host actions ``{id, verb, element_id?, args?, description}`` that the host
    already judged safe; ``args`` (exact coordinates, tokens, text) never leave the host. The
    existing ``choose.validate`` contract is reused for the public request: unique ids, text
    limits, the secret scan and the mandatory ``reobserve``/``abstain`` rows. ``choose.choose``
    itself is not used because it calls the network with its own confidence floor; the fixed
    0.15 margin and admission rechecks live in the Evaluator.

    A screenshot or any source not labelled ``dom``/``accessibility`` yields a local-only
    workflow with only the two local actions and no state: nothing from it can be sent.
    """
    step = _step(step)
    if not isinstance(observation, Observation) or not observation.revision:
        raise WorkflowRejected("observation needs a revision")
    live = current or (lambda: observation)
    validate, authorize = gui_validator(live, allowed)
    local = observation.source not in ADMITTED_SOURCES

    def typed(action: Mapping) -> Dict[str, Any]:
        if not isinstance(action, Mapping) or not {"id", "verb", "description"} <= set(action) \
                or not set(action) <= {"id", "verb", "description", "element_id", "args"}:
            raise WorkflowRejected("an action is {id, verb, description, element_id?, args?}")
        return {"id": action["id"], "verb": action["verb"], "element_id": action.get("element_id"),
                "args": dict(action.get("args") or {}), "description": action["description"],
                "observation_revision": observation.revision}

    values = [typed(a) for a in (LOCAL_ACTIONS if local else actions)]
    if not local:
        # The two local actions are always on the table; the host may not leave them out.
        values += [typed(a) for a in LOCAL_ACTIONS if a["id"] not in {v["id"] for v in values}]
        request = {"schema": choose.REQUEST_SCHEMA, "goal": goal, "observation_id": observation.revision,
                   "regions": [{"id": e.id, "role": e.role, "label": e.label[:300], "interactive": e.interactive}
                               for e in observation.elements[:choose.MAX_REGIONS] if e.label.strip()],
                   "history": list(history),
                   "candidates": [{"id": v["id"], "description": v["description"]} for v in values]}
        try:
            valid = choose.validate(request)
        except ValueError as error:
            raise WorkflowRejected(str(error)) from None
        if not all(validate(GUI_POINT, v) for v in values):
            raise WorkflowRejected("an action does not match this observation")
        table = valid["table"]
        state: Any = {"goal": valid["goal"], "on_screen": valid["regions"], "already_tried": valid["history"]}
    else:
        table = {v["id"]: v["description"] for v in values}
        state = {}
    question = client.choice("Which single action should be taken next to move toward the goal?", table)
    # Values follow the table order: the Evaluator reports a position, never an id.
    values = [next(v for v in values if v["id"] == key) for key in table]
    return Workflow(GUI_POINT, step, observation.revision, state, question, "code_enum", values,
                    validate, authorize, local_only=local)


# ── code.approach ────────────────────────────────────────────────────────────

MAX_APPROACHES = 6
DEFAULT_DIMENSIONS = (
    ("fit", 0.4, "How well does the approach solve the stated task as scoped?"),
    ("simplicity", 0.2, "How small and easy to review is the change the approach implies?"),
    ("testability", 0.2, "How easily can the approach be verified with automated tests?"),
    ("reversibility", 0.2, "How easily can the change be backed out if it proves wrong?"),
)
LEVELS = ("poor", "fair", "good", "excellent")
_APPROACH_FIELDS = {"id", "title", "summary"}
_APPROACH_ID = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")


def accept_approaches(alternatives: Any) -> List[Dict[str, str]]:
    """Typed approach alternatives from host patterns or ONE bounded LLM draft, or WorkflowRejected.

    Each is ``{id, title, summary}``: single-line prose. Fenced or multi-line text is refused,
    so a draft cannot smuggle code or instructions through a description.
    """
    if not isinstance(alternatives, (list, tuple)) or not 2 <= len(alternatives) <= MAX_APPROACHES:
        raise WorkflowRejected(f"between 2 and {MAX_APPROACHES} approaches are required")
    seen, clean = set(), []
    for item in alternatives:
        if not isinstance(item, Mapping) or set(item) != _APPROACH_FIELDS:
            raise WorkflowRejected("an approach is exactly {id, title, summary}")
        ident, title, summary = item["id"], item["title"], item["summary"]
        if not isinstance(ident, str) or not _APPROACH_ID.fullmatch(ident) or ident in seen:
            raise WorkflowRejected("approach ids are unique lowercase slugs")
        for text, limit in ((title, 80), (summary, 300)):
            if (not isinstance(text, str) or not text.strip() or len(text) > limit or "\n" in text
                    or "```" in text or privacy.is_sensitive(text)):
                raise WorkflowRejected("approach text must be short, single-line, non-sensitive prose")
        seen.add(ident)
        clean.append({"id": ident, "title": title.strip(), "summary": summary.strip()})
    return clean


def _weights(dimensions: Sequence[Tuple[str, float, str]]) -> None:
    total = 0.0
    for name, weight, rule in dimensions:
        if (not isinstance(name, str) or not name or not isinstance(rule, str) or not rule
                or isinstance(weight, bool) or not isinstance(weight, (int, float))
                or not math.isfinite(weight) or weight <= 0):
            raise WorkflowRejected("a dimension is (name, positive weight, rule)")
        total += weight
    if not dimensions or abs(total - 1.0) > 1e-9:
        raise WorkflowRejected("dimension weights must sum to 1")


def composite_evaluate(evaluate_fn: Callable, approaches: Sequence[Mapping],
                       dimensions: Sequence[Tuple[str, float, str]], candidate_source: str):
    """Wrap ``Evaluator.evaluate`` so ONE Consumer request becomes one Score per (approach, dimension).

    One batch goes to the Evaluator. Every reading must itself be adequate (fixed rubric
    margin); the weighted totals are computed here in code and the top approach must lead the
    runner-up by the fixed 0.15 margin. Any non-Jev or inadequate reading is returned as the
    inadequate result it is, so the host's fallback applies; a mixed batch never half-applies.
    """
    margin_floor = decisions.REGISTRY[CODE_POINT].margin
    levels = len(LEVELS)

    def composite(state: Any, requests: Sequence[Mapping], *, admission: Callable[[], bool]) -> list:
        task = state.get("task") if isinstance(state, Mapping) else None
        batch = [{"point": CODE_POINT, "candidate_source": candidate_source,
                  "question": client.score({"task": task,
                                            "approach": {"title": a["title"], "summary": a["summary"]},
                                            "dimension": name, "rule": rule, "direction": "higher is better"},
                                           list(LEVELS))}
                 for a in approaches for name, _, rule in dimensions]
        items = evaluate_fn(state, batch, admission=admission)
        bad = next((i for i in items if i.get("evaluation") != "jev"), None) \
            or next((i for i in items if not i.get("adequate")), None)
        if bad is not None:
            return [{**bad, "recommendation": None, "adequate": False, "margin": None,
                     "reason": bad.get("reason") if bad.get("reason") != "usable" else "uncertainty"}]
        totals = []
        for row, _approach in enumerate(approaches):
            mark = 0.0
            for col, (_, weight, _rule) in enumerate(dimensions):
                score = items[row * len(dimensions) + col]["recommendation"]
                mark += weight * min(1.0, max(0.0, float(score) / (levels - 1)))
            totals.append(mark)
        ranked = sorted(range(len(totals)), key=lambda i: totals[i], reverse=True)
        gap = totals[ranked[0]] - totals[ranked[1]]
        usable = gap >= margin_floor
        first = items[0]
        return [{**first, "recommendation": ranked[0] if usable else None, "adequate": usable,
                 "margin": gap, "reading": None, "reason": "usable" if usable else "uncertainty",
                 "fallback": "none" if usable else decisions.REGISTRY[CODE_POINT].fallback}]

    return composite


def build_code_approach(*, step: str, task: str, revision: str, patterns: Optional[Sequence[Mapping]] = None,
                        drafted: Optional[Sequence[Mapping]] = None, mode: str = "choice",
                        dimensions: Sequence[Tuple[str, float, str]] = DEFAULT_DIMENSIONS,
                        commit_ok: Optional[Callable[[Mapping], bool]] = None) -> Workflow:
    """A code.approach workflow over code-known patterns, else one bounded LLM draft.

    Jev picks ONE approach (Choice) or scores each approach on weighted rubric dimensions
    (``mode="score"``). The chosen approach is a short typed description; the LLM, never Jev,
    writes the code. ``patterns`` win over ``drafted``; each is labelled honestly:
    ``code_enum`` for patterns, ``bounded_llm_draft`` for a draft.
    """
    step = _step(step)
    if mode not in ("choice", "score"):
        raise WorkflowRejected("mode is choice or score")
    if not isinstance(task, str) or not task.strip() or len(task) > 600 or privacy.is_sensitive(task):
        raise WorkflowRejected("task must be a short, non-sensitive public summary")
    if patterns:
        approaches, source = accept_approaches(patterns), "code_enum"
    elif drafted:
        approaches, source = accept_approaches(drafted), "bounded_llm_draft"
    else:
        raise WorkflowRejected("no approaches: draft once or supply code patterns")
    point = decisions.REGISTRY[CODE_POINT]
    if source not in (getattr(point, "candidate_sources", None) or (point.candidate_source,)):
        # Never relabel code patterns as an LLM draft to get past the registry.
        raise WorkflowRejected(f"candidate_source_not_registered:{source}")
    if mode == "score":
        _weights(dimensions)
        if len(approaches) * len(dimensions) > 32:
            raise WorkflowRejected("too many approach/dimension pairs for one batch")
    table = {a["id"]: f'{a["title"]}: {a["summary"]}' for a in approaches}
    question = client.choice("Which approach best fits the task? Pick one; do not write code.", table)

    def validate(point: str, value: Any) -> bool:
        return (point == CODE_POINT and isinstance(value, Mapping) and set(value) == _APPROACH_FIELDS
                and value in approaches)

    def authorize(point: str, value: Any) -> bool:
        return validate(point, value) and (commit_ok is None or commit_ok(value) is True)

    wrap = (lambda fn: composite_evaluate(fn, approaches, dimensions, source)) if mode == "score" \
        else (lambda fn: fn)
    return Workflow(CODE_POINT, step, revision, {"task": task.strip()}, question, source,
                    [dict(a) for a in approaches], validate, authorize, wrap_evaluate=wrap)


# ── approval.triage ──────────────────────────────────────────────────────────

# Deterministic, never put to Jev: Adam-only and money/investment actions stay outside model discretion.
HUMAN_ONLY = frozenset({"money_movement", "investment_change", "purchase", "credential_entry",
                        "account_open_close", "adam_only"})
CLASSIFIABLE = frozenset({"file_write", "network_request", "process_exec", "package_install",
                          "browser_script", "other_flagged"})
EFFECTS = ("ask_person", "defer_to_guardian")  # neither one approves anything
_FACT = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_ASK_PERSON = {"id": "ask_person", "effect": "ask_person"}
_DEFER = {"id": "defer_to_guardian", "effect": "defer_to_guardian"}


def triage_precheck(category: Any, hard_rule: Optional[Callable[[str], Optional[str]]] = None) -> Optional[str]:
    """The deterministic answer when there is one: ``"ask_person"``, ``"deny"`` or ``"existing"``; else None.

    Human-only categories, a host hard rule, and any category the host did not type never
    reach Jev. ``"existing"`` means: take the existing guardian path unchanged.
    """
    if category in HUMAN_ONLY:
        return "ask_person"
    if category not in CLASSIFIABLE:
        return "existing"
    if hard_rule is not None:
        verdict = hard_rule(category)
        if verdict in ("ask_person", "deny"):
            return verdict
    return None


def build_approval_triage(*, step: str, category: str, facts: Mapping[str, Any], revision: str,
                          classifier: str = "choice") -> Workflow:
    """An approval.triage workflow. The only candidates are ``ask_person`` and ``defer_to_guardian``.

    Jev sees the host's category and typed boolean/integer facts, never the command text. Its
    answer can only escalate to a person or leave the decision with the existing guardian; no
    candidate means approve, and ``validate`` refuses a table that contains any other effect.
    ``classifier="noul"`` asks one yes/no ("needs a person?"): yes applies ask_person, no
    declines and the host continues with the existing guardian.
    """
    step = _step(step)
    if category not in CLASSIFIABLE:
        raise WorkflowRejected("only classifiable categories reach Jev; use triage_precheck")
    if classifier not in ("choice", "noul"):
        raise WorkflowRejected("classifier is choice or noul")
    if not isinstance(facts, Mapping) or len(facts) > 12:
        raise WorkflowRejected("facts are a small mapping")
    clean: Dict[str, Any] = {}
    for key, value in facts.items():
        if not isinstance(key, str) or not _FACT.fullmatch(key) \
                or not (isinstance(value, (bool, int)) and abs(int(value)) <= 1_000_000):
            raise WorkflowRejected("facts are lowercase names with boolean or integer values")
        clean[key] = value
    state = {"category": category, "facts": clean}

    def validate(point: str, value: Any) -> bool:
        return point == APPROVAL_POINT and value in (_ASK_PERSON, _DEFER)

    def authorize(point: str, value: Any) -> bool:
        return validate(point, value) and value["effect"] in EFFECTS

    if classifier == "choice":
        question = client.choice(
            "Does this flagged action need a person's review, or can the existing guardian assess it? "
            "You cannot approve anything.",
            {"ask_person": "A person should review this action before anything else happens.",
             "defer_to_guardian": "The existing guardian can assess this action."})
        values = [dict(_ASK_PERSON), dict(_DEFER)]
    else:
        question = client.noul("Does this flagged action need a person's review before any approval step?",
                               {"true": "A person should review it.", "false": "The existing guardian can assess it."})
        values = [dict(_ASK_PERSON)]
    return Workflow(APPROVAL_POINT, step, revision, state, question, "code_enum", values, validate, authorize)


# ── gui.step: the existing GUI runner's choose, aligned with the decision registry ─────────────

def _gui_settings():
    """The host's frozen decision settings when running inside Hermes, else None (registry off)."""
    try:
        from agent.decision_config import load_settings
        return load_settings()
    except Exception:
        return None


class GuiOutcome:
    """Bound outcome for one applied table row: a durable claim before the driver acts and a settle
    after it. A claim that already exists is a replay: the row is never driven twice."""

    def __init__(self, path, key: str, receipts: bool):
        import sqlite3
        self.path, self.key, self.receipts = path, key, receipts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), timeout=1)
        self._db.execute("CREATE TABLE IF NOT EXISTS rows (key TEXT PRIMARY KEY, status TEXT, outcome TEXT)")

    def reserve(self) -> bool:
        with self._db:
            return self._db.execute("INSERT OR IGNORE INTO rows VALUES (?, 'reserved', '')", (self.key,)).rowcount == 1

    def settle(self, op: str, detail: str, landed: bool) -> None:
        with self._db:
            self._db.execute("UPDATE rows SET status=?, outcome=? WHERE key=? AND status='reserved'",
                             ("applied" if landed else "unverified", json.dumps({"op": op}), self.key))


def gui_choose(request, choose, *, session: str = "", digest: str = "", settings=None, home=None):
    """(reply, bound) for one GUI step. Off, excluded or unhealthy: ``choose(request)`` exactly as
    the runner always called it, and no bound outcome. On: the registry deadline is the choose
    timeout, the registry margin turns a near tie into the runner's own ``reobserve`` (no action),
    and a selected row gets a durable claim. Opaque browser_exec/CDP scripts are never table rows
    and stay on the original model path; this only covers rows the runner already validated."""
    settings = settings if settings is not None else _gui_settings()
    if settings is None or not settings.includes("gui.step"):
        return choose(request), None
    from pathlib import Path
    home = Path(home) if home else _hermes_home()
    try:
        from . import decision_health
        if "gui.step" in decision_health.HealthLedger(home).unhealthy_points():
            return choose(request), None
    except Exception:
        return choose(request), None
    reply = dict(choose(request, timeout=settings.timeout_seconds))
    action = reply.get("selected_id", "reobserve")
    spread = reply.get("probabilities")
    ranked = sorted((v for v in spread.values() if isinstance(v, (int, float))), reverse=True) \
        if isinstance(spread, dict) else []
    gap = ranked[0] - ranked[1] if len(ranked) >= 2 else None
    if action not in ("reobserve", "done", "abstain") and not (gap is not None and gap >= settings.margin):
        reply.update(selected_id="reobserve", reason="uncertainty")
        return reply, None
    if action in ("reobserve", "done", "abstain"):
        return reply, None
    bound = GuiOutcome(home / "decisions" / "gui-journal.sqlite",
                       f"{session}|{request.get('observation_id')}|{digest}|{action}", settings.receipts)
    if not bound.reserve():
        reply.update(selected_id="reobserve", reason="replayed")   # reconcile: never drive it twice
        return reply, None
    return reply, bound


def _hermes_home():
    from pathlib import Path
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
