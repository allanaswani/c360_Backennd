"""Tiny in-process TTL cache for the portfolio overview.

Level 1 aggregates across the whole book, so it must never run live on every page
load (architecture §6). In production this becomes a nightly Celery-precomputed
summary table; here a short-TTL process cache stands in for that pattern and proves
the shape — the service reads through this, the management command warms it.
"""
from __future__ import annotations

import time
from threading import Lock
from typing import Any, Callable

_TTL_SECONDS = 300  # dev stand-in for a nightly refresh
_store: dict[str, tuple[float, Any]] = {}
_lock = Lock()


def scope_key(sales_codes: list[str] | None) -> str:
    return 'ALL' if sales_codes is None else ','.join(sorted(sales_codes)) or 'NONE'


def get_or_build(key: str, builder: Callable[[], Any], *, ttl: int = _TTL_SECONDS) -> tuple[Any, dict]:
    """Return (payload, cache_meta). cache_meta records freshness for the UI."""
    now = time.time()
    with _lock:
        hit = _store.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], {'cached': True, 'age_seconds': int(now - hit[0])}
    payload = builder()
    with _lock:
        _store[key] = (now, payload)
    return payload, {'cached': False, 'age_seconds': 0}


def warm(key: str, builder: Callable[[], Any]) -> None:
    with _lock:
        _store[key] = (time.time(), builder())


def invalidate() -> None:
    with _lock:
        _store.clear()
