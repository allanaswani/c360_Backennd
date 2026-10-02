"""A short-lived cache for the customer page's tab payloads.

The warehouse closes once a day, so a customer's tabs are the same for every reader
until the next load. Opening a customer, switching tab and coming back, or two RMs
opening the same customer, re-ran every warehouse read each time - 8 to 46 seconds
a tab on node1 (2026-10-02). Each tab's payload is now kept for ten minutes, keyed by
the customer, the period asked for and the warehouse as-of date (so a new day's load
is never served from yesterday's figures).

Only payloads that do not depend on WHO is asking are cached here: the access check
runs before every lookup, and anything filtered by the reader's scope (linked and
related parties) is not cached. Errors are never cached; a failed build is simply
retried on the next request. Per worker process, in memory, bounded.
"""
from __future__ import annotations

import json
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, Callable

TTL_SECONDS = 600
MAX_ENTRIES = 2000

_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_lock = Lock()


def key(name: str, cust_id: str, as_of: Any, period: Any = None) -> str:
    p = json.dumps(period.to_dict(), sort_keys=True, default=str) if period is not None else ''
    return f'{name}|{cust_id}|{as_of}|{p}'


def get_or_build(k: str, build: Callable[[], Any]) -> Any:
    now = time.time()
    with _lock:
        hit = _cache.get(k)
        if hit and now - hit[0] < TTL_SECONDS:
            _cache.move_to_end(k)
            return hit[1]
        if hit:
            _cache.pop(k, None)
    value = build()
    # None is "not found" - not worth keeping, and keeping it would hide a customer
    # who appears in the next load.
    if value is not None:
        with _lock:
            _cache[k] = (time.time(), value)
            _cache.move_to_end(k)
            while len(_cache) > MAX_ENTRIES:
                _cache.popitem(last=False)
    return value


def clear() -> None:
    with _lock:
        _cache.clear()
