"""jev-mana: Jev-assisted decision points for Hermes.

Registers the decision hooks only when the normal config master is on (``jev.decisions: on``) and this
plugin is enabled. Off, or with the plugin disabled, nothing is registered: no hook, evaluator, receipt,
journal or network call exists, and Hermes runs its original code paths unchanged.

The hooks answer the core's generic decision seam (``agent/decision_seams.py``): they turn each typed
request into one Jev question, read the typed answer, and return a reading. They never execute, authorize,
draft code or choose outside the candidates the host handed over.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)
RECEIPT_FILE = "jev-decisions.jsonl"


def _decisions_on() -> bool:
    try:
        from hermes_cli.config import load_config_readonly
        value = (load_config_readonly().get("jev") or {}).get("decisions", "off")
    except Exception:
        return False
    return value is True or (isinstance(value, str) and value.strip().lower() == "on")


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _profile(home: Path) -> str:
    return home.name if home.parent.name == "profiles" else "default"


def write_receipt(receipt: Dict[str, Any]) -> None:
    """Append one public-safe receipt row (numbers, ids and fixed words only) to the profile's log, 0600."""
    try:
        home = _home()
        path = home / "logs" / RECEIPT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": round(time.time(), 3), "profile": _profile(home), **receipt}
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
    except OSError:
        pass


def register(ctx: Any) -> None:
    if not _decisions_on():
        return
    from .decision_hooks import register_decision_hooks
    register_decision_hooks(ctx, log=_log_receipt)


def _log_receipt(entry: Dict[str, Any]) -> None:
    receipt = entry.get("decision") if isinstance(entry, dict) else None
    if isinstance(receipt, dict):
        write_receipt(receipt)
