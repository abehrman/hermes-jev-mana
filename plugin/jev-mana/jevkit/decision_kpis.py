"""Read-only KPIs over local decision receipts, for the router dashboard.

Standard library only. Never calls Jev, never writes, never opens a file other than the two
named receipt logs inside a Hermes home. Output carries ids, numbers and fixed reason words
only: raw goals, state, arguments and option names are never copied, because every output
field is built from a whitelist, not from the input row.

Accepted receipt fields (all optional; rows are normalised tolerantly, anything else is ignored)
-------------------------------------------------------------------------------------------
  id            receipt_id | id | decision_id         shown only as a canonical lowercase version-4 UUID; any other
                                                       string (raw source/tool-call ids, custom text) is dropped
  time          ts | timestamp                         epoch seconds, or ISO-8601 text
  profile       profile | profile_scope                safe token
  point         point_id | point                       registry id; unknown ids are kept and warned
  call          network_call | jev_call (bool)         else derived from `evaluation` (see below)
  evaluation    evaluation                             jev | outage | budget_timeout | privacy_revoked |
                                                       invalid | local_privacy_skip | budget_skip | ...
  adequate      adequate (bool)  or adequacy.adequate
  applied       applied (candidate index, 0 counts) or status/application_status == "applied"
  status        application_status | status            applied | fallback | ...
  reason        reason | reason_code | adequacy.reason_code | override | fallback_reason
  fallback      fallback                               none | original | deterministic | ...
  confidence    confidence | max(probabilities)        0..1, else dropped
  margin        margin                                 0..1, else dropped
  score         score                                  finite number
  latency_ms    latency_ms | timing.jev_ms             >= 0
  tokens        tokens_in/out | input_tokens/output_tokens | usage.input_tokens/output_tokens |
                jev_tokens | tokens                    absent means unknown, never zero
  batch         batch_size (1..32), call_id | batch_id  the evaluator repeats a batch's tokens and latency on
                                                       every receipt in it. With a call id the batch is counted
                                                       once; with only batch_size each receipt carries 1/batch_size;
                                                       with neither it is counted whole and flagged batch_unknown
  activity      activity_class                         create | maintain | anything else -> other. Only this actual
                                                       field is read; it is never inferred from the point or text
  chosen        applied (int 0..999)                   the chosen candidate position, numeric only
  original_would_call_llm  (bool)                      only True counts toward avoided LLM calls
  schema        decision_receipt.v1 marks a new-style receipt

Legacy `jev-decisions.jsonl` rows have no point id. Only `kind: route` rows are decisions
(point route.session; `routed` + mode "on" is applied); `route_effective` and every other kind
are ignored and counted in ``ignored_rows``.

A counted Jev call is a network evaluation: evaluation jev/outage/budget_timeout/privacy_revoked,
or invalid with latency > 0 (invalid before the network has latency 0). Skips (local_privacy_skip,
budget_skip) and excluded points are decisions that fell back to the original path, not calls.
"""
from __future__ import annotations

import datetime
import json
import math
import os
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

# The standard (public) package must not assume sibling modules, so the twelve ids are a literal;
# tests assert they equal jevkit.decisions.REGISTRY. Change both together.
POINT_IDS = ("ask.closed", "ask.decision_gate", "route.session", "route.effort", "skill.pick",
             "context.pick", "tool.plan", "search.pick", "gui.step", "approval.triage", "code.approach",
             "work.executor")
REASONS = ("error", "timeout", "tie", "privacy", "excluded", "invalid", "stale", "health_failed")
ACTIVITY_CLASSES = ("create", "maintain")
ERROR_REASONS = ("error", "timeout", "invalid")
DEFAULTS = {"margin": 0.15, "timeout_seconds": 1.0, "turn_budget_seconds": 5.0, "receipts": True}
RECEIPT_FILES = ("jev-decisions.jsonl", "jev-decision-receipts.jsonl")
MAX_TAIL_BYTES = 2_000_000
MAX_ROWS = 20_000
RECENT_LIMIT = 25

_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_REASON_WORDS = {
    "outage": "error", "error": "error", "jev_error": "error", "failed": "error", "fail_open": "error",
    "budget_timeout": "timeout", "budget_skip": "timeout", "timeout": "timeout", "budget": "timeout",
    "uncertainty": "tie", "tie": "tie", "near_tie": "tie",
    "local_privacy_skip": "privacy", "privacy_revoked": "privacy", "privacy": "privacy",
    "privacy_skip": "privacy",
    "excluded": "excluded", "point_excluded": "excluded", "health_failed": "health_failed",
    "invalid": "invalid", "stale": "stale", "replayed": "stale",
    "usable": "none", "none": "none",
}
_CALL_EVALUATIONS = {"jev", "outage", "budget_timeout", "privacy_revoked"}
_FALLBACKS = {"original", "deterministic", "permitted_llm", "human"}


# ------------------------------------------------------------------ helpers
def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _token(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and _TOKEN.match(value) else None


def _unit(value: Any) -> Optional[float]:
    return float(value) if _num(value) and 0 <= value <= 1 else None


def _nonneg(value: Any) -> Optional[float]:
    return float(value) if _num(value) and value >= 0 else None


def _epoch(value: Any) -> Optional[float]:
    if _num(value):
        return float(value) if value > 0 else None
    if isinstance(value, str) and value.strip():
        try:
            return datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _first(row: Mapping, *names: str) -> Any:
    for name in names:
        if row.get(name) is not None:
            return row[name]
    return None


def _sub(row: Mapping, name: str) -> Mapping:
    value = row.get(name)
    return value if isinstance(value, Mapping) else {}


def _reason_word(*candidates: Any) -> Optional[str]:
    for candidate in candidates:
        if isinstance(candidate, str):
            word = _REASON_WORDS.get(candidate.strip().lower())
            if word is not None:
                return word
    return None


_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _uuid(value: Any) -> Optional[str]:
    """A canonical lowercase version-4 UUID, else None. Raw source-call ids and custom strings never pass."""
    if isinstance(value, str) and _UUID4.match(value):
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    return None


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """Nearest-rank percentile: the value at 1-based rank ceil(q * n). None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


# --------------------------------------------------------------- normalizer
def normalize(row: Any) -> Optional[Dict[str, Any]]:
    """One tolerant, whitelist-only record, or None when the row is not a decision receipt."""
    if not isinstance(row, Mapping):
        return None
    legacy = False
    point = _token(_first(row, "point_id", "point"))
    if point is None:
        if row.get("kind") != "route":
            return None
        legacy, point = True, "route.session"
    probs = row.get("probabilities")
    if not isinstance(probs, Mapping):
        probs = _sub(row, "reading").get("probabilities")
    top = sorted((float(v) for v in probs.values() if _unit(v) is not None), reverse=True) \
        if isinstance(probs, Mapping) else []
    confidence = _unit(row.get("confidence"))
    if confidence is None and top:
        confidence = top[0]
    margin = _unit(row.get("margin"))
    if margin is None and len(top) > 1:
        margin = top[0] - top[1]
    latency = _nonneg(_first(row, "latency_ms"))
    if latency is None:
        latency = _nonneg(_sub(row, "timing").get("jev_ms"))
    usage = _sub(row, "usage")
    tin = _nonneg(_first(row, "tokens_in", "input_tokens") if _first(row, "tokens_in", "input_tokens") is not None
                  else usage.get("input_tokens"))
    tout = _nonneg(_first(row, "tokens_out", "output_tokens") if _first(row, "tokens_out", "output_tokens") is not None
                   else usage.get("output_tokens"))
    tokens = None
    if tin is not None or tout is not None:
        tokens = (tin or 0.0) + (tout or 0.0)
    else:
        tokens = _nonneg(_first(row, "jev_tokens", "tokens"))
    evaluation = row.get("evaluation") if isinstance(row.get("evaluation"), str) else None
    adequacy = _sub(row, "adequacy")
    adequate = row.get("adequate") if isinstance(row.get("adequate"), bool) else adequacy.get("adequate")
    adequate = adequate if isinstance(adequate, bool) else None
    status = _first(row, "application_status", "status")
    status = status.strip().lower() if isinstance(status, str) else None
    reason = _reason_word(row.get("reason"), row.get("reason_code"), adequacy.get("reason_code"),
                          row.get("override"), row.get("fallback_reason"), evaluation)
    applied_field = row.get("applied")
    if legacy:
        applied = bool(row.get("routed")) and row.get("mode") == "on"
        evaluation = "jev" if (latency is not None or confidence is not None) else None
        status = "applied" if applied else "fallback"
        reason = reason or ("none" if applied else None)
    elif status == "applied":
        applied = True
    elif status in ("fallback", "fell_back"):
        applied = False
    else:
        applied = applied_field is not None and applied_field is not False
    # A reason of "none" is the usable path, not a failure.
    if reason == "none" and not applied and adequate is False:
        reason = "tie"
    explicit = _first(row, "network_call", "jev_call")
    if isinstance(explicit, bool):
        call = explicit
    elif evaluation in _CALL_EVALUATIONS:
        call = True
    elif evaluation == "invalid":
        call = latency is not None and latency > 0
    else:
        call = False
    if reason in ("excluded",) and not isinstance(explicit, bool):
        call = False
    fallback = row.get("fallback") if isinstance(row.get("fallback"), str) else None
    if applied:
        outcome = "applied"
    elif (status in ("fallback", "fell_back") or reason not in (None, "none") or adequate is False
          or fallback in _FALLBACKS):
        outcome = "fallback"
        reason = reason if reason not in (None, "none") else "other"
    else:
        outcome = "unresolved"
        reason = None
    would = row.get("original_would_call_llm")
    raw_id = _first(row, "receipt_id", "id", "decision_id")
    size = row.get("batch_size")
    size = size if isinstance(size, int) and not isinstance(size, bool) and 1 <= size <= 32 else None
    chosen = applied_field if isinstance(applied_field, int) and not isinstance(applied_field, bool) \
        and 0 <= applied_field <= 999 and outcome == "applied" else None
    activity = row.get("activity_class")
    activity = None if activity is None else (activity if activity in ACTIVITY_CLASSES else "other")
    return {
        "id": _token(raw_id), "uuid": _uuid(raw_id), "chosen": chosen, "activity": activity,
        "batch_size": size, "batch_key": _token(_first(row, "call_id", "batch_id")),
        "ts": _epoch(_first(row, "ts", "timestamp")),
        "profile": _token(_first(row, "profile", "profile_scope")),
        "point": point, "call": call, "outcome": outcome, "reason": reason,
        "confidence": confidence, "margin": margin,
        "score": float(row["score"]) if _num(row.get("score")) else None,
        "latency_ms": latency, "tokens": tokens,
        "would_call_llm": would if isinstance(would, bool) else None,
    }


# -------------------------------------------------------------- file adapter
def receipt_sources(hermes_home: str, profiles: Optional[Iterable[str]] = None,
                    homes: Optional[Mapping[str, str]] = None) -> List[Dict[str, str]]:
    """The only files this module will open: two named logs per profile home. Symlinks are refused.
    `homes` (name -> actual path, from Hermes' own profile API) is used as given: no path is rebuilt.
    Without it (a bare checkout) the standard `<root>/profiles/<name>` layout is scanned."""
    if homes is not None:
        homes = [(n, str(p)) for n, p in homes.items() if _TOKEN.match(n)]
    else:
        homes = [("default", hermes_home)]
        pdir = os.path.join(hermes_home, "profiles")
        names = sorted(os.listdir(pdir)) if os.path.isdir(pdir) else []
        homes += [(n, os.path.join(pdir, n)) for n in names if _TOKEN.match(n) and os.path.isdir(os.path.join(pdir, n))]
    if profiles is not None:
        wanted = set(profiles)
        homes = [h for h in homes if h[0] in wanted]
    return [{"profile": name, "path": os.path.join(home, "logs", fname), "file": fname}
            for name, home in homes for fname in RECEIPT_FILES]


def read_receipts(hermes_home: str, profiles: Optional[Iterable[str]] = None,
                  homes: Optional[Mapping[str, str]] = None):
    """(records, report). Read-only tail reads; rows missing a profile take the folder's profile."""
    records: List[Dict[str, Any]] = []
    report = {"files": [], "rows": 0, "ignored_rows": 0, "bad_lines": 0, "truncated": False}
    for src in receipt_sources(hermes_home, profiles, homes):
        path = src["path"]
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(max(0, size - MAX_TAIL_BYTES))
                chunk = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        lines = chunk.splitlines()
        if size > MAX_TAIL_BYTES:
            lines = lines[1:]
            report["truncated"] = True
        used = 0
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                report["bad_lines"] += 1
                continue
            rec = normalize(row)
            if rec is None:
                report["ignored_rows"] += 1
                continue
            rec["profile"] = rec["profile"] or src["profile"]
            records.append(rec)
            used += 1
        report["rows"] += used
        report["files"].append({"profile": src["profile"], "file": src["file"], "rows": used})
    if len(records) > MAX_ROWS:
        records.sort(key=lambda r: r["ts"] or 0)
        records, report["truncated"] = records[-MAX_ROWS:], True
    return records, report


# ------------------------------------------------------------------- config
def master_value(raw: Any) -> str:
    """jev.decisions from a parsed config. YAML 1.1 turns bare on/off into booleans."""
    value = raw.get("jev", {}).get("decisions") if isinstance(raw, Mapping) and isinstance(raw.get("jev"), Mapping) else None
    if value is True or (isinstance(value, str) and value.strip().lower() == "on"):
        return "on"
    return "off"


def effective_config(global_cfg: Optional[Mapping], profile_cfg: Optional[Mapping], *,
                     is_default: bool = False) -> Dict[str, Any]:
    """Merge the global and per-profile sidecar. Scalars: profile wins. exclude: union.
    include: subtracted from the exclusions. use_global false: the profile file stands alone.
    The global file's own use_global/include are ignored. Unknown point ids warn and are dropped."""
    warnings: List[str] = []
    gcfg = global_cfg if isinstance(global_cfg, Mapping) else {}
    pcfg = {} if is_default else (profile_cfg if isinstance(profile_cfg, Mapping) else {})
    use_global = pcfg.get("use_global", True) is not False
    layers = ([("global", gcfg)] if use_global else []) + ([("profile", pcfg)] if not is_default else [])
    merged = dict(DEFAULTS)
    for label, layer in layers:
        for key, ok in (("margin", lambda v: _num(v) and 0 < v <= 1),
                        ("timeout_seconds", lambda v: _num(v) and v > 0),
                        ("turn_budget_seconds", lambda v: _num(v) and v > 0),
                        ("receipts", lambda v: isinstance(v, bool))):
            if key in layer and layer[key] is not None:
                if ok(layer[key]):
                    merged[key] = layer[key]
                else:
                    warnings.append("%s %s is invalid; default kept" % (label, key))

    def ids(layer: Mapping, key: str, label: str) -> set:
        raw = layer.get(key) or []
        if not isinstance(raw, (list, tuple)):
            warnings.append("%s %s must be a list" % (label, key))
            return set()
        out = set()
        for item in raw:
            if item in POINT_IDS:
                out.add(item)
            else:
                warnings.append("unknown point id in %s %s: %s" % (label, key, _token(item) or "<unprintable>"))
        return out

    exclude: set = set()
    for label, layer in layers:
        exclude |= ids(layer, "exclude", label)
    include = ids(pcfg, "include", "profile") if use_global and not is_default else set()
    for conflict in sorted(include & ids(pcfg, "exclude", "profile")) if include else []:
        warnings.append("point %s is both excluded and included in the profile file; include wins" % conflict)
    exclude -= include
    merged["exclude"] = [p for p in POINT_IDS if p in exclude]
    merged["use_global"] = use_global
    merged["warnings"] = sorted(set(warnings))
    return merged


def profile_configs(master_by_profile: Mapping[str, Any], global_cfg: Optional[Mapping],
                    sidecar_by_profile: Mapping[str, Optional[Mapping]]) -> Dict[str, Dict[str, Any]]:
    """Per profile: master on/off, effective sidecar values and which points are live."""
    out = {}
    for name, master_cfg in master_by_profile.items():
        cfg = effective_config(global_cfg, sidecar_by_profile.get(name), is_default=(name == "default"))
        master = master_value(master_cfg)
        out[name] = {"master": master, "config": cfg,
                     "points": {p: ("off" if master == "off" else "excluded" if p in cfg["exclude"] else "on")
                                for p in POINT_IDS}}
    return out


# --------------------------------------------------------------- aggregation
def parse_window(window: Optional[str], now: float) -> Optional[float]:
    m = re.fullmatch(r"(\d+)([hdm])", window or "")
    return now - int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)] if m else None


def filter_records(records: Sequence[Dict], *, profile: Optional[str] = None, point: Optional[str] = None,
                   since: Optional[float] = None, until: Optional[float] = None) -> List[Dict]:
    out = []
    for r in records:
        if profile and r["profile"] != profile:
            continue
        if point and r["point"] != point:
            continue
        if since is not None or until is not None:
            if r["ts"] is None or (since is not None and r["ts"] < since) or (until is not None and r["ts"] > until):
                continue
        out.append(r)
    return out


def _distribution(values: Sequence[Optional[float]]) -> Dict[str, Any]:
    known = [v for v in values if v is not None]
    bins = [0] * 10
    for v in known:
        bins[min(9, int(math.floor(v * 10 + 1e-9)))] += 1
    return {"n": len(known), "missing": len(values) - len(known), "bins": bins,
            "bin_edges": [round(i / 10, 1) for i in range(11)],
            "mean": round(sum(known) / len(known), 6) if known else None,
            "min": min(known) if known else None, "max": max(known) if known else None}


def _rate(n: int, d: int) -> Optional[float]:
    return round(n / d, 6) if d else None


def _token_attribution(calls: Sequence[Dict]) -> Dict[str, Any]:
    """Jev tokens and network batches without counting a batch once per receipt.

    The evaluator stamps one batch's tokens on every receipt in it. A call/batch id counts the batch
    once; batch_size alone gives each receipt 1/batch_size (exact when every receipt of the batch is
    in view); neither counts the receipt whole and is flagged batch_unknown. Unknown is never zero."""
    total, recorded, batches = 0.0, 0, 0.0
    how = {"unique_call": 0, "divided": 0, "batch_unknown": 0}
    seen: Dict[Any, List[int]] = {}
    for r in calls:
        key = (r["profile"], r["batch_key"]) if r["batch_key"] else None
        if key is not None:
            if key in seen:
                seen[key][0] += 1
            else:
                seen[key] = [1, r["batch_size"] or 0]
                batches += 1
                how["unique_call"] += r["tokens"] is not None
                total += r["tokens"] or 0.0
        else:
            batches += 1.0 / r["batch_size"] if r["batch_size"] else 1
            if r["tokens"] is not None:
                if r["batch_size"]:
                    total += r["tokens"] / r["batch_size"]
                    how["divided"] += 1
                else:
                    total += r["tokens"]
                    how["batch_unknown"] += 1
        recorded += r["tokens"] is not None
    incomplete = sum(1 for seen_n, size in seen.values() if size and seen_n < size)
    return {"batches": round(batches, 6), "tokens": {
        "total": round(total, 6) if recorded else None, "recorded_calls": recorded, "attribution": how,
        "incomplete_batches": incomplete,
        "note": "each batch counted once (call id) or as 1/batch_size per receipt; unknown is not zero"}}


def _activity(records: Sequence[Dict]) -> Dict[str, Any]:
    """create/maintain counts from the receipt's own activity_class only; unknown values aggregate to other."""
    out: Dict[str, Any] = {k: {"decisions": 0, "calls": 0, "applied": 0, "fallback": 0}
                           for k in ACTIVITY_CLASSES + ("other",)}
    for r in records:
        if r["activity"] is None:
            continue
        a = out[r["activity"]]
        a["decisions"] += 1
        a["calls"] += r["call"]
        a["applied"] += r["outcome"] == "applied"
        a["fallback"] += r["outcome"] == "fallback"
    out["work_executor_without_class"] = sum(1 for r in records if r["point"] == "work.executor" and r["activity"] is None)
    return out


def aggregate(records: Sequence[Dict], *, historical_profiles: Iterable[str] = (),
              bucket_seconds: int = 3600) -> Dict[str, Any]:
    """KPIs over already-filtered records. Rates have decisions as the denominator unless named call_*.

    historical_profiles are profiles whose master switch is off now: their receipts are history,
    so they add to calls_historical and never to calls_current."""
    history = set(historical_profiles)
    total = len(records)
    calls = [r for r in records if r["call"]]
    applied = [r for r in records if r["outcome"] == "applied"]
    fallback = [r for r in records if r["outcome"] == "fallback"]
    reasons = {w: sum(1 for r in fallback if r["reason"] == w) for w in REASONS}
    reasons["other"] = sum(1 for r in fallback if r["reason"] not in REASONS)
    by_reason = {w: {"count": n, "rate": _rate(n, total)} for w, n in reasons.items()}
    per_point: Dict[str, Dict[str, Any]] = {}
    for r in records:
        p = per_point.setdefault(r["point"], {"decisions": 0, "calls": 0, "applied": 0, "fallback": 0})
        p["decisions"] += 1
        p["calls"] += r["call"]
        p["applied"] += r["outcome"] == "applied"
        p["fallback"] += r["outcome"] == "fallback"
    tokens = _token_attribution(calls)
    lat = [r["latency_ms"] for r in calls if r["latency_ms"] is not None]
    avoided = [r for r in applied if r["would_call_llm"] is True]
    buckets: Dict[int, Dict[str, int]] = {}
    for r in calls:
        if r["ts"] is None:
            continue
        b = buckets.setdefault(int(r["ts"] // bucket_seconds * bucket_seconds),
                               {"calls": 0, "error": 0, "timeout": 0, "invalid": 0})
        b["calls"] += 1
        if r["outcome"] == "fallback" and r["reason"] in ERROR_REASONS:
            b[r["reason"]] += 1
    for r in records:  # error-class fallbacks that never reached the network still show as a bucket
        if not r["call"] and r["ts"] is not None and r["outcome"] == "fallback" and r["reason"] in ERROR_REASONS:
            b = buckets.setdefault(int(r["ts"] // bucket_seconds * bucket_seconds),
                                   {"calls": 0, "error": 0, "timeout": 0, "invalid": 0})
            b[r["reason"]] += 1
    return {
        "decisions": total, "calls": len(calls), "skipped_not_calls": total - len(calls),
        "calls_current": sum(1 for r in calls if r["profile"] not in history),
        "calls_historical": sum(1 for r in calls if r["profile"] in history),
        "outcomes": {"applied": len(applied), "fallback": len(fallback), "unresolved": total - len(applied) - len(fallback)},
        "rates": {"applied": _rate(len(applied), total), "fallback": _rate(len(fallback), total),
                  "call_applied": _rate(sum(1 for r in applied if r["call"]), len(calls)),
                  "call_fallback": _rate(sum(1 for r in fallback if r["call"]), len(calls))},
        "fallback_by_reason": by_reason, "per_point": per_point,
        "confidence": _distribution([r["confidence"] for r in calls]),
        "margin": _distribution([r["margin"] for r in calls]),
        "latency_ms": {"n": len(lat), "p50": percentile(lat, 0.5), "p95": percentile(lat, 0.95)},
        "tokens": {**tokens["tokens"], "unrecorded_calls": len(calls) - tokens["tokens"]["recorded_calls"]},
        "network_batches": tokens["batches"], "activity": _activity(records),
        "health_failed_by_point": {p: n for p, n in sorted(
            ((p, sum(1 for r in fallback if r["reason"] == "health_failed" and r["point"] == p))
             for p in {r["point"] for r in fallback}), key=lambda kv: kv[0]) if n},
        "avoided_llm_calls": {"estimate": len(avoided),
                              "unknown_original": sum(1 for r in applied if r["would_call_llm"] is None),
                              "basis": "applied decisions with original_would_call_llm true"},
        "timeline": [{"t": t, **buckets[t]} for t in sorted(buckets)], "bucket_seconds": bucket_seconds,
    }


def recent_receipts(records: Sequence[Dict], limit: int = RECENT_LIMIT) -> List[Dict[str, Any]]:
    keep = sorted(records, key=lambda r: r["ts"] or 0, reverse=True)[:limit]
    return [{k: r[k] for k in ("id", "ts", "profile", "point", "call", "outcome", "reason",
                               "confidence", "margin", "score", "latency_ms")} for r in keep]


# ------------------------------------------------------- safe (hostile-input) outputs
HEALTH_STATUSES = ("healthy", "unhealthy", "unknown", "not_applicable", "off")
HEALTH_PROBES = ("registration", "hashes", "config", "plugin", "patch", "smoke", "flow", "jev")
HEALTH_STALE_SECONDS = 86400
_REASON_CODE = re.compile(r"^[a-z_]{1,24}:[a-z_]{1,32}(:[A-Za-z]{1,40})?$")   # probe:reason[:ExceptionName]
_PROBE_REASON = re.compile(r"^[a-z_]{1,32}(:[A-Za-z]{1,40})?$")
_OUTCOMES = ("applied", "fallback", "unresolved")
_ALL_REASONS = REASONS + ("other", "none")


def _enum(value: Any, allowed: Sequence[str], default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def safe_recent(records: Sequence[Dict], known_profiles: Iterable[str], limit: int = RECENT_LIMIT) -> List[Dict[str, Any]]:
    """Recent decisions as numbers, booleans and fixed enums only. No receipt-supplied string
    survives: ids are dropped, an unregistered point is "other", a profile is shown only when the
    host's own profile list names it, and reason/outcome are re-checked against their enums."""
    known = set(known_profiles)
    keep = sorted(records, key=lambda r: r["ts"] or 0, reverse=True)[:limit]
    return [{"n": i + 1, "id": r["uuid"], "chosen": r["chosen"],
             "activity_class": r["activity"], "ts": r["ts"],
             "profile": r["profile"] if r["profile"] in known else None,
             "point": _enum(r["point"], POINT_IDS, "other"), "call": bool(r["call"]),
             "outcome": _enum(r["outcome"], _OUTCOMES, "unresolved"),
             "reason": _enum(r["reason"], _ALL_REASONS, "other") if r["reason"] else None,
             "confidence": r["confidence"], "margin": r["margin"], "score": r["score"],
             "latency_ms": r["latency_ms"]} for i, r in enumerate(keep)]


def sanitize_health(raw: Any, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Whitelist a health.json (the full jev-decision-health/1 report, the ledger state.json, or
    a pre-sanitized banner). Free text such as `summary` is never copied; point ids must be
    registry ids; reasons must look like `probe:reason_code`."""
    out: Dict[str, Any] = {"status": "unavailable", "enabled": None, "counts": None, "unhealthy_points": [],
                           "unknown_points": [], "points": {}, "probes": {}, "probe_reasons": {}, "reason_codes": {},
                           "reachability": None,
                           "checked_at": None, "age_seconds": None, "stale": None}
    if not isinstance(raw, Mapping):
        return out
    out["status"] = _enum(raw.get("status"), HEALTH_STATUSES, "unknown")
    out["enabled"] = raw["enabled"] if isinstance(raw.get("enabled"), bool) else None
    pts = raw.get("points") if isinstance(raw.get("points"), Mapping) else {}
    for pid in POINT_IDS:
        entry = pts.get(pid)
        if isinstance(entry, Mapping):
            out["points"][pid] = _enum(entry.get("status"), HEALTH_STATUSES, "unknown")
            codes = [c for c in (entry.get("reasons") or []) if isinstance(c, str) and _REASON_CODE.match(c)][:6]
            if codes:
                out["reason_codes"][pid] = codes
        elif isinstance(entry, str):
            out["points"][pid] = _enum(entry, HEALTH_STATUSES, "unknown")
    for key in ("unhealthy_points", "unknown_points"):
        listed = raw.get(key) if isinstance(raw.get(key), (list, tuple)) else []
        out[key] = [p for p in POINT_IDS if p in listed]
    for p, st in out["points"].items():
        if st == "unhealthy" and p not in out["unhealthy_points"]:
            out["unhealthy_points"].append(p)
    counts = raw.get("counts") if isinstance(raw.get("counts"), Mapping) else None
    if counts is not None:
        out["counts"] = {k: int(counts[k]) if _num(counts.get(k)) and counts[k] >= 0 else None
                         for k in ("healthy", "unhealthy", "unknown")}
    probes = raw.get("probes")
    if isinstance(probes, list):
        probes = {p.get("name"): p for p in probes if isinstance(p, Mapping)}
    if isinstance(probes, Mapping):
        for name in HEALTH_PROBES:
            entry = probes.get(name)
            if isinstance(entry, Mapping):
                out["probes"][name] = _enum(entry.get("status"), HEALTH_STATUSES, "unknown")
                code = entry.get("reason")
                if isinstance(code, str) and _PROBE_REASON.match(code):
                    out["probe_reasons"][name] = code
            elif isinstance(entry, str):
                out["probes"][name] = _enum(entry, HEALTH_STATUSES, "unknown")
    reach = raw.get("reachability")
    if isinstance(reach, Mapping):
        verified = reach.get("verified_points") if isinstance(reach.get("verified_points"), (list, tuple)) else []
        out["reachability"] = {"verified_points": [p for p in POINT_IDS if p in verified],
                               "full": reach.get("full") is True}
    at = _epoch(_first(raw, "checked_at", "last_seen", "ts"))
    if at is not None:
        out["checked_at"] = at
        if now is not None:
            out["age_seconds"] = max(0.0, now - at)
            out["stale"] = out["age_seconds"] > HEALTH_STALE_SECONDS
    return out


def read_health(root: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Read-only: `<root>/jev-health/health.json` (the hook the host wires), else the ledger's
    `state.json`. A missing or unreadable file is "unavailable", never an error and never a check."""
    for name in ("health.json", "state.json"):
        path = os.path.join(root, "jev-health", name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, "rb") as fh:
                raw = json.loads(fh.read(512_000).decode("utf-8", "replace"))
        except (OSError, ValueError):
            continue
        return sanitize_health(raw, now=now)
    return sanitize_health(None, now=now)


def patch_from_health(health: Mapping) -> Dict[str, Any]:
    """Patch status exactly as the local health checker (`patch --dry-run`, run by the producer) reported it.
    healthy/applied, unhealthy/not_applied or drifted, unknown/no_patch_root, not_applicable/no_patch.
    No producer, or no patch probe: not_applicable. The dashboard runs nothing."""
    probes = health.get("probes") if isinstance(health, Mapping) else None
    state = probes.get("patch") if isinstance(probes, Mapping) else None
    reasons = health.get("probe_reasons") if isinstance(health, Mapping) else None
    code = reasons.get("patch") if isinstance(reasons, Mapping) and state in HEALTH_STATUSES else None
    return {"state": state if state in HEALTH_STATUSES else "not_applicable", "reason": code, "source": "health"}


# -------------------------------------------------------------------- the view
def build_view(records: Sequence[Dict], report: Mapping, configs: Mapping[str, Mapping], *,
               profile: Optional[str] = None, point: Optional[str] = None, since: Optional[float] = None,
               until: Optional[float] = None, patch: Optional[Mapping] = None, now: Optional[float] = None,
               bucket_seconds: int = 3600, safe: bool = False,
               health: Optional[Mapping] = None) -> Dict[str, Any]:
    """safe=True is the standard-dashboard mode: every receipt-derived string is dropped or enum-checked."""
    warnings: List[str] = []
    for name, cfg in configs.items():
        warnings += ["%s: %s" % (name, w) for w in cfg["config"]["warnings"]]
    if point and point not in POINT_IDS:
        warnings.append("unknown point id in filter: %s" % (_token(point) or "<unprintable>"))
    if profile and profile not in configs:
        warnings.append("unknown profile in filter: %s" % (_token(profile) or "<unprintable>"))
    seen = sorted({r["point"] for r in records if r["point"] not in POINT_IDS})
    if safe:
        if seen:
            warnings.append("receipts name %d point id(s) that are not in the registry" % len(seen))
        records = [{**r, "point": r["point"] if r["point"] in POINT_IDS else "other"} for r in records]
        warnings = [w for w in warnings if not w.startswith("unknown point id in filter")
                    and not w.startswith("unknown profile in filter")]
        if point and point not in POINT_IDS:
            warnings.append("point filter is not a registry id; no rows match")
        if profile and profile not in configs:
            warnings.append("profile filter is not a known profile; no rows match")
    else:
        warnings += ["unknown point id in receipts: %s" % p for p in seen]
    scoped = {k: v for k, v in configs.items() if not profile or k == profile}
    off = [k for k, v in scoped.items() if v["master"] == "off"]
    state = "off" if scoped and len(off) == len(scoped) else "on" if not off else "mixed"
    if not scoped:
        state = "off"
    rows = filter_records(records, profile=profile, point=point, since=since, until=until)
    kpis = aggregate(rows, historical_profiles=off, bucket_seconds=bucket_seconds)
    # A profile that is off makes no calls now, whatever its old receipts say.
    kpis["calls_current"] = 0 if state == "off" else kpis["calls_current"]
    shown = {"profile": profile, "point": point, "since": since, "until": until}
    if safe:
        shown = {"profile": profile if profile in configs else None, "point": point if point in POINT_IDS else None,
                 "since": since if _num(since) else None, "until": until if _num(until) else None}
    return {"generated_at": time.time() if now is None else now,
            "filters": shown,
            "state": state, "kpis": kpis, "profiles": configs, "points": list(POINT_IDS),
            "patch": dict(patch) if patch is not None else None,
            "recent": safe_recent(rows, configs) if safe else recent_receipts(rows),
            "health": dict(health) if health is not None else None,
            "sources": dict(report), "warnings": sorted(set(warnings))}
