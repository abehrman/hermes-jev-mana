"""Minimal Jev client for the jev-mana plugin: typed question builders and one `jev ask` call.

Jev answers typed questions about a state: ``choice`` (one of a closed set), ``score`` (a position on an
ordered rubric) and ``noul`` (the probability that a statement is true). It never writes text.

Transport is the ``jev ask`` command (JSON on stdin, JSON on stdout), so the key lives in the operator's own
secret store and this package never touches it. Every failure is a ``JevError``; callers treat that as "no
usable answer" and run the original Hermes code path.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Union

MAX_STATE_CHARS = 60_000
JEV_COMMAND = "jev"
State = Union[str, Mapping[str, Any], Sequence[Any]]
Transport = Callable[[Dict[str, Any], float], Dict[str, Any]]

class JevError(RuntimeError):
    """Anything that means "do not trust or use this Jev result"."""

    def __init__(self, code: str, detail: str = "", retry_after: Optional[float] = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        # Set only by a contradiction check (`client._invalid`): which invariant the reply broke.
        self.invariant: Optional[str] = None
        # Seconds the server asked us to wait (`retry-after-ms` / `retry-after`), when it said.
        # Only a patient caller (a batch job) waits that long; a live caller keeps its budget.
        self.retry_after = retry_after


# ── question builders ────────────────────────────────────────────────────────

def choice(instructions: str, criteria: Mapping[str, str]) -> Dict[str, Any]:
    if len(criteria) < 2:
        raise ValueError("a choice needs at least two options")
    return {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}


def score(instructions: str, levels: Sequence[str]) -> Dict[str, Any]:
    if len(levels) < 2:
        raise ValueError("a score needs at least two levels")
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def noul(instructions: Any, criteria: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """A yes/no question. ``criteria`` optionally says what a yes and a no mean.

    ``{"true": ..., "false": ...}`` is part of the API's Noul (the guardrails cookbook and
    LangChain's AutoMode both depend on it). Written criteria are always sent: LangChain's
    AutoModeMiddleware defaults them in ``__init__`` and then never sends them, which is the
    bug a caller of this builder cannot reproduce.
    """
    question: Dict[str, Any] = {"type": "noul", "instructions": instructions}
    if criteria is not None:
        question["criteria"] = dict(criteria)
    return question


# ── the shape of a question ──────────────────────────────────────────────────
#
# The builders above only guard the callers that use them. A dict written by hand went
# straight to the wire, and `_check_answer` then read `question["criteria"]` and died on a
# KeyError, or read an answer against a question nobody had actually asked. The rules live
# here, once, and `ask` applies them to everything it is handed — so a feature cannot ship a
# question Jev cannot answer, whoever built it.

QUESTION_TYPES = ("choice", "score", "noul")
MIN_CRITERIA = 2
# The API's own limits (TypeSafe API reference, read 2026-09-26): at most 255 options in a
# Choice, and a Score of 2 to 10 levels. Refused here, because the API answers either with a
# 422 that every feature reads as "Jev had no opinion" — a silent fail-open on a caller bug.
MAX_CHOICE_OPTIONS = 255
MAX_SCORE_LEVELS = 10
NOUL_CRITERIA_KEYS = ("true", "false")


def _structured(value: Any) -> bool:
    """A string with words in it, or a non-empty object or array: what the API takes as text.

    ``instructions`` and every criterion may be an object or an array as well as a string,
    so a long question can keep its data in named fields and point at them in backticks.
    """
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, (Mapping, list, tuple)) and bool(value)


def _identifier(text: Any) -> str:
    """Fold punctuation/case/underscores, keeping letters and digits in every script."""
    return re.sub(r"[\W_]+", " ", str(text).lower()).strip()


def _shown(value: Any) -> str:
    """A refusal quotes part of the caller's own input back, never all of it.

    A 1 MB type came back as a 1 MB error with the actual complaint at the far end of it.
    """
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= 80 else text[:77] + "..."


def check_question(name: str, question: Any) -> Dict[str, Any]:
    """One question in the shape Jev answers, or ``ValueError`` saying what is wrong.

    The rules, and why each one exists:

    * the ``type`` decides how the reply is read, so an unknown one is refused;
    * ``instructions`` is the question. The name is an identifier — a question whose
      instructions repeat its own name was never written, and Jev scores the state against
      that name;
    * a choice needs a mapping of 2 to 255 options and a score a list of 2 to 10 levels,
      because one option is not a choice, the reply check reads the criteria, and the API
      refuses anything past its limits;
    * a noul's criteria, when written, are exactly ``{"true": ..., "false": ...}`` (either or
      both). Anything else would be sent and silently ignored, so it is refused.

    ``instructions`` and each criterion may be a string, or an object or array (the API's
    structured form). Only a string can "repeat its own name", so only a string is checked
    for that.
    """
    if not isinstance(question, Mapping):
        raise ValueError(f'question "{_shown(name)}" must be an object like {{"type": ..., "instructions": ...}}')
    kind = question.get("type")
    if kind not in QUESTION_TYPES:
        found = "has no type" if kind is None else f"has unknown type {_shown(json.dumps(kind, default=str))}"
        raise ValueError(f'question "{_shown(name)}" {found}; use one of {", ".join(QUESTION_TYPES)}')
    text = question.get("instructions")
    if not _structured(text):
        raise ValueError(f'question "{_shown(name)}" has no instructions: the text of the question, as a string '
                         f'(or a non-empty object or array holding it)')
    if isinstance(text, str) and _identifier(text) == _identifier(name):
        raise ValueError(f'question "{_shown(name)}" asks nothing: its instructions only repeat its own name. '
                         f'The id names the question, "instructions" asks it')
    text = text if isinstance(text, str) else json.loads(json.dumps(text, default=str))
    criteria = question.get("criteria")
    if kind == "noul":
        if criteria is None:
            return {"type": "noul", "instructions": text}
        if (not isinstance(criteria, Mapping) or not criteria
                or not set(map(str, criteria)) <= set(NOUL_CRITERIA_KEYS)):
            raise ValueError(f'question "{_shown(name)}" is a noul: its criteria, if any, must be '
                             f'{{"true": "what a yes means", "false": "what a no means"}}; anything else '
                             f'would be sent and never read')
        for side, meaning in criteria.items():
            if not _structured(meaning):
                raise ValueError(f'question "{_shown(name)}": the noul criterion "{side}" is empty')
        return {"type": "noul", "instructions": text,
                "criteria": {str(side): criteria[side] for side in criteria}}
    wanted, shape = ((Mapping, 'an object of 2 to 255 options, {"option": "what it means"}')
                     if kind == "choice" else (list, "a list of 2 to 10 levels, lowest first"))
    if criteria is None:
        raise ValueError(f'question "{_shown(name)}": criteria are required for a {kind}, as {shape}')
    if not isinstance(criteria, wanted) or len(criteria) < MIN_CRITERIA:
        raise ValueError(f'question "{_shown(name)}": criteria for a {kind} must be {shape}')
    if kind == "choice":
        if len(criteria) > MAX_CHOICE_OPTIONS:
            raise ValueError(f'question "{_shown(name)}": {len(criteria)} options; a choice takes at most '
                             f'{MAX_CHOICE_OPTIONS}. Pick a group first, then a member')
        return {"type": kind, "instructions": text, "criteria": dict(criteria)}
    if len(criteria) > MAX_SCORE_LEVELS:
        raise ValueError(f'question "{_shown(name)}": {len(criteria)} levels; a score takes at most '
                         f'{MAX_SCORE_LEVELS}')
    for position, level in enumerate(criteria):
        if not _structured(level):
            raise ValueError(f'question "{_shown(name)}": score level {position} is empty')
    return {"type": kind, "instructions": text, "criteria": list(criteria)}


def check_questions(questions: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Every question named and shaped, in the order and under the names it was handed."""
    if not isinstance(questions, Mapping):
        raise ValueError("questions must be a mapping of {name: question}")
    if not questions:
        raise ValueError("no questions")
    return {str(name): check_question(str(name), question) for name, question in questions.items()}



# ── transport: the `jev ask` command ─────────────────────────────────────────

def _request(state: State, questions: Mapping[str, Any]) -> Dict[str, Any]:
    checked = check_questions(questions)
    text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, allow_nan=False)
    if len(text) > MAX_STATE_CHARS:
        raise ValueError("state_too_large")
    return {"state": text, "questions": [{"id": name, **q} for name, q in checked.items()]}


def _jev_command(request: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    exe = shutil.which(JEV_COMMAND)
    if exe is None:
        raise JevError("no_jev_cli", "the `jev` command is not on PATH")
    try:
        done = subprocess.run([exe, "ask", "--timeout", str(timeout)], input=json.dumps(request),
                              capture_output=True, text=True, timeout=timeout + 1.0)
    except subprocess.TimeoutExpired as exc:
        raise JevError("timeout") from exc
    except OSError as exc:
        raise JevError("spawn_failed") from exc
    try:
        reply = json.loads(done.stdout)
    except ValueError as exc:
        raise JevError("bad_reply") from exc
    if done.returncode != 0 or not isinstance(reply, dict) or "error" in reply:
        code = reply.get("error") if isinstance(reply, dict) else None
        raise JevError(str(code or "jev_failed")[:64])
    return reply


def ask(state: State, questions: Mapping[str, Any], *, timeout: float = 4.0, retries: int = 0,
        transport: Optional[Transport] = None, **_ignored: Any) -> Dict[str, Any]:
    """One typed call. Returns Jev's reply (``answers``, ``usage``, ``input_tokens``, ``jev_model``).

    ``retries`` is accepted for API compatibility and ignored: a decision either arrives in time or the
    caller takes its original branch. ``transport(request, timeout) -> reply`` replaces the command in tests.
    """
    request = _request(state, questions)
    reply = (transport or _jev_command)(request, float(timeout))
    answers = reply.get("answers") if isinstance(reply, dict) else None
    if not isinstance(answers, dict):
        raise JevError("bad_reply", "no answers object")
    return reply

