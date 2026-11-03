"""Tiny in-process TTL cache for the whole-book pages (portfolio overview, worklist,
alerts, maturities, activity call list).

Level 1 aggregates across the whole book, so it must never run live on every page
load (architecture §6). Per worker process, in memory.

Two rules keep anyone from waiting on a rebuild they did not need (2026-11-03):

- One build per key at a time. Requests that miss while a build is running wait for
  that build instead of each starting their own - five RMs opening My book together
  cost one warehouse read, not five.
- Past its TTL an entry is still served, for up to ``STALE_FACTOR`` x TTL, while ONE
  background thread rebuilds it. Only a missing (or very old) entry makes a request
  wait. Every caller keys by data mode and, where the figures change with the load,
  by the warehouse as-of date, so a stale entry is never another day's figures.
"""
from __future__ import annotations

import logging
import threading
import time
from threading import Lock
from typing import Any, Callable

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300
STALE_FACTOR = 4
_store: dict[str, tuple[float, Any]] = {}
_lock = Lock()
_key_locks: dict[str, Lock] = {}
_refreshing: set[str] = set()


def scope_key(sales_codes: list[str] | None) -> str:
    return 'ALL' if sales_codes is None else ','.join(sorted(sales_codes)) or 'NONE'


def _meta(at: float, now: float, **extra) -> dict:
    return {'cached': True, 'age_seconds': int(now - at), **extra}


def _key_lock(key: str) -> Lock:
    with _lock:
        return _key_locks.setdefault(key, Lock())


def _refresh(key: str, builder: Callable[[], Any]) -> None:
    """Rebuild one entry; on failure keep the old one (it is retried next time)."""
    try:
        with _key_lock(key):
            started = time.time()
            payload = builder()
            with _lock:
                _store[key] = (started, payload)
    except Exception:                                          # noqa: BLE001
        logger.warning('background refresh of %s failed; serving the previous copy', key, exc_info=True)
    finally:
        with _lock:
            _refreshing.discard(key)


def _start(key: str, builder: Callable[[], Any]) -> None:
    threading.Thread(target=_refresh, args=(key, builder), name='c360-cache-refresh', daemon=True).start()


def get_or_build(key: str, builder: Callable[[], Any], *, ttl: int = _TTL_SECONDS) -> tuple[Any, dict]:
    """Return (payload, cache_meta). cache_meta records freshness for the UI."""
    now = time.time()
    with _lock:
        hit = _store.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1], _meta(hit[0], now)
        if hit and now - hit[0] < ttl * STALE_FACTOR:
            refresh = key not in _refreshing
            if refresh:
                _refreshing.add(key)
        else:
            refresh = None
    if refresh is not None:
        if refresh:
            _start(key, builder)
        return hit[1], _meta(hit[0], now, refreshing=True)
    with _key_lock(key):
        # Someone else may have built it while this request waited for the lock.
        with _lock:
            hit = _store.get(key)
        now = time.time()
        if hit and now - hit[0] < ttl:
            return hit[1], _meta(hit[0], now)
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
