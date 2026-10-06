"""Jev dashboard plugin: read-only backend, mounted by Hermes at /api/plugins/jev-mana/.

Hermes' own dashboard middleware authenticates every /api/ request before it reaches this
router, so nothing here checks credentials, opens a port or bypasses that gate. Handlers only
read local files and return summaries: they never return raw receipt rows, goals, state,
arguments or customer data, never call Jev, and never run the health doctor. Homes and
profiles come from Hermes' own APIs (hermes_constants, hermes_cli.profiles); where those
cannot be imported (a bare test harness) a plain directory scan of the same layout is used.

Costs (so nobody adds a probe here): the periodic health script is local only, zero LLM and zero
Jev. Boot and post-update smoke is one batched Jev call and zero LLM, made by the health producer.
A status change costs one operator turn (the producer's alert callback); an unchanged status is
silent. This page makes none of those calls: it only reads what the producer wrote.

Parent wiring hooks (module level, all optional):
    set_health_provider(callable)   returns a health.json-shaped dict; replaces the file read
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from fastapi import APIRouter, Query

log = logging.getLogger(__name__)
router = APIRouter()

_HERE = Path(__file__).resolve().parent
_NAME_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
_HEALTH_PROVIDER: Optional[Callable[[], Any]] = None
_KPIS = None


def set_health_provider(provider: Optional[Callable[[], Any]]) -> None:
    global _HEALTH_PROVIDER
    _HEALTH_PROVIDER = provider


def _kpis():
    """jevkit/decision_kpis.py, loaded by path: installed beside the plugin as jevkit/, or in the repo checkout."""
    global _KPIS
    if _KPIS is not None:
        return _KPIS
    for base in (_HERE.parent, *[p for p in _HERE.parents if (p / "jevkit" / "decision_kpis.py").is_file()][:1]):
        path = base / "jevkit" / "decision_kpis.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("jev_mana_decision_kpis", path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            _KPIS = module
            return module
    raise RuntimeError("jevkit/decision_kpis.py not found beside the plugin")


# ------------------------------------------------------------------ homes and profiles
def _root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001 - bare harness
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _homes(root: Path) -> Dict[str, Path]:
    """name -> actual home as Hermes reports it (never rebuilt from the name). Zero profiles: just the
    default home. Only when Hermes' profile API cannot be imported is `<root>/profiles` scanned."""
    homes: Dict[str, Path] = {"default": root}
    try:
        from hermes_cli.profiles import list_profiles
        for info in list_profiles(lazy_skill_count=True):
            name = str(getattr(info, "name", ""))
            if name and name != "default" and set(name) <= _NAME_OK and len(name) <= 64:
                homes[name] = Path(info.path)
        return homes
    except Exception:  # noqa: BLE001
        pass
    pdir = root / "profiles"
    try:
        names = sorted(os.listdir(pdir))
    except OSError:
        names = []
    for name in names:
        if set(name) <= _NAME_OK and len(name) <= 64 and (pdir / name).is_dir():
            homes[name] = pdir / name
    return homes


def _load_yaml(path: Path) -> Optional[Mapping]:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        from hermes_yaml import safe_load
    except Exception:  # noqa: BLE001
        try:
            from yaml import safe_load
        except Exception:  # noqa: BLE001
            return None
    try:
        data = safe_load(text)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, Mapping) else None


def _float(value: Optional[str]) -> Optional[float]:
    try:
        number = float(value) if value not in (None, "") else None
    except ValueError:
        return None
    return number if number is not None and number == number and abs(number) != float("inf") else None


def build_summary(profile: Optional[str] = None, point: Optional[str] = None, window: str = "24h",
                  since: Optional[str] = None, until: Optional[str] = None, *, now: Optional[float] = None) -> dict:
    K = _kpis()
    now = time.time() if now is None else now
    root = _root()
    homes = _homes(root)
    start = _float(since)
    if start is None:
        start = K.parse_window(window, now)
    global_cfg = _load_yaml(root / "jev_config.yaml")
    masters = {n: _load_yaml(h / "config.yaml") for n, h in homes.items()}
    sidecars = {n: _load_yaml(h / "jev_config.yaml") for n, h in homes.items() if n != "default"}
    configs = K.profile_configs(masters, global_cfg, sidecars)
    records, report = K.read_receipts(str(root), [profile] if profile in homes else None,
                                      homes={n: str(h) for n, h in homes.items()})
    if _HEALTH_PROVIDER is not None:
        try:
            health = K.sanitize_health(_HEALTH_PROVIDER(), now=now)
        except Exception:  # noqa: BLE001 - a broken provider is "unavailable", not a 500
            health = K.sanitize_health(None, now=now)
    else:
        health = K.read_health(str(root), now=now)
    bucket = 300 if window == "1h" else 86400 if window == "7d" else 3600
    view = K.build_view(records, report, configs, profile=profile, point=point, since=start,
                        until=_float(until), patch=K.patch_from_health(health), now=now,
                        bucket_seconds=bucket, safe=True, health=health)
    view["window"] = window if window in ("1h", "24h", "7d", "all") else "all"
    return view


@router.get("/summary")
def summary(profile: Optional[str] = Query(None), point: Optional[str] = Query(None), window: str = Query("24h"),
            since: Optional[str] = Query(None), until: Optional[str] = Query(None)):
    # No FastAPI length validation on purpose: a 422 would echo the caller's text back.
    clip = lambda v: v[:64] if isinstance(v, str) else v  # noqa: E731
    return build_summary(clip(profile), clip(point), clip(window), clip(since), clip(until))


@router.get("/health")
def health():
    """The sanitised health banner only. Never runs a probe."""
    K = _kpis()
    if _HEALTH_PROVIDER is not None:
        try:
            return K.sanitize_health(_HEALTH_PROVIDER(), now=time.time())
        except Exception:  # noqa: BLE001
            return K.sanitize_health(None, now=time.time())
    return K.read_health(str(_root()), now=time.time())
