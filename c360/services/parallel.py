"""Run a page's independent warehouse reads side by side.

One long-lived pool per worker process (as in services/insights.py): its threads are
reused, so each keeps its own Trino and Postgres connection across requests (both
connectors are per-thread). Only request threads submit work here - nothing running
on the pool submits more - so a busy pool queues, it never deadlocks.

The customer page and its Overview and HFCB tabs each made ten or more reads one
after another; on a slow day that was long enough for the page to time out
(2026-10-02). Side by side, a page takes about as long as its slowest read.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# Shared by every request thread in the worker (gunicorn runs 6). One customer open
# submits ~25 reads across its header, Overview and HFCB tabs, so with 8 threads a
# second user on the same worker queued behind the first: with several testers at
# once (2026-11-03) customers took far longer than their slowest read. 16 doubles
# that. Not higher by default: each pool thread keeps its own reporting-Postgres
# connection open (warehouse/connector.py), and 3 workers x 2 servers share that
# database's connection limit. C360_READ_POOL overrides.
POOL = ThreadPoolExecutor(max_workers=int(os.environ.get('C360_READ_POOL', '16') or '16'),
                          thread_name_prefix='c360-read')


def quiet(fn, *args):
    """Call ``fn``; None on any failure. For reads the page can do without."""
    try:
        return fn(*args)
    except Exception as exc:                                   # noqa: BLE001
        logger.warning('read %s failed: %s', getattr(fn, '__name__', fn), exc)
        return None


def submit(fn, *args, **kwargs) -> Future:
    """A read the page cannot do without: ``.result()`` re-raises its error."""
    return POOL.submit(fn, *args, **kwargs)


def optional(calls: dict[str, tuple]) -> dict[str, Future]:
    """Start ``{key: (fn, *args)}`` reads that may fail; each result is None if so."""
    return {k: POOL.submit(quiet, *call) for k, call in calls.items()}


def gather(futures: dict[str, Future]) -> dict[str, Any]:
    return {k: f.result() for k, f in futures.items()}
